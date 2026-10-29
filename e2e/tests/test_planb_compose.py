#!/usr/bin/env python3
"""TDD spec du plan B rider (run 40833325705) — `python3 e2e/tests/test_planb_compose.py`.

Run 40833325705 : Artemis a échoué, le harnais a écrit « étape reprise par taps + API »
alors qu'AUCUN tap ni AUCUNE saisie n'a eu lieu. La destination n'a jamais été saisie et
tout le parcours a échoué en chaîne. Ces tests épinglent :
  * `planb_pick_place` fait de VRAIS taps et une VRAIE saisie (`input text`), puis choisit
    un résultat et confirme ; sans résultat, il échoue SANS confirmer ;
  * contrat : aucun `mark` de run.sh n'annonce une reprise (« reprise », « plan B ») sans
    qu'un appel d'action UI (`planb_pick_place`, `tap_*`, `try_*`) le précède dans son bloc ;
  * destination, stop1 et stop2 appellent tous trois `planb_pick_place` ;
  * Artemis hors chemin critique : le smoke est un seul essai court, WARN non bloquant, et
    les étapes rider n'appellent Artemis que si ARTEMIS_CRITICAL=1 ;
  * lecture écran : un appareil `offline` est reconnecté et la cause est journalisée ;
    un tap dont le dump est indisponible se rabat sur la dernière coordonnée connue.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
UI = os.path.join(ROOT, "ui.sh")
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


PICKER = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" class="android.widget.FrameLayout" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Où allez-vous ?" content-desc="" class="android.view.View" visible-to-user="true" bounds="[100,200][900,300]" />
    <node index="2" text="" content-desc="" class="android.widget.EditText" visible-to-user="true" bounds="[100,400][900,500]" />
    <node index="3" text="" content-desc="Point E, Dakar, Sénégal" class="android.view.View" visible-to-user="true" bounds="[100,600][900,700]" />
    <node index="4" text="" content-desc="Confirmer le lieu" class="android.widget.Button" visible-to-user="true" bounds="[100,2200][900,2300]" />
  </node>
</hierarchy>
"""
NO_RESULT = re.sub(r'.*Point E, Dakar.*\n', '', PICKER)


def stub_env(fixture, adb_body=""):
    tmp = tempfile.mkdtemp(prefix="e2e-planb-")
    xml = os.path.join(tmp, "w.xml")
    open(xml, "w", encoding="utf-8").write(fixture)
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir)
    adb_log = os.path.join(tmp, "adb.log")
    stub = os.path.join(bin_dir, "adb")
    open(stub, "w").write('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$ADB_LOG"\n' + adb_body)
    os.chmod(stub, 0o755)
    env = dict(os.environ, OUT=tmp, UI_XML=xml, UI_XML_SRC=xml, E2E_ALLOW_FIXTURE="1",
               SMOKE_LOG=os.path.join(tmp, "smoke.log"), PLANB_SETTLE="0",
               PATH=bin_dir + os.pathsep + os.environ["PATH"], ADB_LOG=adb_log)
    env.pop("GITHUB_ACTIONS", None)
    return tmp, adb_log, env


def run_ui(script, env):
    return subprocess.run(["bash", "-c", script, "_", UI], capture_output=True, text=True,
                          env=env, timeout=120)


def read(p):
    try:
        return open(p, encoding="utf-8").read()
    except OSError:
        return ""


@case
def test_planb_really_taps_types_selects_and_confirms():
    tmp, adb_log, env = stub_env(PICKER)
    proc = run_ui('. "$1"; planb_pick_place sn.galass.galassrider "Où allez-vous ?" '
                  '"Point E Dakar" "Point E"', env)
    assert proc.returncode == 0, (proc.returncode, proc.stderr, read(os.path.join(tmp, "run.log")))
    calls = read(adb_log).splitlines()
    taps = [c for c in calls if c.startswith("shell input tap")]
    assert "shell input text Point%sE%sDakar" in calls, calls
    assert "shell input tap 500 250" in taps, taps   # « Où allez-vous ? »
    assert "shell input tap 500 450" in taps, taps   # champ de saisie
    assert "shell input tap 500 650" in taps, taps   # résultat
    assert "shell input tap 500 2250" in taps, taps  # Confirmer le lieu
    order = [taps.index(t) for t in ("shell input tap 500 250", "shell input tap 500 450",
                                     "shell input tap 500 650", "shell input tap 500 2250")]
    assert order == sorted(order), taps
    assert "planb: OK" in read(os.path.join(tmp, "run.log"))


@case
def test_planb_without_result_fails_and_never_confirms():
    tmp, adb_log, env = stub_env(NO_RESULT)
    proc = run_ui('export PLANB_RESULT_TMO=2; . "$1"; planb_pick_place sn.galass.galassrider '
                  '"Où allez-vous ?" "Point E Dakar" "Point E"', env)
    assert proc.returncode != 0, proc.returncode
    assert "shell input tap 500 2250" not in read(adb_log), read(adb_log)
    assert "planb: aucun résultat" in read(os.path.join(tmp, "run.log"))


def blocks_with_marks():
    lines = read(RUN).splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*mark\s", line) and re.search(r"reprise|plan B|planb", line, re.I):
            yield i, line, lines[max(0, i - 12):i]


@case
def test_contract_no_skip_announces_a_recovery_without_action():
    seen = 0
    for i, line, before in blocks_with_marks():
        seen += 1
        assert any(re.search(r"\b(planb_pick_place|tap_exact|tap_when|try_tap|try_confirm|ui_action)\b", b)
                   for b in before), "ligne %d annonce une reprise sans action: %s" % (i + 1, line)
    assert seen >= 2, "les étapes rider doivent journaliser leur plan B (%d)" % seen
    assert "étape reprise par taps + API" not in read(RUN), "libellé SKIP mensonger encore présent"


@case
def test_destination_and_both_stops_use_planb():
    text = read(RUN)
    for step in ("rider-destination", "rider-stop1", "rider-stop2"):
        assert re.search(r'planb_step\s+%s\b' % step, text), step


@case
def test_rider_steps_call_artemis_only_when_critical():
    text = read(RUN)
    for m in re.finditer(r"^\s*artemis_run rider-", text, re.M):
        head = text[max(0, m.start() - 300):m.start()]
        assert 'ARTEMIS_CRITICAL' in head, text[m.start():m.start() + 60]
    assert re.search(r'ARTEMIS_CRITICAL="\$\{ARTEMIS_CRITICAL:-0\}"', text)


@case
def test_smoke_is_one_short_attempt_and_warn():
    text = read(RUN)
    m = re.search(r'ARTEMIS_MAX_ATTEMPTS=1 artemis_run smoke "[^"]*" "\$ARTEMIS_SMOKE_TMO"', text)
    assert m, "smoke: un seul essai court attendu"
    assert re.search(r'ARTEMIS_SMOKE_TMO="\$\{ARTEMIS_SMOKE_TMO:-[0-9]+\}"', text)
    assert 'mark artemis-smoke WARN' in text and 'mark artemis-smoke FAIL' not in text


@case
def test_ui_xml_reconnects_offline_device_and_logs_cause():
    body = ('case "$*" in\n'
            '  "get-state") echo offline ;;\n'
            '  *"uiautomator dump"*) echo "error: device offline" >&2 ;;\n'
            'esac\n')
    tmp, adb_log, env = stub_env(PICKER, body)
    env.pop("UI_XML_SRC")
    env["UI_DUMP_RECONNECT_TMO"] = "1"
    proc = run_ui('. "$1"; ui_xml; echo rc=$?', env)
    assert "rc=4" in proc.stdout, proc.stdout
    calls = read(adb_log)
    assert "reconnect offline" in calls, calls
    log = read(os.path.join(tmp, "run.log"))
    assert "device offline" in log and "state=offline" in log, log


@case
def test_tap_exact_falls_back_to_last_known_coordinates():
    tmp, adb_log, env = stub_env(PICKER)
    proc = run_ui('. "$1"; tap_exact "Confirmer le lieu"; UI_XML_SRC=""; '
                  'ui_xml() { return 4; }; : > "$ADB_LOG"; tap_exact "Confirmer le lieu"; echo rc=$?', env)
    assert "rc=0" in proc.stdout, (proc.stdout, proc.stderr)
    assert "shell input tap 500 2250" in read(adb_log)
    assert "coordonnées mémorisées" in read(os.path.join(tmp, "run.log"))


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("ok   " + fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %r" % (fn.__name__, exc))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
