# UI helpers — fresh uiautomator dumps, explicit screen markers, one sidecar per capture.
# A dump is never reused: /sdcard/w.xml is deleted before every attempt, the dump must
# print "dumped to", contain <hierarchy and be >400 bytes, otherwise it is a failure.
# Only `wait_app_ui` and `launch_fg` may relaunch the app, and only bounded and
# journalised; `wait_fg`/`wait_ui` never launch anything. A splash screen is not a
# screen state, and relaunching from wait_ui was how every dump ended up on Driver home.
RIDER_PKG="${RIDER_PKG:-sn.galass.galassrider}"
DRIVER_PKG="${DRIVER_PKG:-sn.galass.galassdriver}"
OUT="${OUT:-e2e-out}"
UI_XML_PATH="${UI_XML:-/tmp/w.xml}"
ADB_TMO="${ADB_TMO:-60}"
# Attente ACTIVE du premier plan : un cold start Flutter debug dépasse largement
# 60 s, d'où la borne haute (surchargeable) au lieu d'un sleep fixe.
WAIT_FG_SEC="${WAIT_FG_SEC:-600}"
mkdir -p "$OUT/screens" "$OUT/ui"

. "$(dirname "${BASH_SOURCE[0]}")/smoke_log.sh"

# Every adb call is bounded: a wedged emulator must not eat the job's wall clock.
# Callers see exit 124 and turn it into DUMP_UNAVAILABLE/FAIL instead of hanging.
adb() { command timeout "$ADB_TMO" adb "$@"; }
_ulog() {
  local line
  line=$(printf '[ui %s] %s' "$(date -u +%H:%M:%S)" "$*")
  printf '%s\n' "$line" >> "$OUT/run.log"
  smoke_emit "$line"
}

UI_DUMP_HASH=""
UI_DUMP_BYTES=0
UI_DUMP_VALID=0

# fg_pkg : paquet réellement au premier plan, sur le display PAR DÉFAUT. Jamais vide :
# UNKNOWN si illisible. Un `dumpsys window` multi-display imprime d'abord le launcher
# du display secondaire ; lire sa première ligne `mCurrentFocus` faisait croire que
# l'app du display 0 était absente (chaque attente échouait sur un mensonge).
# Le parseur (fg.py) ne retient que mDisplayId=0, puis bascule window -> activities -> top.
FG_PARSER="${FG_PARSER:-$(dirname "${BASH_SOURCE[0]}")/fg.py}"
PY="${PY:-python3}"
fg_pkg() {
  local cur="" dump
  if [ -f "$FG_PARSER" ]; then
    dump=$(adb shell dumpsys window displays 2>/dev/null) || dump=""
    cur=$(printf '%s\n' "$dump" | "$PY" "$FG_PARSER" --source window 2>/dev/null) || cur=""
    if [ -z "$cur" ]; then
      dump=$(adb shell dumpsys activity activities 2>/dev/null) || dump=""
      cur=$(printf '%s\n' "$dump" | "$PY" "$FG_PARSER" --source activities 2>/dev/null) || cur=""
    fi
    if [ -z "$cur" ]; then
      dump=$(adb shell dumpsys activity top 2>/dev/null) || dump=""
      cur=$(printf '%s\n' "$dump" | "$PY" "$FG_PARSER" --source top 2>/dev/null) || cur=""
    fi
  fi
  echo "${cur:-UNKNOWN}"
}

# ensure_fg : relance UNIQUEMENT si un AUTRE paquet connu est au premier plan.
# UNKNOWN (dump illisible, écran de transition) ne justifie jamais une relance.
ensure_fg() {
  local pkg="$1" cur
  cur=$(fg_pkg)
  [ "$cur" = "$pkg" ] && return 0
  case "$cur" in
    UNKNOWN|"") return 0 ;;
    *.*) ;;
    *) return 0 ;;
  esac
  _ulog "ensure_fg: $cur -> $pkg (relance)"
  adb shell monkey -p "$pkg" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
  sleep 3
  [ "$(fg_pkg)" = "$pkg" ]
}

# wait_fg pkg [timeout_s] : attente ACTIVE du premier plan. Sonde `fg_pkg` en
# boucle et rend 0 DÈS QUE le paquet est au premier plan — jamais un sleep fixe.
# Défaut `WAIT_FG_SEC` (600 s) : un cold start de debug Flutter peut dépasser 60 s,
# et une app absente du premier plan ne doit pas coûter le reste du parcours.
wait_fg() {
  local pkg="$1" tmo="${2:-$WAIT_FG_SEC}" t0=$SECONDS cur
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    cur=$(fg_pkg)
    [ "$cur" = "$pkg" ] && return 0
    sleep 2
  done
  _ulog "wait_fg: $pkg jamais au premier plan (${tmo}s, fg=$(fg_pkg))"
  return 1
}

# --- overlays système (run 40375590679) --------------------------------------
# Le dashboard Driver appelle requestIgnoreBatteryOptimization() : sans exemption
# batterie, Android ouvre le dialogue d'exemption, qui appartient à
# `com.android.settings` et VOLE le premier plan ~60 s — la capture « En ligne »
# ne voyait alors jamais l'accueil. `close_system_overlay` est le filet ; l'app
# réelle, elle, n'est jamais corrigée.
SYSTEM_OVERLAYS="com.android.settings com.android.permissioncontroller com.google.android.permissioncontroller"
OVERLAY_TRIES="${OVERLAY_TRIES:-3}"
OVERLAY_SETTLE_SEC="${OVERLAY_SETTLE_SEC:-1}"
LAUNCH_FG_SEC="${LAUNCH_FG_SEC:-180}"
FG_POLL_SEC="${FG_POLL_SEC:-2}"
WAIT_APP_UI_SLICE="${WAIT_APP_UI_SLICE:-15}"
WAIT_APP_UI_RELAUNCH_SEC="${WAIT_APP_UI_RELAUNCH_SEC:-30}"
WAIT_APP_UI_MAX_RELAUNCH="${WAIT_APP_UI_MAX_RELAUNCH:-2}"

# is_system_overlay pkg : 0 seulement pour un overlay système CONNU (fg.py décide,
# avec repli sur la même liste si le parseur est absent ou illisible). Launcher3,
# une app métier, UNKNOWN : jamais un overlay.
is_system_overlay() {
  local pkg="$1" rc
  [ -n "$pkg" ] || return 1
  if [ -f "$FG_PARSER" ]; then
    "$PY" "$FG_PARSER" --overlay "$pkg" >/dev/null 2>&1
    rc=$?
    case "$rc" in
      0) return 0 ;;
      1) return 1 ;;
    esac
  fi
  case " $SYSTEM_OVERLAYS " in
    *" $pkg "*) return 0 ;;
  esac
  return 1
}

# fg_component : composant pkg/activity du premier plan par défaut, "" si illisible.
# Sert à NOMMER l'écran fautif dans le journal (quel dialogue Réglages a volé le focus).
fg_component() {
  local dump comp
  [ -f "$FG_PARSER" ] || return 1
  dump=$(adb shell dumpsys window displays 2>/dev/null) || dump=""
  comp=$(printf '%s\n' "$dump" | "$PY" "$FG_PARSER" --component --source window 2>/dev/null) || comp=""
  if [ -z "$comp" ]; then
    dump=$(adb shell dumpsys activity activities 2>/dev/null) || dump=""
    comp=$(printf '%s\n' "$dump" | "$PY" "$FG_PARSER" --component --source activities 2>/dev/null) || comp=""
  fi
  printf '%s' "$comp"
  [ -n "$comp" ]
}

# launcher_component pkg : composant launcher résolu (resolve-activity --brief), ""
# et rc=1 s'il n'y en a pas — jamais un `am start` sans composant.
launcher_component() {
  local pkg="$1" out comp
  out=$(adb shell cmd package resolve-activity --brief \
        -c android.intent.category.LAUNCHER "$pkg" 2>/dev/null) || out=""
  comp=$(printf '%s\n' "$out" | grep -F '/' | tail -1 | tr -d '\r' \
         | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  printf '%s' "$comp"
  printf '%s' "$comp" | grep -Eq '^[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+$'
}

# close_system_overlay [essais] : si le premier plan est un overlay SYSTÈME, BACK
# puis `am force-stop` s'il résiste, borné (3 essais par défaut), composant
# journalisé. rc=0 quand plus aucun overlay n'est devant, 1 s'il persiste.
# Sur une app normale ou un dump illisible : AUCUN input, AUCUN force-stop.
close_system_overlay() {
  local tries="${1:-$OVERLAY_TRIES}" i=1 cur comp
  cur=$(fg_pkg)
  is_system_overlay "$cur" || return 0
  while [ "$i" -le "$tries" ]; do
    comp=$(fg_component 2>/dev/null) || comp=""
    _ulog "close_system_overlay: overlay $cur au premier plan${comp:+ (composant $comp)} — BACK $i/$tries"
    adb shell input keyevent 4 >/dev/null 2>&1 || true
    [ "$OVERLAY_SETTLE_SEC" = "0" ] || sleep "$OVERLAY_SETTLE_SEC"
    cur=$(fg_pkg)
    if ! is_system_overlay "$cur"; then
      _ulog "close_system_overlay: overlay fermé par BACK (fg=$cur)"
      return 0
    fi
    _ulog "close_system_overlay: $cur résiste au BACK — am force-stop $cur ($i/$tries)"
    adb shell am force-stop "$cur" >/dev/null 2>&1 || true
    [ "$OVERLAY_SETTLE_SEC" = "0" ] || sleep "$OVERLAY_SETTLE_SEC"
    cur=$(fg_pkg)
    if ! is_system_overlay "$cur"; then
      _ulog "close_system_overlay: overlay arrêté, premier plan rendu (fg=$cur)"
      return 0
    fi
    i=$((i + 1))
  done
  _ulog "close_system_overlay: overlay persistant ($cur) après $tries essais"
  return 1
}

# launch_fg pkg [timeout_s] : lance le composant launcher RÉSOLU via
# `am start -W -n`, puis rend 0 UNIQUEMENT quand `fg_pkg` = pkg, en refermant les
# overlays au passage. Au plus 2 lancements (jamais de boucle de redémarrage).
# rc=1 journalisé si le paquet n'est jamais devant — un lancement non prouvé n'est
# pas un lancement.
launch_fg() {
  local pkg="$1" tmo="${2:-$LAUNCH_FG_SEC}" t_end attempt=0 cur comp slice a_end
  t_end=$((SECONDS + tmo))
  while [ "$attempt" -lt 2 ] && [ "$SECONDS" -lt "$t_end" ]; do
    attempt=$((attempt + 1))
    comp=$(launcher_component "$pkg") || comp=""
    if [ -n "$comp" ]; then
      _ulog "launch_fg: am start -W -n $comp (essai $attempt/2)"
      adb shell am start -W -n "$comp" >/dev/null 2>&1 || true
    else
      _ulog "launch_fg: composant launcher introuvable pour $pkg — repli monkey (essai $attempt/2)"
      adb shell monkey -p "$pkg" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
    fi
    # Chaque essai reçoit une part du budget (moitié au premier) : un second
    # lancement ne sert que s'il lui reste du temps pour être prouvé.
    slice=$(( (t_end - SECONDS) / (3 - attempt) ))
    [ "$slice" -lt 1 ] && slice=1
    a_end=$((SECONDS + slice))
    while [ "$SECONDS" -lt "$a_end" ]; do
      cur=$(fg_pkg)
      if [ "$cur" = "$pkg" ]; then
        _ulog "launch_fg: $pkg au premier plan (essai $attempt/2, fg=$cur)"
        return 0
      fi
      close_system_overlay >/dev/null 2>&1 || true
      cur=$(fg_pkg)
      if [ "$cur" = "$pkg" ]; then
        _ulog "launch_fg: $pkg au premier plan après fermeture d'overlay (essai $attempt/2)"
        return 0
      fi
      [ "$SECONDS" -lt "$a_end" ] || break
      [ "$FG_POLL_SEC" = "0" ] || sleep "$FG_POLL_SEC"
    done
  done
  _ulog "launch_fg: $pkg jamais au premier plan (${attempt} lancement(s), tmo=${tmo}s, fg=$(fg_pkg))"
  return 1
}

# ui_xml : dump FRAIS et validé. 0 = ok, 4 = DUMP_UNAVAILABLE. Aucun /tmp/w.xml périmé.
ui_xml() {
  local try rc=4
  UI_DUMP_VALID=0
  UI_DUMP_HASH=""
  UI_DUMP_BYTES=0
  # Fixture déterministe pour les tests locaux uniquement : opt-in explicite, jamais en CI.
  if [ -n "${UI_XML_SRC:-}" ] && [ -s "$UI_XML_SRC" ]; then
    if [ "${E2E_ALLOW_FIXTURE:-0}" != "1" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
      _ulog "ui_xml: REFUS fixture (E2E_ALLOW_FIXTURE!=1 ou GITHUB_ACTIONS posé)"
      return 4
    fi
    printf '\n*** FIXTURE UI_XML_SRC=%s — preuve NON RÉELLE ***\n' "$UI_XML_SRC" >&2
    [ "$UI_XML_SRC" != "$UI_XML_PATH" ] && cp "$UI_XML_SRC" "$UI_XML_PATH"
    UI_DUMP_BYTES=$(wc -c < "$UI_XML_PATH" 2>/dev/null || echo 0)
    UI_DUMP_HASH=$(sha256sum "$UI_XML_PATH" 2>/dev/null | cut -d' ' -f1)
    UI_DUMP_VALID=1
    return 0
  fi
  rm -f /tmp/w.xml
  [ "$UI_XML_PATH" != "/tmp/w.xml" ] && rm -f "$UI_XML_PATH"
  # Run 40833325705 : 46 DUMP_UNAVAILABLE à partir de 07:48, chacun en ~4 s, ET
  # fg=UNKNOWN au même moment => l'appareil adb ne répondait plus (pas uiautomator).
  # La cause n'était jamais journalisée. Désormais : 1re ligne de la sortie du dump
  # et `adb get-state` dans le log ; appareil non `device` => `reconnect offline` +
  # `wait-for-device` borné ; essais 2 et 3 sur /data/local/tmp (pas /sdcard).
  local dev="/sdcard/w.xml" cause="" state=""
  for try in 1 2 3; do
    [ "$try" -gt 1 ] && dev="/data/local/tmp/w.xml"
    adb shell pkill -f uiautomator >/dev/null 2>&1 || true
    adb shell rm -f "$dev" >/dev/null 2>&1
    ADB_TMO=20 adb shell uiautomator dump --compressed "$dev" > "$OUT/ui/.dump-$try.out" 2>&1
    if ! grep -q "dumped to" "$OUT/ui/.dump-$try.out" 2>/dev/null; then
      cause=$(grep -v '^[[:space:]]*$' "$OUT/ui/.dump-$try.out" 2>/dev/null | head -n 1 | tr -d '\r' | cut -c1-160)
      [ -n "$cause" ] || cause="sortie vide (timeout ou adb muet)"
      state=$(adb get-state 2>&1 | head -n 1 | tr -d '\r')
      if [ "$state" != "device" ]; then
        _ulog "ui_xml: essai $try state=${state:-?} — adb reconnect offline + wait-for-device (${UI_DUMP_RECONNECT_TMO:-30}s)"
        adb reconnect offline >/dev/null 2>&1 || true
        ADB_TMO="${UI_DUMP_RECONNECT_TMO:-30}" adb wait-for-device >/dev/null 2>&1 || true
      fi
      sleep 1
      continue
    fi
    adb shell cat "$dev" > "$UI_XML_PATH" 2>/dev/null
    if grep -q "<hierarchy" "$UI_XML_PATH" 2>/dev/null \
       && [ "$(wc -c < "$UI_XML_PATH" 2>/dev/null || echo 0)" -gt 400 ]; then
      rc=0
      break
    fi
    sleep 1
  done
  if [ "$rc" -ne 0 ]; then
    rm -f "$UI_XML_PATH"
    _ulog "ui_xml: DUMP_UNAVAILABLE après 3 essais (state=${state:-?}, cause: ${cause:-dump vide ou <400 o})"
    return 4
  fi
  UI_DUMP_BYTES=$(wc -c < "$UI_XML_PATH" 2>/dev/null || echo 0)
  UI_DUMP_HASH=$(sha256sum "$UI_XML_PATH" 2>/dev/null | cut -d' ' -f1)
  UI_DUMP_VALID=1
  return 0
}

# clear_sys : ferme « X isn't responding » et replie le tiroir de notifications.
clear_sys() {
  adb shell cmd statusbar collapse >/dev/null 2>&1 || true
  grep -q "isn't responding" "$UI_XML_PATH" 2>/dev/null || return 0
  tap_exact "Wait" >/dev/null 2>&1 || tap_exact "Close app" >/dev/null 2>&1
  _ulog "dialogue système fermé"; sleep 2; ui_xml
}

# _spec_ok : chaque argument est une spec `+A|B|-C` (le type se propage sur les |).
# Négatif : seuls les noeuds visibles et non vides comptent (Flutter garde les routes
# précédentes en mémoire, visible-to-user="false"). Écrit la spec fautive dans ui/.last-spec-fail.
_spec_ok() {
  python3 - "$UI_XML_PATH" "$OUT/ui/.last-spec-fail" "$@" <<'PY'
import html, os, re, sys

path, failpath = sys.argv[1], sys.argv[2]
try:
    xml = open(path, encoding="utf-8", errors="replace").read()
except OSError:
    xml = ""

nodes = []
for m in re.finditer(r"<node\b[^>]*>", xml):
    tag = m.group(0)

    def attr(name):
        a = re.search(name + r'="([^"]*)"', tag)
        return html.unescape(a.group(1)) if a else ""

    b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
    bounds = tuple(map(int, b.groups())) if b else (0, 0, 0, 0)
    nodes.append({
        "edit": "EditText" in attr("class"),
        "text": attr("text"),
        "desc": attr("content-desc"),
        "visible": attr("visible-to-user").lower() != "false",
        "area": bounds[2] > bounds[0] and bounds[3] > bounds[1],
    })


def groups(spec):
    """`+A|B|-C` -> [('+', ['A','B']), ('-', ['C'])] (le type se propage sur les |)."""
    out, kind = [], None
    for raw in spec.split("|"):
        raw = raw.strip()
        if not raw:
            continue
        if raw[0] in "+-":
            kind, raw = raw[0], raw[1:]
        if not kind or not raw:
            continue
        if out and out[-1][0] == kind:
            out[-1][1].append(raw)
        else:
            out.append((kind, [raw]))
    return out


def compile_any(pats):
    rxs = []
    for pat in pats:
        try:
            rxs.append(re.compile(pat, re.I | re.S))
        except re.error:
            rxs.append(re.compile(re.escape(pat), re.I | re.S))
    return rxs


def hit(node, rx):
    return bool(rx.search(node["text"]) or rx.search(node["desc"]))


for spec in sys.argv[3:]:
    for kind, pats in groups(spec):
        rxs = compile_any(pats)
        if kind == "+":
            # B1: a positive must be visible-to-user too. An invisible Flutter route
            # kept in memory is not a screen, so it can never satisfy a marker.
            # `area` stays optional for positives (a visible zero-area node counts).
            # Un champ de saisie ne prouve rien : il contient ce que le harnais a
            # tapé (faux MATCH snapshot 15 run 40844335683, « Point E » dans l'EditText).
            ok = any(n["visible"] and not n["edit"] and hit(n, rx)
                     for n in nodes for rx in rxs)
        else:
            ok = not any(n["visible"] and n["area"] and hit(n, rx)
                         for n in nodes for rx in rxs)
        if not ok:
            try:
                open(failpath, "w").write(spec)
            except OSError:
                pass
            sys.exit(1 if kind == "+" else 2)
try:
    os.remove(failpath)
except OSError:
    pass
sys.exit(0)
PY
}

# wait_ui pkg timeout spec... -> 0 match, 1 timeout, 3 foreground mismatch, 4 no dump.
# Ne relance JAMAIS l'app. Le marqueur est AUTORITAIRE : un premier plan qui ne
# correspond pas est journalisé et la boucle continue (le dump a pu être pris
# pendant une transition, ou le display par défaut est momentanément illisible).
# rc=3 seulement si TOUS les relevés connus étaient étrangers ET aucun marqueur.
wait_ui() {
  local pkg="$1" tmo="${2:-30}" t0=$SECONDS rc=1 cur checks=0 foreign=0
  shift 2
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    if ! ui_xml; then
      rc=4
      sleep 1
      continue
    fi
    clear_sys
    cur=$(fg_pkg)
    if [ -n "$pkg" ] && [ "$cur" != "UNKNOWN" ]; then
      checks=$((checks + 1))
      if [ "$cur" != "$pkg" ]; then
        foreign=$((foreign + 1))
        _ulog "wait_ui: premier plan $cur != $pkg (marqueur prioritaire)"
      fi
    fi
    if _spec_ok "$@"; then
      return 0
    fi
    rc=1
    sleep 1
  done
  if [ "$rc" = "1" ] && [ "$checks" -gt 0 ] && [ "$foreign" -eq "$checks" ]; then
    return 3
  fi
  return "$rc"
}

# wait_app_ui pkg timeout spec... : attend un marqueur d'écran EN GARDANT l'app
# devant. Boucle `wait_ui` par tranches, referme les overlays système au passage,
# et relance l'app (au plus WAIT_APP_UI_MAX_RELAUNCH fois) si elle a perdu le
# premier plan. rc=0 dès que le marqueur matche ; rc=3 seulement si la boucle
# s'achève sans match. Ce rc ne prouve pas le premier plan : si celui-ci n'est pas
# le paquet, le dump de `snap` (ex. capture 33) rejette le MATCH en ACTION_MISS et
# le verdict reste WARN — la preuve finale est le dump de `snap`, pas ce retour.
wait_app_ui() {
  local pkg="$1" tmo="${2:-30}" t_end rc cur slice relaunch=0
  shift 2
  t_end=$((SECONDS + tmo))
  while [ "$SECONDS" -lt "$t_end" ]; do
    close_system_overlay >/dev/null 2>&1 || true
    slice=$((t_end - SECONDS))
    [ "$slice" -gt "$WAIT_APP_UI_SLICE" ] && slice="$WAIT_APP_UI_SLICE"
    [ "$slice" -lt 1 ] && slice=1
    wait_ui "$pkg" "$slice" "$@"
    rc=$?
    [ "$rc" = 0 ] && return 0
    cur=$(fg_pkg)
    if [ "$cur" != "$pkg" ] && [ "$relaunch" -lt "$WAIT_APP_UI_MAX_RELAUNCH" ] \
       && [ "$SECONDS" -lt "$t_end" ]; then
      relaunch=$((relaunch + 1))
      _ulog "wait_app_ui: $pkg a perdu le premier plan (fg=$cur) — relance $relaunch/$WAIT_APP_UI_MAX_RELAUNCH"
      launch_fg "$pkg" "$WAIT_APP_UI_RELAUNCH_SEC" >/dev/null 2>&1 || true
    fi
  done
  _ulog "wait_app_ui: marqueur jamais présent pour $pkg (tmo=${tmo}s, fg=$(fg_pkg))"
  return 3
}

# tap_exact label : noeud VISIBLE dont text ou content-desc, espaces normalisés,
# vaut EXACTEMENT le label, puis tap au centre. « Accepter la\ncourse » == « Accepter la course ».
tap_exact() {
  local label="$1" res
  if ! ui_xml; then
    # Repli par coordonnées : la dernière position où CE libellé a été vu et tapé.
    res=$(awk -F'\t' -v l="$label" '$1 == l { xy = $2 } END { print xy }' "$OUT/ui/.tap-cache" 2>/dev/null)
    if [ -n "$res" ]; then
      set -- "$1" $res
      adb shell input tap "$2" "$3" >/dev/null 2>&1
      _ulog "tap_exact $1 @ $2,$3 (dump indisponible — coordonnées mémorisées, tap aveugle)"
      return 0
    fi
    _ulog "tap_exact: dump indisponible pour '$1'"
    return 4
  fi
  res=$(python3 - "$UI_XML_PATH" "$label" <<'PY'
import html, re, sys

path, label = sys.argv[1], sys.argv[2]
norm = lambda s: re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_attr(s):
    return re.sub(r"\s+", " ", s).strip()


want = norm(label)
try:
    xml = open(path, encoding="utf-8", errors="replace").read()
except OSError:
    xml = ""
best = None
for m in re.finditer(r"<node\b[^>]*>", xml):
    tag = m.group(0)

    def attr(name):
        a = re.search(name + r'="([^"]*)"', tag)
        return html.unescape(a.group(1)) if a else ""

    if attr("visible-to-user").lower() == "false":
        continue
    b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
    if not b:
        continue
    x1, y1, x2, y2 = map(int, b.groups())
    if x2 <= x1 or y2 <= y1:
        continue
    if norm_attr(attr("text")) == want or norm_attr(attr("content-desc")) == want:
        best = ((x1 + x2) // 2, (y1 + y2) // 2)
        break
print("" if best is None else "%d %d" % best)
PY
)
  if [ -z "$res" ]; then
    _ulog "tap_exact miss: $1"
    return 1
  fi
  set -- "$1" $res
  adb shell input tap "$2" "$3" >/dev/null 2>&1
  printf '%s\t%s %s\n' "$1" "$2" "$3" >> "$OUT/ui/.tap-cache" 2>/dev/null || true
  _ulog "tap_exact $1 @ $2,$3"
}

# _find_node mode pattern -> "x y" du premier noeud VISIBLE, "" sinon.
# mode=re : text/content-desc contient la regex (insensible à la casse), EditText exclu.
# mode=class : la classe contient le motif (ex. EditText).
_find_node() {
  python3 - "$UI_XML_PATH" "$1" "$2" <<'PY'
import html, re, sys
path, mode, pat = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    xml = open(path, encoding="utf-8", errors="replace").read()
except OSError:
    xml = ""
rx = re.compile(pat, re.I | re.S) if mode == "re" else None
for m in re.finditer(r"<node\b[^>]*>", xml):
    tag = m.group(0)

    def attr(name):
        a = re.search(r'\b' + name + r'="([^"]*)"', tag)
        return html.unescape(a.group(1)) if a else ""

    if attr("visible-to-user").lower() == "false":
        continue
    b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
    if not b:
        continue
    x1, y1, x2, y2 = map(int, b.groups())
    if x2 <= x1 or y2 <= y1:
        continue
    cls = attr("class")
    if mode == "class":
        ok = pat in cls
    elif mode == "rid":
        rid = attr("resource-id")
        ok = rid == pat or rid.endswith(":id/" + pat) or rid.endswith("/" + pat)
    else:
        ok = "EditText" not in cls and (rx.search(attr("text")) or rx.search(attr("content-desc")))
    if ok:
        print("%d %d" % ((x1 + x2) // 2, (y1 + y2) // 2))
        break
PY
}

# tap_node mode pattern : dump frais puis tap au centre du premier noeud trouvé.
tap_node() {
  local res
  ui_xml || { _ulog "tap_node: dump indisponible ($1 '$2')"; return 4; }
  res=$(_find_node "$1" "$2")
  [ -n "$res" ] || { _ulog "tap_node miss: $1 '$2'"; return 1; }
  set -- "$1" "$2" $res
  adb shell input tap "$3" "$4" >/dev/null 2>&1
  _ulog "tap_node $1 '$2' @ $3,$4"
}

# planb_pick_place pkg open_label query result_regex : plan B RÉEL d'un choix de lieu.
# 1 tap sur open_label · 2 tap sur le champ (EditText) · 3 `input text` de query ·
# 4 attente d'un résultat qui matche result_regex puis tap · 5 « Confirmer le lieu ».
# rc 0 = tout fait ; 1..5 = numéro de l'étape qui a échoué (rien de plus n'est tapé).
# La recherche de l'app est Google Places : la requête doit être un VRAI lieu.
PLANB_SETTLE="${PLANB_SETTLE:-2}"
PLANB_RESULT_TMO="${PLANB_RESULT_TMO:-25}"
# planb_evidence query [tag] : capture + dump juste après la saisie (diagnostic
# Places : suggestions absentes ou invisibles pour l'arbre d'accessibilité).
PLANB_EVIDENCE_N=0
planb_evidence() {
  PLANB_EVIDENCE_N=$((PLANB_EVIDENCE_N + 1))
  local slug tag="${2:-typed}"
  slug=$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '-' | cut -c1-30)
  local base="planb-${PLANB_EVIDENCE_N}-${slug}-${tag}"
  mkdir -p "$OUT/screens" "$OUT/ui"
  adb exec-out screencap -p > "$OUT/screens/$base.png" 2>/dev/null || true
  if ui_xml; then
    cp -f "$UI_XML_PATH" "$OUT/ui/$base.xml" 2>/dev/null || true
    python3 - "$UI_XML_PATH" <<'PY' | head -25 | while IFS= read -r l; do _ulog "planb: noeud $l"; done
import html, re, sys
x = open(sys.argv[1], encoding="utf-8", errors="replace").read()
for t in re.findall(r"<node\b[^>]*>", x):
    g = lambda n: html.unescape((re.search(r'\b' + n + r'="([^"]*)"', t) or [None, ""])[1])
    lab = (g("text") or g("content-desc")).replace("\n", " ")[:60]
    rid = g("resource-id")
    if lab or rid:
        print("%s|%s|%s|%s" % (g("class").split(".")[-1], rid, lab, g("bounds")))
PY
  fi
  _ulog "planb: preuve $base (png $(wc -c < "$OUT/screens/$base.png" 2>/dev/null || echo 0)B, xml $(wc -c < "$OUT/ui/$base.xml" 2>/dev/null || echo 0)B)"
}

# rider_ui_login pkg : si l'écran de login est affiché (session injectée refusée),
# connecte le rider de test RIDER_TEST_PHONE / RIDER_TEST_OTP par l'UI.
rider_ui_login() {
  local pkg="$1"
  [ -n "${RIDER_TEST_PHONE:-}" ] && [ -n "${RIDER_TEST_OTP:-}" ] || { _ulog "login: RIDER_TEST_PHONE/OTP absents"; return 3; }
  ui_xml || return 4
  grep -qi 'Où allez-vous' "$UI_XML_PATH" && { _ulog "login: session déjà active"; return 0; }
  grep -q 'EditText' "$UI_XML_PATH" || { tap_when "$pkg" "Connexion" >/dev/null 2>&1 || true; sleep 2; }
  tap_node class EditText || { _ulog "login: champ téléphone introuvable"; return 1; }
  adb shell input text "$RIDER_TEST_PHONE" >/dev/null 2>&1
  adb shell input keyevent 4 >/dev/null 2>&1 || true
  tap_when "$pkg" "Connexion" || { _ulog "login: bouton connexion introuvable"; return 1; }
  sleep 5
  tap_node class EditText || { _ulog "login: champ OTP introuvable"; return 2; }
  adb shell input text "$RIDER_TEST_OTP" >/dev/null 2>&1
  sleep 1
  tap_when "$pkg" "Vérifier" >/dev/null 2>&1 || true
  _ulog "login: rider de test soumis (numéro et OTP masqués)"
  return 0
}

planb_pick_place() {
  local pkg="$1" open="$2" query="$3" want="$4" enc
  _ulog "planb: '$open' -> saisie '$query' -> résultat /$want/"
  tap_when "$pkg" "$open" || { _ulog "planb: échec étape 1 (tap '$open')"; return 1; }
  sleep "${PLANB_SETTLE:-2}"
  tap_node class EditText || { _ulog "planb: échec étape 2 (champ de saisie introuvable)"; return 2; }
  sleep 1
  enc=$(printf '%s' "$query" | sed 's/ /%s/g')
  adb shell input text "$enc" >/dev/null 2>&1 || { _ulog "planb: échec étape 3 (input text)"; return 3; }
  _ulog "planb: saisi '$query'"
  planb_evidence "$query"
  # Le champ lui-même contient la requête : on attend un noeud NON EditText (une
  # suggestion Places), en sondant jusqu'à PLANB_RESULT_TMO.
  local t0=$SECONDS hit=1 rtmo="${PLANB_RESULT_TMO:-25}"
  case "$rtmo" in ''|*[!0-9]*) rtmo=25 ;; esac
  while :; do
    # Semantics(identifier) Flutter -> resource-id : la 1re suggestion Places,
    # sans regex sur son libellé ; repli sur le libellé attendu (anciens APK).
    tap_node rid place_prediction_0 && { hit=0; _ulog "planb: place_prediction_0 tapé"; break; }
    tap_node re "$want" && { hit=0; break; }
    [ $((SECONDS - t0)) -ge "$rtmo" ] && break
    sleep 1
  done
  if [ "$hit" -ne 0 ]; then
    _ulog "planb: ni place_prediction_0 ni /$want/ après ${rtmo}s — pas de confirmation"
    planb_evidence "$query" nomatch
    return 4
  fi
  sleep "${PLANB_SETTLE:-2}"
  tap_node rid confirm_location || tap_when "$pkg" "Confirmer le lieu" || { _ulog "planb: échec étape 5 (Confirmer le lieu)"; return 5; }
  _ulog "planb: OK '$query'"
  return 0
}

# ui_action pkg label : attend un libellé tolérant aux espaces, tape, EXIGE le dialogue.
# B3: a tap whose confirmation dialog never opened is not a successful action, so the
# no-dialog branch returns 1 (the old `|| true` + trailing sleep made it look fine).
# The dialog is probed exactly (+^Confirmer$) so the trigger label (« Confirmer l'arrêt »)
# can never satisfy its own confirmation.
ui_action() {
  local pkg="$1" lab="$2" re tag
  re=$(printf '%s' "$lab" | python3 -c 'import re,sys;print(r"\s+".join(re.escape(w) for w in sys.stdin.read().split()))')
  wait_ui "$pkg" 20 "+$re" || return 1
  tap_exact "$lab" || return 1
  sleep 2
  tag=$(echo "$lab" | tr -c 'a-zA-Z' '_')
  if wait_ui "$pkg" "${UI_CONFIRM_TIMEOUT:-8}" '+^Confirmer$'; then
    tap_exact "Confirmer" || return 1
    sleep 2
    return 0
  fi
  adb exec-out screencap -p > "$OUT/screens/dbg-after-$tag.png" 2>/dev/null
  _ulog "pas de dialogue Confirmer après $lab"
  return 1
}

# tap_when pkg label... : attend puis tape, sans exiger de dialogue (ouverture de picker,
# navigation). Réservé aux actions dont la preuve est l'écran SUIVANT, pas le dialogue.
tap_when() {
  local pkg="$1" lab re; shift
  for lab in "$@"; do
    re=$(printf '%s' "$lab" | python3 -c 'import re,sys;print(r"\s+".join(re.escape(w) for w in sys.stdin.read().split()))')
    if wait_ui "$pkg" 15 "+$re" && tap_exact "$lab"; then
      sleep 2
      return 0
    fi
  done
  return 1
}

_write_sidecar() {
  # name pkg spec status waited action action_ok point marker_source failed_spec
  python3 - "$OUT/screens/$1.json" "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}" \
            "$OUT/screens/$1.png" "$OUT/ui/$1.xml" "$(fg_pkg 2>/dev/null || echo UNKNOWN)" <<'PY'
import hashlib, json, os, re, sys, time

(path, name, pkg, spec, status, waited, action, action_ok,
 point, source, failed, png, xml, fg) = sys.argv[1:15]


def sha(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


labels = []
try:
    raw = open(xml, encoding="utf-8", errors="replace").read()
except OSError:
    raw = ""
count = len(raw.encode()) if raw else 0
node_count = 0
for m in re.finditer(r"<node\b[^>]*>", raw):
    node_count += 1
    tag = m.group(0)
    if re.search(r'visible-to-user="false"', tag):
        continue
    for attr in ("text", "content-desc"):
        a = re.search(attr + r'="([^"]*)"', tag)
        if not a:
            continue
        value = re.sub(r"\s+", " ", a.group(1)).strip()
        if value and value not in labels:
            labels.append(value)
labels = labels[:40]

json.dump({
    "name": name, "point": point, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "pkg": pkg, "fg_pkg": fg, "marker": spec, "marker_source": source,
    "status": status, "waited_s": int(waited or 0),
    "action": action, "action_ok": action_ok == "1",
    "dump_sha256": sha(xml), "dump_bytes": count, "png_sha256": sha(png),
    "node_count": node_count,
    "labels": labels, "failed_spec": failed,
    "screenshot": f"{name}.png",
    "ride_state": (open(os.path.join(os.path.dirname(os.path.dirname(path)),
                                     "last-ride-state.txt"), encoding="utf-8").read().strip()
                   if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(path)),
                                                  "last-ride-state.txt")) else ""),
}, open(path, "w"), indent=1)
PY
}

# snap name pkg timeout spec [action_label] [point] [marker_source]
# 0 MATCH · 3 TIMEOUT · 4 DUMP_UNAVAILABLE · 5 ACTION_MISS / CAPTURE_MISS
# The point is passed in explicitly (S4): a capture can never file itself under the
# previous point, which is how capture 33 landed under rider-home-loaded.
snap() {
  local name="$1" pkg="$2" tmo="${3:-30}" spec="${4:-}" action="${5:-}"
  local point="${6:-}" source="${7:-grounded}" t0=$SECONDS rc st first_hash png_bytes cur
  wait_ui "$pkg" "$tmo" "$spec"
  rc=$?
  case "$rc" in
    0) st=MATCH ;;
    4) st=DUMP_UNAVAILABLE ;;
    *) st=TIMEOUT ;;
  esac
  first_hash="$UI_DUMP_HASH"
  # The screencap is taken immediately after the dump that satisfied the marker.
  adb exec-out screencap -p > "$OUT/screens/$name.png" 2>/dev/null
  png_bytes=$(wc -c < "$OUT/screens/$name.png" 2>/dev/null || echo 0)
  [ -z "$png_bytes" ] && png_bytes=0
  if [ "$st" = MATCH ] && [ "$png_bytes" -lt 8 ]; then
    # B4: an empty screencap is not evidence; never let it ride a MATCH, and never
    # keep the stub file as if it were a screenshot.
    st=CAPTURE_MISS
    rm -f "$OUT/screens/$name.png"
    _ulog "snap $name: screencap vide (${png_bytes}B) — pas de MATCH"
  fi
  # S8: the pixels must still show the screen the marker matched. Re-check the
  # foreground and re-dump right after the capture; a changed screen invalidates it.
  if [ "$st" = MATCH ] && [ -n "$first_hash" ]; then
    if ui_xml; then
      cur=$(fg_pkg)
      if [ -n "$pkg" ] && [ "$cur" != "UNKNOWN" ] && [ "$cur" != "$pkg" ]; then
        st=ACTION_MISS
        _ulog "snap $name: premier plan $cur != $pkg après capture"
      elif ! _spec_ok "$spec"; then
        st=ACTION_MISS
        _ulog "snap $name: marqueur absent au second dump"
      elif [ "$UI_DUMP_HASH" != "$first_hash" ]; then
        # A live screen (ETA, distance) changes its dump every second. The marker
        # still holds, so the pixels still show the claimed state; the drift is
        # logged instead of voiding the capture (see rapport: écart S8 assumé).
        _ulog "snap $name: digest changé mais marqueur stable (écran vivant)"
      fi
    fi
  fi
  if [ -n "$UI_DUMP_VALID" ] && [ "$UI_DUMP_VALID" = "1" ]; then
    cp "$UI_XML_PATH" "$OUT/ui/$name.xml" 2>/dev/null || true
  else
    : > "$OUT/ui/$name.xml"
  fi
  local action_ok=0 failed=""
  [ -f "$OUT/ui/.last-spec-fail" ] && failed=$(cat "$OUT/ui/.last-spec-fail")
  if [ -n "$action" ] && [ "$st" = MATCH ]; then
    if tap_exact "$action"; then action_ok=1; sleep 2; else st=ACTION_MISS; fi
  fi
  _write_sidecar "$name" "$pkg" "$spec" "$st" "$((SECONDS - t0))" "$action" "$action_ok" \
                 "$point" "$source" "$failed"
  echo "$name|$st|fg=$(fg_pkg)|point=$point|marker=$spec|wait=$((SECONDS - t0))s" \
    >> "$OUT/captures.txt"
  _ulog "snap $name $st ($((SECONDS - t0))s)"
  case "$st" in
    MATCH) return 0 ;;
    DUMP_UNAVAILABLE) return 4 ;;
    ACTION_MISS|CAPTURE_MISS) return 5 ;;
    *) return 3 ;;
  esac
}
