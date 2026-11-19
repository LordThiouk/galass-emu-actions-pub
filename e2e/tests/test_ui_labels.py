#!/usr/bin/env python3
"""Tests : fenêtre de prise en charge (dialog_confirm) et diagnostic du champ Places.
Sans émulateur : `adb` est un faux script sur PATH qui rejoue des dumps uiautomator."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import dump_diag  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def node(i, text, bounds, cls="android.widget.TextView", clickable="false"):
    return ('<node index="%d" text="%s" class="%s" clickable="%s" visible-to-user="true" '
            'bounds="%s" />' % (i, text, cls, clickable, bounds))


def dump(*nodes):
    return ("<?xml version='1.0'?><hierarchy>" + "".join(nodes) + "</hierarchy>")


DIALOG = dump(node(0, "Prise en charge du client", "[100,800][900,900]"),
              node(1, "Annuler", "[100,1000][400,1100]", clickable="true"),
              node(2, "Confirmer", "[500,1000][900,1100]", clickable="true"))
AFTER = dump(node(0, "Terminer l'arrêt", "[100,1800][900,1900]", clickable="true"))


def run_dialog(frames, appear=6, gone=8):
    """frames : dumps servis dans l'ordre (le dernier se répète) ; retourne (rc, taps)."""
    tmp = tempfile.mkdtemp(prefix="e2e-dlg-")
    for i, f in enumerate(frames):
        open(os.path.join(tmp, "f%d.xml" % i), "w").write(f)
    taps = os.path.join(tmp, "taps")
    open(taps, "w").close()
    fake = os.path.join(tmp, "bin")
    os.mkdir(fake)
    with open(os.path.join(fake, "adb"), "w") as fh:
        fh.write('#!/bin/bash\necho "$@" >> %s\n' % taps)
    os.chmod(os.path.join(fake, "adb"), 0o755)
    script = r'''
set -u
export OUT="%(tmp)s"; export PATH="%(fake)s:$PATH"
. "%(ui)s"
n=0
ui_xml() { local f="%(tmp)s/f$n.xml"; [ -f "$f" ] || f="%(tmp)s/f%(last)d.xml"; n=$((n+1)); UI_XML_PATH="$f"; return 0; }
dialog_confirm pkg Confirmer Annuler %(appear)d %(gone)d
echo "rc=$?"
''' % dict(tmp=tmp, fake=fake, ui=os.path.join(ROOT, "ui.sh"), last=len(frames) - 1,
           appear=appear, gone=gone)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    rc = int([l for l in p.stdout.splitlines() if l.startswith("rc=")][-1][3:])
    return rc, open(taps).read(), p.stdout + p.stderr


@case
def test_dialog_confirm_taps_confirmer_then_waits_for_it_to_vanish():
    rc, taps, out = run_dialog([DIALOG, DIALOG, AFTER])
    assert rc == 0, out
    assert "input tap 700 1050" in taps, taps      # centre de « Confirmer », pas « Annuler »
    assert "input tap 250 1050" not in taps, taps


@case
def test_dialog_confirm_reports_stuck_dialog():
    rc, taps, out = run_dialog([DIALOG], appear=4, gone=6)
    assert rc == 1, out


@case
def test_dialog_confirm_returns_2_when_no_dialog():
    rc, taps, out = run_dialog([AFTER], appear=3, gone=4)
    assert rc == 2, out
    assert taps.strip() == "", taps


@case
def test_dialog_confirm_ignores_lone_confirmer_without_annuler():
    only = dump(node(0, "Confirmer", "[500,1000][900,1100]", clickable="true"))
    rc, taps, out = run_dialog([only], appear=3, gone=4)
    assert rc == 2, out


@case
def test_dump_diag_reports_field_text_rows_and_nodes():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "d.xml")
    open(p, "w").write(dump(
        node(0, "E2E Stop 1 Médina", "[50,200][900,300]", cls="android.widget.EditText"),
        node(1, "Médina, Dakar", "[50,400][900,500]", clickable="true"),
        node(2, "Fann, Dakar", "[50,520][900,620]", clickable="true"),
        node(3, "Où allez-vous ?", "[50,100][900,180]")))
    d = dump_diag.describe(p)
    assert [f["text"] for f in d["fields"]] == ["E2E Stop 1 Médina"], d
    assert d["rows"] == 2, d
    assert "Médina, Dakar" in d["texts"], d


@case
def test_dump_diag_no_results_and_unreadable():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "d.xml")
    open(p, "w").write(dump(node(0, "", "[50,200][900,300]", cls="android.widget.EditText")))
    assert dump_diag.describe(p)["rows"] == 0
    open(p, "w").write("<not xml")
    assert dump_diag.describe(p)["readable"] is False


def fnode(i, text, bounds, cls="android.widget.EditText", focused="false", clickable="false"):
    return ('<node index="%d" text="%s" class="%s" focused="%s" clickable="%s" '
            'visible-to-user="true" bounds="%s" />' % (i, text, cls, focused, clickable, bounds))


def write_dump(*nodes):
    tmp = tempfile.mkdtemp(prefix="e2e-d-")
    p = os.path.join(tmp, "d.xml")
    open(p, "w").write(dump(*nodes))
    return p


TAP = os.path.join(ROOT, "tap_node.py")
DIAG = os.path.join(ROOT, "dump_diag.py")


def cli(script, *args):
    return subprocess.run([sys.executable, script, *args], capture_output=True, text=True).stdout.strip()


@case
def test_dump_diag_prints_full_field_text_and_focus():
    long_txt = "PG8J+MWG, 611 Av.El Hadj Mansour Sy., Dakar E2E Dest"
    p = write_dump(fnode(0, long_txt, "[50,100][900,200]"),
                   fnode(1, "", "[50,220][900,320]", focused="true"))
    out = cli(DIAG, p)
    assert long_txt in out, out            # plus de coupe à 40 caractères
    assert "FOCUS:" in out, out            # le champ qui a le focus est indiqué


@case
def test_dump_diag_contains_is_accent_and_case_insensitive():
    p = write_dump(fnode(0, "e2e stop 1 médina", "[50,100][900,200]"))
    assert subprocess.run([sys.executable, DIAG, "--contains", p, "E2E Stop 1 Medina"]).returncode == 0
    assert subprocess.run([sys.executable, DIAG, "--contains", p, "E2E Dest"]).returncode == 1


@case
def test_dump_diag_texts_mode_lists_visible_texts_for_timeouts():
    p = write_dump(node(0, "Paiement", "[0,0][100,100]"), node(1, "Récapitulatif de la course", "[0,200][100,300]"))
    out = cli(DIAG, p, "--texts")
    assert "Paiement" in out and "Récapitulatif de la course" in out, out


@case
def test_tap_node_empty_input_targets_the_empty_destination_field():
    p = write_dump(fnode(0, "PG8J+MWG, 611 Av.", "[50,100][900,200]"), fnode(1, "", "[50,220][900,320]"))
    assert cli(TAP, "--empty-input", p) == "475 270 input"
    only_filled = write_dump(fnode(0, "A", "[50,100][900,200]"), fnode(1, "Adresse de l'arrêt", "[50,220][900,320]"))
    assert cli(TAP, "--empty-input", only_filled) == "475 270 input-last"
    assert cli(TAP, "--empty-input", write_dump(node(0, "x", "[0,0][10,10]"))) == ""


@case
def test_tap_node_row_control_finds_the_icon_on_the_mode_de_paiement_row():
    img = "android.widget.ImageView"
    p = write_dump(
        node(0, "Mode de paiement", "[87,1818][600,1908]"),
        node(1, "", "[845,1818][997,1908]", cls=img, clickable="true"),   # la bonne cible
        node(2, "", "[845,1400][997,1490]", cls=img, clickable="true"),   # autre ligne
        node(3, "", "[700,1818][780,1908]", cls=img, clickable="false"),  # non cliquable
        node(4, "Pourboire", "[87,1500][600,1590]"))
    assert cli(TAP, "--row-control", p, "Mode de paiement") == "921 1863 control"
    assert cli(TAP, "--row-control", p, "Libellé absent") == ""


@case
def test_ui_screen_diag_logs_visible_texts_from_a_real_dump():
    p = write_dump(node(0, "Prise en charge du client", "[46,1942][1034,2081]"),
                   node(1, "Annuler la course", "[46,2100][1034,2200]"))
    env = dict(os.environ, OUT=tempfile.mkdtemp(), UI_XML=p, UI_XML_SRC=p, E2E_ALLOW_FIXTURE="1")
    os.makedirs(os.path.join(env["OUT"], "ui"), exist_ok=True)
    env.pop("GITHUB_ACTIONS", None)
    r = subprocess.run(["bash", "-c", '. "$1"; ui_screen_diag "driver 37"', "_", os.path.join(ROOT, "ui.sh")],
                       capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr
    assert "driver 37" in out and "Prise en charge du client" in out and "Annuler la course" in out, out


@case
def test_snap_timeout_always_logs_the_real_screen_texts():
    t = open(os.path.join(ROOT, "ui.sh"), encoding="utf-8").read()
    assert '[ "$st" = TIMEOUT ] && ui_screen_diag "snap $name TIMEOUT' in t


@case
def test_places_field_check_reports_typed_text_presence():
    p = write_dump(fnode(0, "PG8J+MWG", "[50,100][900,200]"), fnode(1, "E2E Dest", "[50,220][900,320]", focused="true"))
    env = dict(os.environ, OUT=tempfile.mkdtemp(), UI_XML=p, UI_XML_SRC=p, E2E_ALLOW_FIXTURE="1")
    os.makedirs(os.path.join(env["OUT"], "ui"), exist_ok=True)
    env.pop("GITHUB_ACTIONS", None)
    def check(q):
        r = subprocess.run(["bash", "-c", '. "$1"; . "$2"; places_field_check "$3"; echo "rc=$?"', "_",
                            os.path.join(ROOT, "ui.sh"), os.path.join(ROOT, "device_prep.sh"), q],
                           capture_output=True, text=True, env=env)
        return r.stdout + r.stderr
    ok = check("E2E Dest")
    assert "rc=0" in ok and "trouvée_dans_un_champ=oui" in ok and "FOCUS:E2E Dest" in ok, ok
    ko_ = check("Autre requête")
    assert "rc=1" in ko_ and "trouvée_dans_un_champ=NON" in ko_, ko_


@case
def test_ui_pick_place_focuses_the_field_before_typing_and_verifies():
    t = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
    body = t[t.index("ui_pick_place() {"):t.index("compose_ride_ui() {")]
    assert body.index("tap_node_mode --empty-input") < body.index("adb shell input text"), "tap du champ AVANT la saisie"
    assert "places_field_check" in body and "un seul nouvel essai" in body
    assert "unicodedata" in body, "requête ASCII : adb input text ne tape pas les accents"


@case
def test_pickup_proves_backend_state_right_after_confirm():
    t = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
    i = t.index('dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler"')
    j = t.index("ride_state", i)
    k = t.index('wait_ui "$DRIVER_PKG" 25 "$MARK_driver_running_stop1"')
    assert i < j < k, "ride_state entre la fenêtre confirmée et l'attente de l'écran"
    assert "ui_screen_diag" in t[k:k + 400]
    api = open(os.path.join(ROOT, "api_flow.py"), encoding="utf-8").read()
    assert 'print(f"START remark=' in api, "le repli start doit journaliser la réponse du backend"


@case
def test_payment_goes_through_the_paiement_screen_in_order():
    t = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
    seq = ['try_tap "$RIDER_PKG" "Payer maintenant"',
           "wait_ui \"$RIDER_PKG\" 20 '+Mode de paiement'",
           'tap_node_mode --row-control "$RIDER_PKG" "Mode de paiement"',
           "cap rider-cash-methods 26-rider-cash-methods \"$RIDER_PKG\" 15",
           'tap_when "$RIDER_PKG" "Paiement en espèces" "Espèces" "Cash"',
           "wait_ui \"$RIDER_PKG\" 15 '+Payer' && tap_exact \"Payer\" && pay_ui=1",
           "cap rider-cash-waiting 27-rider-cash-waiting \"$RIDER_PKG\" 25"]
    pos = [t.index(x) for x in seq]
    assert pos == sorted(pos), pos
    # 31 (accueil) ne vient qu'après 27
    assert t.index("cap rider-cash-waiting 27") < t.index("cap rider-home-after 31")




@case
def test_end_ride_and_cash_confirm_handle_dialog():
    t = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
    i = t.index('tap_exact "Terminer la course";')
    assert t[i:].split('\n')[1].strip() == 'dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler" >/dev/null 2>&1 || true'
    j = t.index('tap_exact "Confirmer le paiement";')
    assert t[j:].split('\n')[1].strip() == 'dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler" >/dev/null 2>&1 || true'

@case
def test_places_wait_result_logs_field_diagnostic():
    t = open(os.path.join(ROOT, "device_prep.sh"), encoding="utf-8").read()
    assert t.count("dump_diag.py") >= 2 and "places champ" in t


if __name__ == "__main__":
    ok = 0
    for c in CASES:
        try:
            c()
            print("ok  ", c.__name__)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print("FAIL", c.__name__ + ":", type(e).__name__, e)
    print("%d/%d ui-label cases passed" % (ok, len(CASES)))
    sys.exit(0 if ok == len(CASES) else 1)
