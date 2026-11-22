# UI helpers — fresh uiautomator dumps, explicit screen markers, one sidecar per capture.
# A dump is never reused: /sdcard/w.xml is deleted before every attempt, the dump must
# print "dumped to", contain <hierarchy and be >400 bytes, otherwise it is a failure.
# No helper here relaunches the app on its own during a wait: a splash screen is not a
# screen state, and relaunching from wait_ui was how every dump ended up on Driver home.
RIDER_PKG="${RIDER_PKG:-sn.galass.galassrider}"
DRIVER_PKG="${DRIVER_PKG:-sn.galass.galassdriver}"
OUT="${OUT:-e2e-out}"
UI_XML_PATH="${UI_XML:-/tmp/w.xml}"
ADB_TMO="${ADB_TMO:-60}"
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

# fg_pkg : paquet réellement au premier plan. Jamais vide : UNKNOWN si illisible.
fg_pkg() {
  local cur=""
  cur=$(adb shell dumpsys window 2>/dev/null \
        | grep -m1 -E "mCurrentFocus|mFocusedApp" \
        | grep -oE "[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+" | head -1 | cut -d/ -f1)
  if [ -z "$cur" ]; then
    cur=$(adb shell dumpsys activity activities 2>/dev/null \
          | grep -m1 -E "topResumedActivity|mResumedActivity" \
          | grep -oE "[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+" | head -1 | cut -d/ -f1)
  fi
  echo "${cur:-UNKNOWN}"
}

# xml_pkg_of file : paquet de la fenêtre applicative d'après un dump uiautomator donné
# (attribut package du premier noeud hors com.android.systemui). Vide si illisible.
xml_pkg_of() {
  [ -n "${1:-}" ] && [ -s "$1" ] || { echo ""; return 0; }
  grep -oE 'package="[^"]+"' "$1" 2>/dev/null \
    | cut -d'"' -f2 | grep -v '^com.android.systemui$' | head -1
}

# xml_pkg : paquet de la fenêtre applicative d'après le DERNIER dump uiautomator valide.
# Vide si pas de dump valide. Sert de preuve de premier plan quand dumpsys renvoie le
# launcher alors que l'UI de l'app est bien affichée (steps 14/33 : « marqueur absent »
# sur 29 points alors que le XML montrait l'app).
xml_pkg() {
  [ "${UI_DUMP_VALID:-0}" = "1" ] || { echo ""; return 0; }
  xml_pkg_of "$UI_XML_PATH"
}

# fg_is pkg : vrai si dumpsys OU un dump uiautomator frais place pkg au premier plan.
fg_is() {
  [ "$(fg_pkg)" = "$1" ] && return 0
  ui_xml && [ "$(xml_pkg)" = "$1" ]
}

# fg_diag : trace brute des signaux de premier plan (sans secret) pour comprendre un écart.
fg_diag() {
  _ulog "fg_diag window: $(adb shell dumpsys window 2>/dev/null | grep -m2 -E 'mCurrentFocus|mFocusedApp' | tr -s ' ' | tr '\n' '|')"
  _ulog "fg_diag activity: $(adb shell dumpsys activity activities 2>/dev/null | grep -m2 -E 'topResumedActivity|mResumedActivity' | tr -s ' ' | tr '\n' '|')"
  _ulog "fg_diag xml_pkg=$(xml_pkg)"
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
  for try in 1 2 3; do
    adb shell pkill -f uiautomator >/dev/null 2>&1 || true
    adb shell rm -f /sdcard/w.xml >/dev/null 2>&1
    ADB_TMO=20 adb shell uiautomator dump --compressed /sdcard/w.xml > "$OUT/ui/.dump-$try.out" 2>&1
    if ! grep -q "dumped to" "$OUT/ui/.dump-$try.out" 2>/dev/null; then
      sleep 1
      continue
    fi
    adb shell cat /sdcard/w.xml > "$UI_XML_PATH" 2>/dev/null
    if grep -q "<hierarchy" "$UI_XML_PATH" 2>/dev/null \
       && [ "$(wc -c < "$UI_XML_PATH" 2>/dev/null || echo 0)" -gt 400 ]; then
      rc=0
      break
    fi
    sleep 1
  done
  if [ "$rc" -ne 0 ]; then
    rm -f "$UI_XML_PATH"
    EMU_DUMP_FAILS=$(( ${EMU_DUMP_FAILS:-0} + 1 ))
    [ "$EMU_DUMP_FAILS" -gt "${EMU_DUMP_FAILS_MAX:-0}" ] && EMU_DUMP_FAILS_MAX=$EMU_DUMP_FAILS
    _ulog "ui_xml: DUMP_UNAVAILABLE après 3 essais (d'affilée: $EMU_DUMP_FAILS)"
    return 4
  fi
  EMU_DUMP_FAILS=0
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

# _spec_ok : chaque argument est une spec `+A|B&-C` (le type se propage sur les | et les &).
# `|` = OU à l'intérieur d'un groupe, `&` = ET (nouveau groupe, même type hérité) : deux
# signaux distincts du même écran (compteur ET bannière) s'écrivent `+compteur&+bannière`.
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
        "text": attr("text"),
        "desc": attr("content-desc"),
        "visible": attr("visible-to-user").lower() != "false",
        "area": bounds[2] > bounds[0] and bounds[3] > bounds[1],
    })


def groups(spec):
    """`+A|B&-C` -> [('+', ['A','B']), ('-', ['C'])] (OU dans un groupe, ET entre groupes).

    `|` ajoute une alternative au groupe courant ; `&` ouvre un nouveau groupe (le type
    se propage) : deux signaux distincts s'écrivent `+compteur&+bannière`.
    """
    out, kind, new_group = [], None, True
    for raw in re.split(r"([|&])", spec):
        if raw == "|":
            new_group = False
            continue
        if raw == "&":
            new_group = True
            continue
        raw = raw.strip()
        if not raw:
            continue
        if raw[0] in "+-":
            kind, raw = raw[0], raw[1:]
        if not kind or not raw:
            continue
        if new_group or not out or out[-1][0] != kind:
            out.append((kind, [raw]))
        else:
            out[-1][1].append(raw)
        new_group = False
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
            ok = any(n["visible"] and hit(n, rx) for n in nodes for rx in rxs)
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
# Ne relance JAMAIS l'app : comparer le premier plan après le dump, c'est tout.
wait_ui() {
  local pkg="$1" tmo="${2:-30}" t0=$SECONDS rc=1 cur
  shift 2
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    if ! ui_xml; then
      rc=4
      sleep 1
      continue
    fi
    clear_sys
    cur=$(fg_pkg)
    if [ -n "$pkg" ] && [ "$cur" != "UNKNOWN" ] && [ "$cur" != "$pkg" ]; then
      # Le dump uiautomator fait foi : si l'app attendue y est, dumpsys se trompe.
      if [ "$(xml_pkg)" = "$pkg" ]; then
        _ulog "wait_ui: dumpsys=$cur mais le dump uiautomator montre $pkg (dumpsys ignoré)"
        fg_diag
      else
        _ulog "wait_ui: premier plan $cur != $pkg (xml=$(xml_pkg))"
        fg_diag
        return 3
      fi
    fi
    if _spec_ok "$@"; then
      return 0
    fi
    rc=1
    sleep 1
  done
  return "$rc"
}

# tap_exact label : noeud VISIBLE dont text ou content-desc, espaces normalisés,
# vaut EXACTEMENT le label, puis tap au centre. « Accepter la\ncourse » == « Accepter la course ».
tap_exact() {
  local label="$1" res
  if ! ui_xml; then
    _ulog "tap_exact: dump indisponible pour '$1'"
    return 4
  fi
  res=$(python3 - "$UI_XML_PATH" "$label" <<'PY'
import html, os, re, sys

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
        # TAP_PICK=last : le dernier nœud gagne (bouton placé sous un titre au même libellé).
        if os.environ.get("TAP_PICK") != "last":
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
  _ulog "tap_exact $1 @ $2,$3"
}

# tap_prefix pkg prefix [timeout_s] : tape la première suggestion (nœud visible, hors champ de saisie)
# dont le texte COMMENCE par prefix, sans accent ni casse. Google Places rend la description
# complète (« Point E, Dakar, Senegal ») : un libellé exact ne peut pas la viser.
tap_prefix() {
  local pkg="$1" pre="$2" limit="${3:-20}" res t0=$SECONDS
  while [ $((SECONDS - t0)) -lt "$limit" ]; do
    if ui_xml && [ "$(xml_pkg)" = "$pkg" ]; then
      res=$(python3 - "$UI_XML_PATH" "$pre" <<'PY'
import html, re, sys, unicodedata

path, pre = sys.argv[1], sys.argv[2]


def norm(s):
    s = unicodedata.normalize("NFKD", html.unescape(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


want = norm(pre)
try:
    xml = open(path, encoding="utf-8", errors="replace").read()
except OSError:
    xml = ""
hit = None
nav_top = None
for m in re.finditer(r"<node\b[^>]*>", xml):
    tag = m.group(0)

    def attr(name):
        a = re.search(name + r'="([^"]*)"', tag)
        return a.group(1) if a else ""

    if attr("visible-to-user").lower() == "false":
        continue
    b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
    if not b:
        continue
    x1, y1, x2, y2 = map(int, b.groups())
    if x2 <= x1 or y2 <= y1:
        continue
    txt = attr("text")
    # La nav porte son libellé en content-desc (text vide) — preuve run 44674958244.
    if txt in ("Accueil", "Activité", "Menu") or attr("content-desc") in ("Accueil", "Activité", "Menu"):
        nav_top = y1 if nav_top is None else min(nav_top, y1)
    if hit is None and "EditText" not in attr("class"):
        for t in (txt, attr("content-desc")):
            n = norm(t)
            if n.startswith(want) and len(n) > len(want):
                hit = ((x1 + x2) // 2, (y1 + y2) // 2)
                break
if hit is not None:
    print(hit[0], hit[1], nav_top if nav_top is not None else "")
PY
)
      if [ -n "$res" ]; then
        set -- $res
        # Preuve run 44663541595 : la nav flottante (« Accueil/Activité/Menu ») se superpose à une
        # carte de service en bas de liste et intercepte le tap avant qu'il n'atteigne la carte.
        if [ -n "${3:-}" ] && [ "$2" -ge "$3" ]; then
          _ulog "tap_prefix $pre @ $1,$2 masqué par la nav (nav_top=$3) : défilement"
          adb shell input swipe 540 1900 540 700 300 >/dev/null 2>&1
          sleep 1
        else
          adb shell input tap "$1" "$2" >/dev/null 2>&1
          _ulog "tap_prefix $pre @ $1,$2"
          sleep 2
          return 0
        fi
      fi
    fi
    sleep 2
  done
  _ulog "tap_prefix miss: $pre"
  return 1
}

# ui_screen_diag label : journalise les textes visibles (sans secret) de l'écran courant, pour
# expliquer un TIMEOUT ou un repli API sans déchiffrer les preuves.
ui_screen_diag() {
  local lab="$1" d
  ui_xml || { _ulog "$lab: écran illisible (dump indisponible)"; return 0; }
  d=$(python3 "$(dirname "${BASH_SOURCE[0]}")/dump_diag.py" "$UI_XML_PATH" --texts 2>/dev/null | sed -E 's/(key|token|access_token)=[^&" ]+/\1=***/g')
  _ulog "$lab: ${d:-textes indisponibles}"
}

# tap_node_mode mode pkg [label] : tape le nœud trouvé par tap_node.py (--empty-input |
# --row-control label). rc 0 si un nœud a été tapé.
tap_node_mode() {
  local mode="$1" pkg="$2" lab="${3:-}" res x y kind t0=$SECONDS
  while [ $((SECONDS - t0)) -lt 15 ]; do
    if ui_xml && [ "$(xml_pkg)" = "$pkg" ]; then
      if [ -n "$lab" ]; then res=$(python3 "$(dirname "${BASH_SOURCE[0]}")/tap_node.py" "$mode" "$UI_XML_PATH" "$lab")
      else res=$(python3 "$(dirname "${BASH_SOURCE[0]}")/tap_node.py" "$mode" "$UI_XML_PATH"); fi
      if [ -n "$res" ]; then
        read -r x y kind <<<"$res"
        adb shell input tap "$x" "$y" >/dev/null 2>&1
        _ulog "tap_node_mode $mode ${lab:+« $lab » }@ $x,$y ($kind)"
        sleep 2
        return 0
      fi
    fi
    sleep 2
  done
  _ulog "tap_node_mode $mode ${lab:+« $lab » }: aucun nœud en 15s"
  return 1
}

# dialog_confirm pkg confirm cancel [appear_s] [gone_s] : fenêtre à deux boutons (ex. « Prise en
# charge du client » : Annuler / Confirmer). Détecte la fenêtre, tape le bouton de confirmation
# (parent cliquable du nœud), puis ATTEND qu'elle ait disparu. 0 = confirmée et fermée,
# 2 = aucune fenêtre apparue, 1 = tapée mais toujours là (retap unique à mi-délai).
dialog_confirm() {
  local pkg="$1" ok="$2" no="$3" appear="${4:-15}" gone="${5:-20}" t0=$SECONDS seen=0 res x y kind retapped=0 tool
  tool="$(dirname "${BASH_SOURCE[0]}")/tap_node.py"
  while [ $((SECONDS - t0)) -lt "$appear" ]; do
    if ui_xml && [ -n "$(python3 "$tool" "$UI_XML_PATH" "$ok")" ] && [ -n "$(python3 "$tool" "$UI_XML_PATH" "$no")" ]; then
      seen=1; break
    fi
    sleep 2
  done
  [ "$seen" = 1 ] || { _ulog "dialog_confirm: aucune fenêtre $no/$ok en ${appear}s"; return 2; }
  _ulog "dialog_confirm: fenêtre $no/$ok détectée"
  t0=$SECONDS
  while [ $((SECONDS - t0)) -lt "$gone" ]; do
    res=$(python3 "$tool" "$UI_XML_PATH" "$ok")
    if [ -n "$res" ] && { [ "$retapped" = 0 ] || [ $((SECONDS - t0)) -ge $((gone / 2)) ]; }; then
      read -r x y kind <<<"$res"
      adb shell input tap "$x" "$y" >/dev/null 2>&1
      _ulog "dialog_confirm: « $ok » tapé @ $x,$y ($kind)"
      retapped=1
    fi
    sleep 2
    if ui_xml; then
      if [ -z "$(python3 "$tool" "$UI_XML_PATH" "$no")" ] || [ -z "$(python3 "$tool" "$UI_XML_PATH" "$ok")" ]; then
        _ulog "dialog_confirm: fenêtre fermée après $((SECONDS - t0))s"
        return 0
      fi
    fi
  done
  _ulog "dialog_confirm: fenêtre toujours présente après ${gone}s"
  return 1
}

# last_stops_done : stops_done=N lu dans $OUT/last-ride-state.txt (écrit par ride_state), vide si absent.
last_stops_done() { sed -n 's/.*stops_done=\([0-9][0-9]*\).*/\1/p' "$OUT/last-ride-state.txt" 2>/dev/null | tail -1; }

# app_logcat_since ts : lignes logcat de l'app Driver (processus du paquet) depuis ts « MM-JJ HH:MM:SS.mmm » :
# tout le tag flutter (Logger : url/status des appels HTTP, complete-stop POST/rejected/accepted), exceptions, FATAL.
# Jetons masqués. Statistiques (brut = lignes de la fenêtre, app = lignes du pid du Driver) dans APP_LC_STATS pour que
# « 0 ligne » soit explicable. Run 45046315983 : 0 ligne car la commande date non quotée perdait ses
# quotes côté appareil (ts vide ; commande date passée en 2 arguments) ; sans ts valide on prend désormais les 600 dernières lignes.
APP_LC_STATS=""
app_logcat_since() {
  local ts="${1:-}" pid n_raw=0 n_app=0 raw="$OUT/diag/.applc-raw.txt" app="$OUT/diag/.applc-app.txt"
  local -a tsarg=(-t 600)
  mkdir -p "$OUT/diag"
  case "$ts" in
    [0-9][0-9]-[0-9][0-9]\ [0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]) tsarg=(-T "$ts") ;;
    *) ts="" ;;
  esac
  pid=$(adb shell pidof "$DRIVER_PKG" 2>/dev/null | tr -d '\r' | awk '{print $1}')
  adb logcat -d -v threadtime "${tsarg[@]}" > "$raw" 2>/dev/null || true
  n_raw=$(wc -l < "$raw" 2>/dev/null | tr -d ' ')
  if [ -n "$pid" ]; then awk -v p="$pid" '$3==p' "$raw" > "$app"; else cp "$raw" "$app"; fi
  n_app=$(wc -l < "$app" 2>/dev/null | tr -d ' ')
  APP_LC_STATS="ts=${ts:-aucun(600 dernières lignes)} pid=${pid:-inconnu} brut=${n_raw:-0} app=${n_app:-0}"
  grep -aE ' [VDIWEF] flutter *:|complete-stop|rejected|accepted|refused|http=|DioException|Exception|FATAL' "$app" \
    | sed -E 's/(key|token|access_token|authorization)([=:]) *[^&" ]+/\1\2***/Ig' | tail -60
  rm -f "$raw" "$app"
}

# fg_component pkg : composant lanceur du paquet (résolu HORS fenêtre d'offre, avant le tap de demande).
fg_component() {
  adb shell cmd package resolve-activity --brief -c android.intent.category.LAUNCHER "$1" 2>/dev/null | tail -1 | tr -d '\r'
}

# fg_switch_now pkg [composant] : bascule d'avant-plan SANS attente (un seul am start ; repli monkey).
# Une offre vit 30 s (offer_timeout_sec) : run 45051973500, 21 s perdus entre le tap de demande et le Driver
# au premier plan (deadline_check, dumpsys, wait_fg, dumps uiautomator) -> offre expirée avant d'être vue.
fg_switch_now() {
  local pkg="$1" comp="${2:-}"
  if [[ "$comp" == */* ]]; then adb shell am start -n "$comp" -f 0x20000 >/dev/null 2>&1 || true
  else adb shell monkey -p "$pkg" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true; fi
  return 0
}

# offer_timeout_diag : TIMEOUT d'offre -> logcat du Driver depuis la demande (OFFER_REQ_TS) dans
# diag/offer-timeout.logcat + lignes utiles au log. Distingue « jamais reçue » (aucun loadData/dispatch)
# de « reçue trop tard » (loadData après l'expiration). Jamais bloquant.
offer_timeout_diag() {
  local f="$OUT/diag/offer-timeout.logcat" n
  mkdir -p "$OUT/diag"
  app_logcat_since "${OFFER_REQ_TS:-}" > "$f" 2>/dev/null || true
  n=$(wc -l < "$f" 2>/dev/null | tr -d ' ')
  _ulog "offer_timeout_diag: logcat=${n:-0} ligne(s) [${APP_LC_STATS}] depuis ${OFFER_REQ_TS:-?}"
  grep -aE 'APP STATE|\[dispatch\]|loadData|onOffer|offer #' "$f" 2>/dev/null | tail -15 \
    | while IFS= read -r l; do _ulog "offer_timeout_diag app: ${l:0:220}"; done
  return 0
}

# ui_texts_log tag : textes visibles de l'écran courant dans le log (un toast de refus dure 5 s).
ui_texts_log() {
  local tag="$1" texts=""
  ui_xml && texts=$(python3 "$(dirname "${BASH_SOURCE[0]}")/dump_diag.py" "$UI_XML_PATH" --texts 2>/dev/null | cut -c1-300)
  _ulog "ecran $tag: ${texts:-dump indisponible}"
}

# stop_diag tag : preuves d'une tentative d'arrêt (capture, dump, extrait logcat). Texte seul, aucun secret.
# Écrit TOUJOURS dans le log (« stop_diag dbg-<tag> ») : les fichiers sous diag/ et screens/ ne sont
# pas lisibles dans le log CI (run 45004400552 : aucune ligne « dbg-stop » alors que les taps avaient eu lieu).
stop_diag() {
  local tag="$1" texts="" lc
  mkdir -p "$OUT/diag" "$OUT/screens"
  adb exec-out screencap -p > "$OUT/screens/dbg-$tag.png" 2>/dev/null || true
  if ui_xml; then
    cp "$UI_XML_PATH" "$OUT/diag/dbg-$tag.xml" 2>/dev/null || true
    texts=$(python3 "$(dirname "${BASH_SOURCE[0]}")/dump_diag.py" "$UI_XML_PATH" --texts 2>/dev/null | cut -c1-300)
  fi
  app_logcat_since "${STOP_TAP_TS:-}" > "$OUT/diag/dbg-$tag.logcat" 2>/dev/null || true
  lc=$(wc -l < "$OUT/diag/dbg-$tag.logcat" 2>/dev/null | tr -d ' ')
  _ulog "stop_diag dbg-$tag: png=screens/dbg-$tag.png xml=diag/dbg-$tag.xml logcat=${lc:-0} ligne(s) [${APP_LC_STATS}] | ${texts:-dump indisponible}"
  [ "${lc:-0}" -gt 0 ] && tail -12 "$OUT/diag/dbg-$tag.logcat" | while IFS= read -r l; do _ulog "stop_diag dbg-$tag app: ${l:0:220}"; done
  return 0
}

# stop_prime n : le serveur exige un ping de moins de 15 min et moins de 500 m de l'arrêt
# (RideStopProgressionService::assertStopProximity). Avant CHAQUE tap : GPS émulateur sur l'arrêt, ping serveur
# au même point (API), puis attente (bornée) d'un ping du Driver lui-même dans son logcat. Jamais bloquant.
# Tableaux STOP_LAT / STOP_LNG posés par run.sh ; sans eux ou sans `api`, ne fait rien.
stop_prime() {
  local n="$1" i=$(( $1 - 1 )) lat lng ts t=0 seen=0
  lat="${STOP_LAT[$i]:-}"; lng="${STOP_LNG[$i]:-}"
  [ -n "$lat" ] && [ -n "$lng" ] || return 0
  type api >/dev/null 2>&1 || return 0
  adb emu geo fix "$lng" "$lat" >/dev/null 2>&1 || true
  ts="$(adb shell "date +'%m-%d %H:%M:%S.000'" 2>/dev/null | tr -d '\r')"
  STOP_INDEX=$i api loc_stop >> "$OUT/run.log" 2>&1 || true
  while [ "$t" -lt 20 ]; do
    app_logcat_since "$ts" > "$OUT/diag/.prime.txt" 2>/dev/null || true
    if grep -aqE 'location-update|location ping' "$OUT/diag/.prime.txt"; then seen=1; break; fi
    sleep 4; t=$((t + 4))
  done
  if [ "$seen" = 1 ]; then
    _ulog "stop $n: GPS émulateur + ping API posés sur l'arrêt ($lat,$lng) ; ping du Driver vu après ${t}s"
  else
    _ulog "stop $n: WARN GPS émulateur + ping API posés sur l'arrêt ($lat,$lng) mais aucun ping du Driver vu en 20s [$APP_LC_STATS]"
  fi
  return 0
}

# driver_stop_confirm_ui pkg n : « Confirmer l'arrêt » n côté Driver par taps UI uniquement.
# 0 = le SERVEUR a vu l'arrêt n (stops_done>=n) à la suite de ces taps ; 2 = bouton absent ;
# 3 = état inattendu (arrêt déjà fait avant tout tap) ; 1 = taps sans effet serveur (diagnostic écrit).
# Diagnostic dbg-stopN-aK : écrit à CHAQUE tentative et sur TOUTES les sorties (échec, état inattendu,
# dump indisponible, bouton absent, succès), jamais seulement après un échec silencieux.
# Les taps visent le parent CLIQUABLE (même méthode que « Prise en charge », prouvée) ; l'ancien
# tap_exact visait le centre du libellé, et le serveur n'a reçu aucun complete-stop (run 44927908295).
# Un nouvel essai ne retape jamais si le serveur a déjà vu l'arrêt (sinon il confirmerait l'arrêt suivant).
# Aucun appel API de complétion ici : le repli API reste à l'appelant, compté en échec.
driver_stop_confirm_ui() {
  local pkg="$1" n="$2" attempt tool res x y kind done_now
  tool="$(dirname "${BASH_SOURCE[0]}")/tap_node.py"
  for attempt in 1 2 3; do
    ride_state
    done_now="$(last_stops_done)"; done_now="${done_now:-0}"
    if [ "$done_now" -ge "$n" ]; then
      if [ "$attempt" -eq 1 ]; then
        _ulog "stop $n: déjà fait côté serveur avant tout tap"
        stop_diag "stop${n}-a${attempt}-already"
        return 3
      fi
      _ulog "stop $n: vu par le serveur après le tap de l'essai $((attempt - 1))"
      stop_diag "stop${n}-a${attempt}-seen"
      return 0
    fi
    if ! ui_xml; then
      _ulog "stop $n: dump indisponible (essai $attempt)"
      stop_diag "stop${n}-a${attempt}-nodump"
      sleep 2; continue
    fi
    res=$(python3 "$tool" "$UI_XML_PATH" "Confirmer l'arrêt")
    if [ -z "$res" ]; then
      _ulog "stop $n: bouton « Confirmer l'arrêt » absent (essai $attempt)"
      stop_diag "stop${n}-a${attempt}-nobutton"
      return 2
    fi
    read -r x y kind <<<"$res"
    stop_prime "$n"
    STOP_TAP_TS="$(adb shell "date +'%m-%d %H:%M:%S.000'" 2>/dev/null | tr -d '\r')"
    adb shell input tap "$x" "$y" >/dev/null 2>&1
    _ulog "stop $n: « Confirmer l'arrêt » tapé @ $x,$y ($kind, essai $attempt)"
    # Dialogue d'arrêt réel (app_dialog.dart) : titre « Confirmer l'arrêt » + Annuler/Confirmer, jamais le
    # dialogue de fin de course (« Veuillez confirmer »). Sans lui, aucun « Confirmer » n'est tapé.
    if { ui_xml && _spec_ok '+Annuler&+Confirmer&+Confirmer l.arrêt'; } >/dev/null 2>&1; then
      _ulog "stop $n: dialogue d'arrêt reconnu (titre « Confirmer l'arrêt » + Annuler/Confirmer)"
    else
      _ulog "stop $n: WARN titre « Confirmer l'arrêt » absent du dialogue (essai $attempt)"
    fi
    if dialog_confirm "$pkg" "Confirmer" "Annuler" 8 15; then
      sleep 1
      ui_texts_log "stop${n}-a${attempt}-apres-confirmer"
    else
      _ulog "stop $n: dialogue d'arrêt non confirmé (essai $attempt)"
      stop_diag "stop${n}-a${attempt}-nodialog"
    fi
    sleep 3
    ride_state
    done_now="$(last_stops_done)"; done_now="${done_now:-0}"
    if [ "$done_now" -ge "$n" ]; then
      _ulog "stop $n: serveur stops_done=$done_now après les taps UI (essai $attempt)"
      stop_diag "stop${n}-a${attempt}-ok"
      return 0
    fi
    stop_diag "stop${n}-a${attempt}"
  done
  return 1
}

# rider_leave_rating pkg : après la notation, ramène le Rider à l'accueil SANS Back aveugle.
# Un Back sur l'accueil (racine Flutter) quitte l'app : c'était la fausse panne « rider-home-after »
# du run 44927908295, où la capture « notation » montrait déjà l'accueil. 0 = accueil (« Où allez-vous ? »)
# observé ; 1 = accueil jamais atteint. Un Back n'est envoyé que si l'accueil n'est pas déjà là.
rider_leave_rating() {
  local pkg="$1" i tool res x y kind lab
  tool="$(dirname "${BASH_SOURCE[0]}")/tap_node.py"
  for i in 1 2 3 4 5 6; do
    if ! ui_xml; then sleep 2; continue; fi
    if [ -n "$(python3 "$tool" "$UI_XML_PATH" "Où allez-vous ?")" ]; then
      _ulog "rider_leave_rating: accueil observé (essai $i), aucun Back envoyé si déjà là"
      return 0
    fi
    res=""
    for lab in "Plus tard" "Passer" "Ignorer" "Fermer"; do
      res=$(python3 "$tool" "$UI_XML_PATH" "$lab")
      [ -n "$res" ] && break
    done
    if [ -n "$res" ]; then
      read -r x y kind <<<"$res"
      adb shell input tap "$x" "$y" >/dev/null 2>&1
      _ulog "rider_leave_rating: « $lab » tapé @ $x,$y ($kind)"
    else
      adb shell input keyevent 4 >/dev/null 2>&1
      _ulog "rider_leave_rating: écran non-accueil, Back envoyé (essai $i)"
    fi
    sleep 3
  done
  return 1
}

# _screen_sig xml : signature des textes visibles d'un dump, jetons de durée/horloge masqués
# (un minuteur qui avance n'est pas un changement d'écran ; un compteur « 0/2 » → « 1/2 » en est un).
_screen_sig() {
  python3 - "$1" <<'PY'
import html, re, sys
try:
    xml = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except OSError:
    xml = ""
out = []
for m in re.finditer(r"<node\b[^>]*>", xml):
    tag = m.group(0)
    if re.search(r'visible-to-user="false"', tag):
        continue
    for name in ("text", "content-desc"):
        a = re.search(r'\b' + name + r'="([^"]*)"', tag)
        t = re.sub(r"\s+", " ", html.unescape(a.group(1))).strip().lower() if a else ""
        if t:
            t = re.sub(r"\b\d+(?=\s*(?:mins?|minutes?|h|sec|s)\b)", "#", t)
            t = re.sub(r"\b\d{1,2}:\d{2}\b", "#:#", t)
            out.append(t)
print("\n".join(out))
PY
}

# wait_screen_change ref_xml timeout : attend que l'écran courant DIFFÈRE de ref_xml (signature de textes).
# 0 = changement observé ; 1 = écran toujours identique à la référence après `timeout` s. Une capture
# prise sans attendre redonnait l'écran précédent (35b = 37 octet pour octet, run 45004400552).
wait_screen_change() {
  local ref="$1" tmo="${2:-20}" t0=$SECONDS refsig cur
  refsig="$(_screen_sig "$ref")"
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    if ui_xml; then
      cur="$(_screen_sig "$UI_XML_PATH")"
      if [ -n "$cur" ] && [ "$cur" != "$refsig" ]; then
        _ulog "wait_screen_change: écran différent de $(basename "$ref") après $((SECONDS - t0))s"
        return 0
      fi
    fi
    sleep 2
  done
  _ulog "wait_screen_change: écran toujours identique à $(basename "$ref") après ${tmo}s"
  return 1
}

# rider_dismiss_blocking_dialog pkg : ferme le dialogue « Terminez votre course » (my_strings
# runningRideAlertTitle) qui recouvre l'écran de course côté Rider (run 45004400552, captures 23/24 :
# seuls « Terminez votre course / Continuer / Dismiss » étaient visibles, donc compteur et bannière
# introuvables). Back uniquement : jamais « Continuer » (il terminerait la course). 3 = dialogue absent ;
# 0 = vu puis fermé (la présence est un constat produit à tracer par l'appelant) ; 1 = toujours là.
rider_dismiss_blocking_dialog() {
  local pkg="$1" i
  ui_xml || return 3
  grep -q 'Terminez votre course' "$UI_XML_PATH" 2>/dev/null || return 3
  _ulog "rider: dialogue « Terminez votre course » au-dessus de l'écran de course"
  for i in 1 2; do
    adb shell input keyevent 4 >/dev/null 2>&1
    sleep 2
    if ui_xml && ! grep -q 'Terminez votre course' "$UI_XML_PATH" 2>/dev/null; then
      _ulog "rider: dialogue fermé par Back (essai $i)"
      return 0
    fi
  done
  _ulog "rider: dialogue « Terminez votre course » toujours présent après 2 Back"
  return 1
}

# tap_node_verified pkg label : tape le centre du nœud (ou de son parent cliquable) trouvé par
# texte/content-desc dans le dump, puis PROUVE que l'écran a changé : le libellé a disparu ou
# un champ de saisie est apparu. Jusqu'à 2 retaps. 0 = écran changé, 1 = rien ne s'est passé.
tap_node_verified() {
  local pkg="$1" label="$2" try res x y kind
  for try in 1 2 3; do
    wait_ui "$pkg" 15 "+$(printf '%s' "$label" | python3 -c 'import re,sys;print(r"\s+".join(re.escape(w) for w in sys.stdin.read().split()))')" || true
    ui_xml || { _ulog "tap_node_verified: dump indisponible ($label, essai $try)"; continue; }
    res=$(python3 "$(dirname "${BASH_SOURCE[0]}")/tap_node.py" "$UI_XML_PATH" "$label")
    if [ -z "$res" ]; then
      _ulog "tap_node_verified: nœud « $label » absent (essai $try)"
      sleep 2
      continue
    fi
    read -r x y kind <<<"$res"
    adb shell input tap "$x" "$y" >/dev/null 2>&1
    _ulog "tap_node_verified: « $label » @ $x,$y ($kind, essai $try)"
    sleep 3
    if ! ui_xml; then continue; fi
    if python3 "$(dirname "${BASH_SOURCE[0]}")/tap_node.py" --has-input "$UI_XML_PATH"; then
      _ulog "tap_node_verified: champ de saisie visible — écran changé"
      return 0
    fi
    if [ -z "$(python3 "$(dirname "${BASH_SOURCE[0]}")/tap_node.py" "$UI_XML_PATH" "$label")" ]; then
      _ulog "tap_node_verified: « $label » a disparu — écran changé"
      return 0
    fi
    _ulog "tap_node_verified: écran inchangé après le tap (essai $try)"
  done
  return 1
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
  # name pkg spec status waited action action_ok point marker_source failed_spec [fg] [fg_source]
  # $11 = premier plan arbitré par snap (dumpsys, corrigé par un dump uiautomator) : c'est la
  # preuve que report.py doit lire. Sans $11 (appelant historique) on retombe sur le dumpsys brut.
  # $12 = provenance de cette valeur : "dumpsys" ou "ui-dump" (le dump fait foi).
  python3 - "$OUT/screens/$1.json" "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}" \
            "$OUT/screens/$1.png" "$OUT/ui/$1.xml" \
            "${11:-$(fg_pkg 2>/dev/null || echo UNKNOWN)}" "${12:-dumpsys}" <<'PY'
import hashlib, json, os, re, sys, time

(path, name, pkg, spec, status, waited, action, action_ok,
 point, source, failed, png, xml, fg, fg_source) = sys.argv[1:16]


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
    "pkg": pkg, "fg_pkg": fg, "fg_source": fg_source, "marker": spec, "marker_source": source,
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
  # Le dump qui vient de servir (marqueur trouvé, ou dernier dump du wait) est figé AVANT
  # le second dump : un re-dump raté effaçait cet écran et le sidecar sortait sans
  # libellés, sur 0 octet de XML.
  local matched_dump="$OUT/ui/.marker-$name.xml" matched_pkg=""
  rm -f "$matched_dump"
  if [ -n "$first_hash" ] && [ -s "$UI_XML_PATH" ]; then
    cp "$UI_XML_PATH" "$matched_dump" 2>/dev/null && matched_pkg=$(xml_pkg_of "$matched_dump")
  fi
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
      [ "$cur" != "$pkg" ] && [ "$(xml_pkg)" = "$pkg" ] && cur="$pkg"
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
  elif [ -s "$matched_dump" ]; then
    # Re-dump indisponible : on garde l'écran qui vient d'être capturé. Un fichier vide
    # n'est pas une preuve, c'est une perte de preuve.
    cp "$matched_dump" "$OUT/ui/$name.xml" 2>/dev/null || : > "$OUT/ui/$name.xml"
    _ulog "snap $name: re-dump indisponible, dump capturé conservé"
  else
    : > "$OUT/ui/$name.xml"
  fi
  # Premier plan AU MOMENT DES PIXELS (donc avant le tap, qui déplacerait l'écran) : c'est la
  # preuve que report.py doit lire dans le sidecar. dumpsys window renvoie régulièrement le
  # launcher alors que le dump uiautomator porte l'app — cas déjà arbitré par S8 ci-dessus et
  # documenté dans xml_pkg (ui.sh:43-47, « marqueur absent sur 29 points alors que le XML
  # montrait l'app »). Ordre de preuve : dumpsys, puis le dump post-capture, puis — seulement
  # s'il n'y a pas de dump frais — le dump qui a servi à la capture. Le dump d'après-capture
  # fait foi sur l'écran d'après-capture : il ne peut pas être remplacé par celui d'avant.
  local cap_fg_raw cap_fg cap_fg_src
  cap_fg_raw=$(fg_pkg 2>/dev/null || echo UNKNOWN)
  cap_fg="$cap_fg_raw"
  cap_fg_src="dumpsys"
  if [ -n "$pkg" ] && [ "$cap_fg" != "$pkg" ]; then
    if [ "$(xml_pkg)" = "$pkg" ]; then
      cap_fg="$pkg"; cap_fg_src="ui-dump"
    elif [ "${UI_DUMP_VALID:-0}" != "1" ] && [ -n "$matched_pkg" ] && [ "$matched_pkg" = "$pkg" ]; then
      cap_fg="$pkg"; cap_fg_src="ui-dump"
    fi
  fi
  rm -f "$matched_dump"
  local action_ok=0 failed=""
  [ -f "$OUT/ui/.last-spec-fail" ] && failed=$(cat "$OUT/ui/.last-spec-fail")
  if [ -n "$action" ] && [ "$st" = MATCH ]; then
    if tap_exact "$action"; then action_ok=1; sleep 2; else st=ACTION_MISS; fi
  fi
  _write_sidecar "$name" "$pkg" "$spec" "$st" "$((SECONDS - t0))" "$action" "$action_ok" \
                 "$point" "$source" "$failed" "$cap_fg" "$cap_fg_src"
  echo "$name|$st|fg=$cap_fg|point=$point|marker=$spec|wait=$((SECONDS - t0))s$([ "$cap_fg" != "$cap_fg_raw" ] && echo " (dumpsys=$cap_fg_raw, corrigé par $cap_fg_src)")" \
    >> "$OUT/captures.txt"
  _ulog "snap $name $st ($((SECONDS - t0))s)"
  [ "$st" = TIMEOUT ] && ui_screen_diag "snap $name TIMEOUT : écran réel"
  case "$st" in
    MATCH) return 0 ;;
    DUMP_UNAVAILABLE) return 4 ;;
    ACTION_MISS|CAPTURE_MISS) return 5 ;;
    *) return 3 ;;
  esac
}
