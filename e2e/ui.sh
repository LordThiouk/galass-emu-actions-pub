# UI helpers: captures déclenchées par l'état réel de l'écran (uiautomator), pas par délai fixe.
RIDER_PKG="${RIDER_PKG:-sn.galass.galassrider}"
DRIVER_PKG="${DRIVER_PKG:-sn.galass.galassdriver}"
OUT="${OUT:-e2e-out}"
mkdir -p "$OUT/screens" "$OUT/ui"
_ulog() { printf '[ui %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT/run.log" >&2; }
fg_pkg() { adb shell dumpsys activity activities 2>/dev/null | grep -m1 -E "topResumedActivity|mResumedActivity" | grep -oE "sn\.galass\.[a-z]+" ; }
ui_xml() { adb shell uiautomator dump /sdcard/w.xml >/dev/null 2>&1; adb shell cat /sdcard/w.xml 2>/dev/null; }
ensure_fg() { [ "$(fg_pkg)" = "$1" ] || { adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; }; }
# ferme "X isn't responding" (tap Wait) si présent dans /tmp/w.xml
clear_sys() {
  grep -q "isn't responding" /tmp/w.xml 2>/dev/null || return 0
  tap_exact "Wait" >/dev/null 2>&1 || tap_exact "Close app" >/dev/null 2>&1
  _ulog "dialogue système fermé"; sleep 2; ui_xml > /tmp/w.xml
}
# wait_ui pkg regex timeout -> 0 si le texte apparaît (app au premier plan)
wait_ui() {
  local pkg="$1" re="$2" tmo="${3:-30}" t0=$SECONDS
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    [ -n "$pkg" ] && ensure_fg "$pkg"
    ui_xml > /tmp/w.xml; clear_sys
    if grep -qE "$re" /tmp/w.xml && { [ -z "$pkg" ] || [ "$(fg_pkg)" = "$pkg" ]; }; then return 0; fi
    sleep 1
  done
  return 1
}
# wait_gone pkg regex timeout
wait_gone() {
  local pkg="$1" re="$2" tmo="${3:-30}" t0=$SECONDS
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    ensure_fg "$pkg"; ui_xml > /tmp/w.xml
    [ -s /tmp/w.xml ] && ! grep -qiE "$re" /tmp/w.xml && return 0
    sleep 1
  done
  return 1
}
# snap name pkg regex timeout : attend l'état puis capture + dump; trace dans captures.txt
snap() {
  local name="$1" pkg="$2" re="$3" tmo="${4:-30}" st=MATCH t0=$SECONDS
  wait_ui "$pkg" "$re" "$tmo" || st=TIMEOUT
  adb exec-out screencap -p > "$OUT/screens/$name.png" 2>/dev/null
  cp /tmp/w.xml "$OUT/ui/$name.xml" 2>/dev/null || true
  echo "$name|$st|fg=$(fg_pkg)|re=$re|wait=$((SECONDS - t0))s" >> "$OUT/captures.txt"
  _ulog "snap $name $st ($((SECONDS - t0))s)"
  [ "$st" = MATCH ]
}
# tap_ui regex : tape le centre du premier noeud dont text/content-desc matche
tap_ui() {
  local re="$1" b x1 y1 x2 y2
  ui_xml > /tmp/w.xml
  b=$(tr '>' '\n' < /tmp/w.xml | grep -iE "(text|content-desc)=\"[^\"]*($re)" | grep -oE 'bounds="\[[0-9]+,[0-9]+\]\[[0-9]+,[0-9]+\]"' | head -1)
  [ -z "$b" ] && { _ulog "tap_ui miss: $re"; return 1; }
  read -r x1 y1 x2 y2 <<<"$(echo "$b" | tr -c '0-9' ' ')"
  adb shell input tap $(( (x1 + x2) / 2 )) $(( (y1 + y2) / 2 )) >/dev/null 2>&1
  _ulog "tap_ui $re @ $(( (x1 + x2) / 2 )),$(( (y1 + y2) / 2 ))"
}
# tap_exact label : noeud dont text/content-desc contient label comme LIGNE entière
# (Flutter fusionne des boutons empilés dans un seul noeud "A&#10;B": on tape la bonne tranche)
tap_exact() {
  [ -s /tmp/w.xml ] || ui_xml > /tmp/w.xml
  local res
  res=$(python3 - "$1" <<'PY'
import re, sys, html
lab = sys.argv[1]
xml = open("/tmp/w.xml", encoding="utf-8", errors="replace").read()
best = None
for m in re.finditer(r'<node [^>]*>', xml):
    n = m.group(0)
    for attr in ("text", "content-desc"):
        a = re.search(attr + r'="([^"]*)"', n)
        if not a: continue
        lines = html.unescape(a.group(1)).split("\n")
        if lab in lines:
            bb = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
            if not bb: continue
            x1, y1, x2, y2 = map(int, bb.groups())
            k, cnt = lines.index(lab), len(lines)
            # boutons empilés en bas de la zone (feuille du bas Flutter): chaque bouton ~ 140 px, le dernier au fond
            step = 150
            best = ((x1 + x2) // 2, y2 - 130 - (cnt - 1 - k) * 200 if cnt > 1 or (y2 - y1) > 400 else (y1 + y2) // 2, k + 1, cnt)
print("" if best is None else "%d %d %d/%d" % best)
PY
)
  [ -z "$res" ] && { _ulog "tap_exact miss: $1"; return 1; }
  set -- "$1" $res
  adb shell input tap "$2" "$3" >/dev/null 2>&1
  _ulog "tap_exact $1 @ $2,$3 (ligne $4)"
}
# ui_action pkg bouton : attend le bouton visible, le tape, puis tape "Confirmer" du dialogue
ui_action() {
  local pkg="$1" lab="$2"
  wait_ui "$pkg" "(text|content-desc)=\"([^\"]*&#10;)?$lab(&#10;[^\"]*)?\"" 20 || return 1
  tap_exact "$lab" || return 1
  sleep 2
  local tag; tag=$(echo "$lab" | tr -c 'a-zA-Z' '_')
  if wait_ui "$pkg" '(text|content-desc)="([^"]*&#10;)?Confirmer(&#10;[^"]*)?"' 8; then tap_exact "Confirmer"
  else adb exec-out screencap -p > "$OUT/screens/dbg-after-$tag.png"; cp /tmp/w.xml "$OUT/ui/dbg-after-$tag.xml"; _ulog "pas de dialogue Confirmer après $lab"; fi
  sleep 3
}
