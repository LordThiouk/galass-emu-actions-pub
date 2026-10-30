#!/usr/bin/env python3
"""TDD spec robustesse d'écran + artefact diag (run 40971308162).

Run 40971308162 (1c0e88d, 21 fails) : destination_input introuvable, formulaire
d'adresse rendu dans la MOITIÉ BASSE de l'écran, carte « Voiture » absente. Ce
fichier épingle :
  * `dismiss_permission_dialog` ACCORDE un dialogue de permission (position) —
    un refus laisserait l'app dégradée pour tout le reste du run ;
  * `ui_settle` prépare la recherche (overlay fermé, permission accordée, écran
    STABLE = deux dumps consécutifs de même hash) et ne relance JAMAIS l'app ;
  * `diag_capture` écrit XML SANITISÉ + capture dans $OUT/diag — AUCUN secret
    (email, jeton, téléphone) dans l'artefact NON CHIFFRÉ du dépôt public ;
  * `planb_pick_place` retrouve destination_input après deux dumps sans le champ
    (formulaire décalé) : reparse frais + défile, puis tap ; sinon échec étape 2
    JOURNALISÉ avec preuve noinput ;
  * run.sh : diag_capture aux étapes destination ET service, relecture fraîche de
    l'index carte Voiture ; workflow : artefact staging-e2e-diagnostics non chiffré
    précédé d'une garde anti-fuite mécanique (dépôt public).

Lancer avec `python3 e2e/tests/test_diag_capture.py`. Pas d'émulateur : dumps =
fixture UI_XML_SRC ou adb À ÉTAT (compteur : dumps 1-2 = formulaire décalé SANS
destination_input, dumps suivants = le champ est revenu).
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
UI = os.path.join(ROOT, "ui.sh")
WORKFLOW = os.path.join(ROOT, "..", ".github", "workflows", "artemis-staging-e2e.yml")
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_text():
    return open(RUN, encoding="utf-8").read()


def read(p):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


# Écran picker destination DÉCALÉ (constat run 40971308162) : PAS de destination_input,
# formulaire dans la moitié basse (Lieu de prise en charge y=1599, Confirmer y=2152).
PICKER_NOINPUT = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" class="android.widget.FrameLayout" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Où allez-vous ?" content-desc="" class="android.view.View" visible-to-user="true" bounds="[100,200][900,300]" />
    <node index="2" text="Lieu de prise en charge" content-desc="" class="android.view.View" visible-to-user="true" bounds="[42,1599][900,1699]" />
    <node index="3" text="" content-desc="Confirmer le lieu" class="android.widget.Button" visible-to-user="true" bounds="[46,2152][900,2252]" />
  </node>
</hierarchy>
"""
# Champ revenu (formulaire haut de page) : destination_input + suggestion Places + bouton.
PICKER_INPUT = re.sub(
    r"\n.*Lieu de prise en charge.*\n",
    '\n    <node index="2" text="" content-desc="" class="android.widget.EditText" '
    'resource-id="destination_input" visible-to-user="true" bounds="[100,400][900,500]" />\n'
    '    <node index="4" text="" content-desc="Point E, Dakar, Sénégal" '
    'resource-id="place_prediction_0" visible-to-user="true" bounds="[100,600][900,700]" />\n'
    '    <node index="5" text="" content-desc="Confirmer le lieu" class="android.widget.Button" '
    'resource-id="confirm_location" visible-to-user="true" bounds="[100,2200][900,2300]" />\n',
    PICKER_NOINPUT)
assert "destination_input" in PICKER_INPUT and "Lieu de prise en charge" not in PICKER_INPUT

PERM = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" class="android.widget.FrameLayout" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Autoriser Galass Rider à accéder à la position de cet appareil ?" content-desc="" class="android.view.View" visible-to-user="true" bounds="[100,800][980,1000]" />
    <node index="2" text="Pendant l'utilisation de l&#39;appli" content-desc="" class="android.widget.Button" clickable="true" visible-to-user="true" bounds="[100,1900][980,2050]" />
    <node index="3" text="Ne pas autoriser" content-desc="" class="android.widget.Button" clickable="true" visible-to-user="true" bounds="[100,2100][980,2250]" />
  </node>
</hierarchy>
"""

DIAG_FIXTURE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" class="android.widget.FrameLayout" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Où allez-vous ?" content-desc="" class="android.view.View" visible-to-user="true" bounds="[100,200][900,300]" />
    <node index="2" text="support+ops@galass-test.sn" content-desc="" class="android.view.View" visible-to-user="true" bounds="[100,400][900,500]" />
    <node index="3" text="contact1@hotmail.com digit AABBCCDDEEFF00112233445566778899zz" content-desc="Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig" class="android.view.View" visible-to-user="true" bounds="[100,700][900,800]" />
    <node index="4" text="221 77 123 45 67" content-desc="" class="android.widget.EditText" visible-to-user="true" bounds="[100,1000][900,1100]" />
  </node>
</hierarchy>
"""


def shq(s):
    """Quote shell d'un chemin (apostrophes comprises)."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def stub_env(fixture=None, force_mode=None):
    """stub adb sur PATH.

    fixture: XML servi via UI_XML_SRC (le dump ne passe pas par adb).
    None: adb À ÉTAT — dumps 1-2 servent d1 (PICKER_NOINPUT), les suivants d2
    (PICKER_INPUT : simule un écran intermédiaire qui se referme).
    force_mode 'noinput' | 'input' épingler TOUS les dumps sur d1|d2.
    """
    tmp = tempfile.mkdtemp(prefix="e2e-diag-")
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir)
    os.makedirs(os.path.join(tmp, "ui"))
    adb_log = os.path.join(tmp, "adb.log")
    d1 = os.path.join(tmp, "d1.xml")  # PICKER_NOINPUT : formulaire absent
    d2 = os.path.join(tmp, "d2.xml")  # PICKER_INPUT : formulaire visible
    open(d1, "w", encoding="utf-8").write(PICKER_NOINPUT)
    open(d2, "w", encoding="utf-8").write(PICKER_INPUT)
    if fixture is not None:
        xml = os.path.join(tmp, "w.xml")
        open(xml, "w", encoding="utf-8").write(fixture)
        body = 'case "$*" in "exec-out screencap -p") printf PNGDATA ;; *) : ;; esac\n'
        env = dict(os.environ, OUT=tmp, SMOKE_LOG=os.path.join(tmp, "smoke.log"),
                   UI_XML=xml, UI_XML_SRC=xml, E2E_ALLOW_FIXTURE="1")
    else:
        # Le stub écrit l'HÔTE : /sdcard/w.xml et /data/local/tmp/w.xml sont des
        # chemins d'APPAREIL — sur l'hôte le contenu va dans $EMU. Le champ
        # destination_input n'existe JAMAIS tant que le swipe de défilement
        # n'a pas eu lieu (motif épinglé) — sauf force_mode.
        swipe_flags = os.path.join(tmp, ".swipe")
        first = d1 if force_mode in (None, "noinput") else d2
        after = d1 if force_mode == "noinput" else d2
        body = (
            'case "$*" in\n'
            '  "get-state") echo device ;;\n'
            '  "shell pkill -f uiautomator"|"shell rm -f /sdcard/w.xml"|"shell rm -f /data/local/tmp/w.xml") rm -f "$EMU" ;;\n'
            '  "shell input swipe 540 1500 540 700 400") touch "$SWIPE" ;;\n'
            '  "shell uiautomator dump --compressed /sdcard/w.xml"|"shell uiautomator dump --compressed /data/local/tmp/w.xml")\n'
            '    f=%s; [ -e "$SWIPE" ] && f=%s\n'
            '    cp "$f" "$EMU" 2>/dev/null; echo "UI hierarchie dumped to /sdcard/w.xml" ;;\n'
            '  "shell cat /sdcard/w.xml"|"shell cat /data/local/tmp/w.xml") cat "$EMU" ;;\n'
            '  "exec-out screencap -p") printf PNGDATA ;;\n'
            '  *) : ;;\n'
            'esac\n'
        ) % (shq(first), shq(after))
        env = dict(os.environ, OUT=tmp, SMOKE_LOG=os.path.join(tmp, "smoke.log"),
                   UI_XML=os.path.join(tmp, "w.xml"),
                   EMU=os.path.join(tmp, "emu.xml"), SWIPE=swipe_flags)
    stub = os.path.join(bin_dir, "adb")
    header = "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> " + shq(adb_log) + "\n"
    open(stub, "w").write(header + body)
    os.chmod(stub, 0o755)
    env.update(SMOKE_LOG=os.path.join(tmp, "smoke.log"), PLANB_SETTLE="0", PLANB_RESULT_TMO="6")
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", ".")
    env.pop("GITHUB_ACTIONS", None)
    return tmp, adb_log, env


def run_ui(script, env):
    return subprocess.run(["bash", "-c", script, "_", UI], capture_output=True, text=True,
                          env=env, timeout=240)


# --------------------------------------------------------------- permission
@case
def test_permission_granted_never_denied():
    tmp, adb_log, env = stub_env(PERM)
    proc = run_ui('. "$1"; dismiss_permission_dialog; echo rc=$?', env)
    assert "rc=0" in proc.stdout, (proc.stdout, proc.stderr, read(os.path.join(tmp, "run.log")))
    taps = [c for c in read(adb_log).splitlines() if c.startswith("shell input tap")]
    assert "shell input tap 540 1975" in taps, taps          # « Pendant l'utilisation de l'appli »
    assert "shell input tap 540 2175" not in taps, taps       # « Ne pas autoriser » JAMAIS tapé
    assert "permission accordée" in read(os.path.join(tmp, "run.log"))


@case
def test_permission_absent_is_silent_and_never_taps():
    tmp, adb_log, env = stub_env(PICKER_NOINPUT)
    proc = run_ui('. "$1"; dismiss_permission_dialog; echo rc=$?', env)
    assert "rc=1" in proc.stdout, proc.stdout
    assert not [c for c in read(adb_log).splitlines() if c.startswith("shell input tap")]


# ------------------------------------------------------------------ ui_settle
@case
def test_ui_settle_stable_and_never_relaunches():
    tmp, adb_log, env = stub_env(None)
    proc = run_ui('. "$1"; ui_settle; echo rc=$?', env)
    assert "rc=0" in proc.stdout, (proc.stdout, proc.stderr, read(os.path.join(tmp, "run.log")))
    calls = read(adb_log)
    assert "monkey" not in calls and "am start" not in calls, calls  # AUCUNE relance
    assert "jamais stable" not in read(os.path.join(tmp, "run.log"))


# ---------------------------------------------------------------- diag_capture
@case
def test_diag_capture_writes_sanitized_dump_no_secret_leaves():
    tmp, adb_log, env = stub_env(DIAG_FIXTURE)
    proc = run_ui('. "$1"; diag_capture destination; echo rc=$?', env)
    assert "rc=0" in proc.stdout, (proc.stdout, proc.stderr, read(os.path.join(tmp, "run.log")))
    d = os.path.join(tmp, "diag")
    base = os.path.join(d, "diag-destination")
    assert os.path.exists(base + ".png") and os.path.getsize(base + ".png") > 0, sorted(os.listdir(d))
    xml = read(base + ".xml")
    assert "@galass-test.sn" not in xml and "[EMAIL]" in xml, xml
    assert "AABBCCDDEE" not in xml and "[REDACTED]" in xml, xml   # jeton/base64/token
    assert "eyJhbGciOiJIUzI1NiJ9" not in xml, xml
    assert "221 77 123 45 67" not in xml and "[PHONE]" in xml, xml
    assert "Où allez-vous ?" in xml, xml                          # contenu utile conservé
    assert "diag: diag-destination" in read(os.path.join(tmp, "run.log"))


# ----------------------------------------- planb robustesse destination_input
@case
def test_planb_finds_input_after_interstitial_and_reparse():
    """2 dumps SANS destination_input (formulaire décalé) puis champ revenu -> tap."""
    tmp, adb_log, env = stub_env(None)
    proc = run_ui('. "$1"; planb_pick_place sn.galass.galassrider "Où allez-vous ?" '
                  '"Point E Dakar" "Point E" "" "destination_input"', env)
    log = read(os.path.join(tmp, "run.log"))
    assert proc.returncode == 0, (proc.returncode, proc.stderr, log)
    calls = read(adb_log)
    taps = [c for c in calls.splitlines() if c.startswith("shell input tap")]
    assert "shell input tap 500 450" in taps, taps            # destination_input
    assert "shell input text Point%sE%sDakar" in calls.splitlines(), calls
    assert "shell input tap 500 650" in taps, taps            # place_prediction_0
    assert "shell input tap 500 2250" in taps, taps           # Confirmer le lieu
    assert calls.count("shell input swipe 540 1500 540 700 400") == 1, calls
    assert "défile et reparse" in log, log


@case
def test_planb_missing_input_fails_step2_with_evidence():
    """Champ jamais résolu -> échec étape 2 JOURNALISÉ + preuve noinput, pas de crash."""
    tmp, adb_log, env = stub_env(None, force_mode="noinput")
    env["PLANB_INPUT_TMO"] = "6"  # boucle bornée pour le test
    proc = run_ui('. "$1"; planb_pick_place sn.galass.galassrider "Où allez-vous ?" '
                  '"Point E Dakar" "Point E" "" "destination_input"', env)
    log = read(os.path.join(tmp, "run.log"))
    assert "échec étape 2" in log and "destination_input introuvable" in log, \
        (proc.stdout, proc.stderr, log)
    assert "noinput" in log, log


# ------------------------------------------------------------------- wiring run.sh
@case
def test_run_wires_diag_destination_after_step():
    text = run_text()
    i = text.index("planb_step rider-destination")
    seg = text[i:i + 900]
    assert "diag_capture destination" in seg, seg
    j = text.index("diag_capture destination")
    assert text.index("cap rider-destination-selected") > j  # diag AVANT le cap


@case
def test_run_wireard_request_ride_has_diag_service_and_fresh_reparse():
    text = run_text()
    body = text[text.index("rider_request_ride() {"):]
    body = body[:body.index("\nif [ \"${RIDE_SOURCE_FORCE_API")]
    assert "diag_capture service" in body, body
    assert "for si in 1 2 3 4" in body, body                    # essais reparse
    assert "ui_settle" in body, body                             # écran stable avant tap
    assert "swipe 540 1700 540 900" in body, body                # défile si carte absente


@case
def test_service_card_index_is_fresh_function():
    text = run_text()
    start = text.index("rider_service_card_idx() {")
    end = text.index("PYIDX", start)                 # ouverture <<'PYIDX'
    end = text.index("PYIDX", end + len("PYIDX"))    # marqueur de fermeture
    seg = text[start:end + len("PYIDX") + 2]
    assert seg.count("PYIDX") == 2 and 're.match(r"Voiture\\b(?!s)"' in seg, seg[:400]
    assert "ui_xml || return 4" in seg, seg[:200]    # dump FRAIS à chaque appel


# ------------------------------------------------------------------- wiring workflow
@case
def test_workflow_uploads_diag_unencrypted_with_guard():
    text = open(WORKFLOW, encoding="utf-8").read()
    m = re.search(r"with: \{name: staging-e2e-diagnostics, path: e2e-out/diag, "
                  r"retention-days: 7, if-no-files-found: warn\}", text)
    assert m, "upload NON CHIFFRÉ de e2e-out/diag attendu"
    guard = text[text.index("anti-fuite"):]
    guard = guard[:guard.index("Diagnostics écran")]
    assert "221[0-9]" in guard and "exit 0" in guard, guard      # emails+jetons+221
    assert "evidence.tgz.gpg" in text                            # preuves complètes: toujours chiffrées
    assert "path: evidence.tgz.gpg" in text
    diag_upload = text[text.index("Diagnostics écran"):]
    assert "gpg" not in diag_upload[:diag_upload.find("name: staging-e2e-diagnostics")], \
        "l'artefact diag ne doit PAS passer par le chiffrement"


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
