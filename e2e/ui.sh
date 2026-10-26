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
    _ulog "ui_xml: DUMP_UNAVAILABLE après 3 essais"
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

# tap_exact label : noeud VISIBLE dont text ou content-desc, espaces normalisés,
# vaut EXACTEMENT le label, puis tap au centre. « Accepter la\ncourse » == « Accepter la course ».
tap_exact() {
  local label="$1" res
  if ! ui_xml; then
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
  _ulog "tap_exact $1 @ $2,$3"
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
