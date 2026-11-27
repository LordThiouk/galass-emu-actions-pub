#!/usr/bin/env python3
"""Tests du run 45004400552 : diagnostics d'arrêt toujours écrits, repli API diagnostique,
marqueurs Rider (compteur d'arrêts, accueil), attente de changement d'écran (35b), dialogue
« Terminez votre course ». Sans émulateur ; textes d'écran = ceux relevés dans le log réel."""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import test_stop_confirm_ui as base  # noqa: E402
import test_report  # noqa: E402

CASES = []
node, dump = base.node, base.dump


def case(fn):
    CASES.append(fn)
    return fn


def run_sh():
    return open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()


def marker(name):
    m = re.search(r"^MARK_%s='([^']*)'" % name, run_sh(), re.M)
    assert m, "MARK_%s introuvable" % name
    return m.group(1)


def screen(*texts):
    return dump(*[node(i, t, "[0,%d][1000,%d]" % (i * 100 + 10, i * 100 + 90)) for i, t in enumerate(texts)])


def spec_ok(spec, xml):
    """Évalue une spec avec le vrai _spec_ok de ui.sh sur un dump de fixture."""
    tmp = tempfile.mkdtemp(prefix="e2e-spec-")
    path = os.path.join(tmp, "d.xml")
    open(path, "w", encoding="utf-8").write(xml)
    script = 'export OUT="%s"; mkdir -p "$OUT/ui"; . "%s"; UI_XML_PATH="%s"; _spec_ok "$1"; echo rc=$?' % (
        tmp, os.path.join(ROOT, "ui.sh"), path)
    p = subprocess.run(["bash", "-c", script, "_", spec], capture_output=True, text=True, timeout=30)
    return p.stdout.strip().endswith("rc=0")


# --------------------------------------------------------------- 1. diagnostics dbg-stopN-aK
CALL = base.CALL


@case
def test_call_site_is_unconditional_in_stop_loop():
    """Point d'appel réel : boucle `for i in 0 1`, juste après l'attente du bouton ; aucune garde
    `if`/`&&` ne peut empêcher l'appel (le log du run montre les 3 taps : la fonction EST appelée)."""
    lines = run_sh().splitlines()
    idx = [i for i, l in enumerate(lines) if l.startswith('  driver_stop_confirm_ui "$DRIVER_PKG" "$n"; stop_rc=$?')]
    assert len(idx) == 1, idx
    assert any(l.startswith("for i in 0 1; do") for l in lines[:idx[0]]), "appel hors boucle d'arrêts"
    prev = next(l for l in reversed(lines[:idx[0]]) if l.strip() and not l.strip().startswith("#"))
    assert prev.strip() == "fi", prev  # fin du bloc `wait_ui ... +Confirmer l.arrêt` : pas une garde


@case
def test_stop_diag_logs_one_line_per_attempt_on_failure():
    tmp = tempfile.mkdtemp(prefix="e2e-stop-")
    rc, taps, out = base.harness(tmp, [base.RUNNING, base.DIALOG, base.RUNNING], [0], CALL)
    assert rc == 1, out
    for k in (1, 2, 3):
        assert "stop_diag dbg-stop1-a%d:" % k in out, out
        assert os.path.isfile(os.path.join(tmp, "diag", "dbg-stop1-a%d.xml" % k)), os.listdir(os.path.join(tmp, "diag"))
        assert os.path.isfile(os.path.join(tmp, "screens", "dbg-stop1-a%d.png" % k))
    assert "0/2 arrêt terminé" in out, out  # textes de l'écran dans le log


@case
def test_stop_diag_written_when_server_already_saw_the_stop():
    tmp = tempfile.mkdtemp(prefix="e2e-stop-")
    rc, taps, out = base.harness(tmp, [base.RUNNING], [1], CALL)
    assert rc == 3, out
    assert "stop_diag dbg-stop1-a1-already:" in out, out
    assert os.path.isfile(os.path.join(tmp, "diag", "dbg-stop1-a1-already.xml"))
    assert "input tap" not in taps, taps


@case
def test_stop_diag_written_when_button_absent():
    tmp = tempfile.mkdtemp(prefix="e2e-stop-")
    rc, taps, out = base.harness(tmp, [base.HOME], [0], CALL)
    assert rc == 2, out
    assert "stop_diag dbg-stop1-a1-nobutton:" in out, out
    assert os.path.isfile(os.path.join(tmp, "diag", "dbg-stop1-a1-nobutton.xml"))


@case
def test_stop_diag_written_when_dump_unavailable():
    tmp = tempfile.mkdtemp(prefix="e2e-stop-")
    rc, taps, out = base.harness(tmp, [base.RUNNING], [0], "ui_xml() { return 1; }\n" + CALL)
    assert rc == 1, out
    for k in (1, 2, 3):
        assert "stop_diag dbg-stop1-a%d-nodump:" % k in out, out
        assert os.path.isfile(os.path.join(tmp, "screens", "dbg-stop1-a%d-nodump.png" % k))
    assert "dump indisponible" in out, out
    assert "input tap" not in taps, taps


@case
def test_stop_diag_written_on_success_too():
    tmp = tempfile.mkdtemp(prefix="e2e-stop-")
    rc, taps, out = base.harness(tmp, [base.RUNNING, base.DIALOG, base.DIALOG, base.RUNNING], [0, 1], CALL)
    assert rc == 0, out
    assert "stop_diag dbg-stop1-a1-ok:" in out, out


@case
def test_stop_diag_written_when_server_sees_stop_on_later_attempt():
    tmp = tempfile.mkdtemp(prefix="e2e-stop-")
    rc, taps, out = base.harness(tmp, [base.RUNNING, base.DIALOG, base.RUNNING], [0, 0, 1], CALL)
    assert rc == 0, out
    assert "stop_diag dbg-stop1-a1:" in out and "stop_diag dbg-stop1-a2-seen:" in out, out


@case
def test_workflow_collects_dbg_diag_files():
    wf = open(os.path.join(ROOT, "..", ".github", "workflows", "artemis-staging-e2e.yml"), encoding="utf-8").read()
    assert "e2e-out/diag/dbg-*.xml" in wf and "e2e-out/diag/dbg-*.logcat" in wf


# --------------------------------------------------------------- 2. repli API = diagnostique
@case
def test_api_fallback_never_counts_a_stop_as_ui_done():
    ui = open(os.path.join(ROOT, "ui.sh"), encoding="utf-8").read()
    body = ui[ui.index("driver_stop_confirm_ui() {"):ui.index("# rider_leave_rating pkg")]
    assert not re.search(r"\bapi\s+(stop_one|loc_stop)", body), "appel API dans la confirmation UI"
    src = run_sh()
    loop = src[src.index("for i in 0 1; do"):src.index('mark ui-stops')]
    # ui_stops et UI_STOP* ne sont incrémentés que dans la branche `ui_stop_ok = 1`
    ok_branch = loop[loop.index('if [ "$ui_stop_ok" = "1" ]; then'):loop.index("  else\n    mark \"driver-stop${n}-done\" FAIL")]
    assert "ui_stops=$((ui_stops + 1))" in ok_branch
    fb_start = loop.index("  else\n    mark \"driver-stop${n}-done\" FAIL")
    fb = loop[fb_start:loop.index("  ride_state\n  bring_fg \"$RIDER_PKG\"", fb_start)]
    assert "ui_stops" not in fb and "UI_STOP" not in fb, fb
    assert 'mark "driver-stop${n}-done" FAIL "api fallback' in fb
    assert fb.index("mark ") < fb.index("api stop_one"), "le FAIL doit précéder le repli API"
    assert '[ "$stop_rc" = "0" ] && ui_stop_ok=1' in loop


@case
def test_fail_from_api_fallback_survives_a_later_screen_pass():
    out = test_report.make_out(
        [("driver-stop1-done", "FAIL", "api fallback: stop 1"), ("driver-stop1-done", "PASS", "ui cap=38 marker ok")])
    res = test_report.report.build(out, p0={"driver-stop1-done"}) if hasattr(test_report, "report") else None
    import report
    res = report.build(out, p0={"driver-stop1-done"})
    row = {p["point"]: p for p in res["points"]}["driver-stop1-done"]
    assert row["result"] == "FAIL", row


# --------------------------------------------------------------- 3. marqueurs Rider F4-27 (Arrêt N)
# Papa 27/11 21:22 : plus de bandeaux « En route » / « x/N » ; frise Arrêt 1…N + Destination.
FRISE_RUNNING = screen("Lieu de prise en charge", "Arrêt 1", "Arrêt 2", "Destination", "E2E Driver")
FRISE_DONE = screen("Lieu de prise en charge", "Arrêt 1", "Arrêt 2", "Destination", "Point E", "E2E Driver")
PLACES = screen("Confirmer le lieu", "Destination", "Fann, Senegal")  # run 460606 capture 19
BLOCKED = screen("Terminez votre course", "Vous êtes arrivé à destination. Veuillez confirmer pour term",
                 "Continuer", "Dismiss")


@case
def test_rider_stop1_done_matches_f427_frise():
    assert spec_ok(marker("rider_stop1_done"), FRISE_RUNNING)


@case
def test_rider_stop2_done_matches_f427_frise_with_destination():
    assert spec_ok(marker("rider_stop2_done"), FRISE_DONE)


@case
def test_rider_stop_markers_reject_places_and_old_bandeaux():
    s1, s2 = marker("rider_stop1_done"), marker("rider_stop2_done")
    assert not spec_ok(s1, PLACES)  # stuck compose (460606)
    assert not spec_ok(s1, screen("En route vers E2E Stop 2"))  # bandeau retiré
    assert not spec_ok(s2, screen("2/2 arrêts terminés", "En route vers Point E"))
    assert not spec_ok(s1, BLOCKED) and not spec_ok(s2, BLOCKED)


# --------------------------------------------------------------- 4. accueil Rider
REAL_HOME = screen("E2E Rider MHV3+674, Rue CO 38, Dakar, Senegal", "Partons pour une course", "Où allez-vous ?",
                   "Sélectionnez un service", "Economie voiture/economie CFA400/kilomètres")  # capture 31 réelle


@case
def test_rider_home_after_matches_real_home_screen():
    assert spec_ok(marker("rider_home_after"), REAL_HOME)


@case
def test_old_home_marker_really_failed_on_real_home():
    old = "+Confirmer le lieu|+Lieu de prise en charge|+Destination|-Course terminée"
    assert not spec_ok(old, REAL_HOME)  # reproduit le TIMEOUT du run


@case
def test_rider_home_after_rejects_other_rider_screens():
    spec = marker("rider_home_after")
    assert not spec_ok(spec, screen("Confirmer le lieu", "Lieu de prise en charge", "Destination"))  # composition
    assert not spec_ok(spec, screen("Où allez-vous ?", "Sélectionnez un service"))                    # sans « Partons »
    assert not spec_ok(spec, screen("Partons pour une course", "Course terminée", "Avis"))            # notation
    assert not spec_ok(spec, BLOCKED)
    assert not spec_ok(spec, screen("Partons pour une course", "Où allez-vous ?", "Terminez votre course"))


# --------------------------------------------------------------- 5. duplicate-state (35b)
BASE = [node(0, "0/2 arrêt terminé", "[42,1428][1038,1473]"), node(1, "Confirmer l'arrêt", "[42,1938][1038,2085]", "true")]


def frame(*extra):
    return dump(*(BASE + list(extra)))


@case
def test_wait_screen_change_fails_on_identical_screen():
    ref = frame(node(2, "32 mins Temps estimé", "[0,0][10,10]"))
    tmp = tempfile.mkdtemp(prefix="e2e-wsc-")
    open(os.path.join(tmp, "ref.xml"), "w", encoding="utf-8").write(ref)
    rc, _, out = base.harness(tmp, [ref], [0], 'wait_screen_change "%s/ref.xml" 4\necho "rc=$?"' % tmp)
    assert rc == 1, out


@case
def test_wait_screen_change_ignores_ticking_timer():
    ref = frame(node(2, "32 mins Temps estimé", "[0,0][10,10]"))
    tick = frame(node(2, "31 mins Temps estimé", "[0,0][10,10]"))
    tmp = tempfile.mkdtemp(prefix="e2e-wsc-")
    open(os.path.join(tmp, "ref.xml"), "w", encoding="utf-8").write(ref)
    rc, _, out = base.harness(tmp, [tick], [0], 'wait_screen_change "%s/ref.xml" 4\necho "rc=$?"' % tmp)
    assert rc == 1, out


@case
def test_wait_screen_change_succeeds_when_counter_moves():
    ref = frame()
    moved = dump(node(0, "1/2 arrêt terminé", "[42,1428][1038,1473]"), BASE[1])
    tmp = tempfile.mkdtemp(prefix="e2e-wsc-")
    open(os.path.join(tmp, "ref.xml"), "w", encoding="utf-8").write(ref)
    rc, _, out = base.harness(tmp, [ref, moved], [0], 'wait_screen_change "%s/ref.xml" 10\necho "rc=$?"' % tmp)
    assert rc == 0, out


@case
def test_run_waits_for_distinct_screen_before_capturing_35b():
    src = run_sh()
    i_wait = src.index('wait_screen_change "$OUT/ui/37-driver-running-stop1.xml"')
    i_cap = src.index("cap driver-inride-stops 35b-driver-inride")
    assert i_wait < i_cap
    assert src.index("cap driver-running-stop1 37-driver-running-stop1") < i_wait
    assert "pas de capture dupliquée" in src


# --------------------------------------------------------------- 6. dialogue « Terminez votre course »
@case
def test_dismiss_dialog_uses_back_never_continue():
    rc, taps, out = base.run([BLOCKED, base.RUNNING], [0], 'rider_dismiss_blocking_dialog pkg\necho "rc=$?"')
    assert rc == 0, out
    assert taps.count("keyevent 4") == 1, taps
    assert "input tap" not in taps, taps  # jamais « Continuer » (terminerait la course)


@case
def test_dismiss_dialog_absent_sends_nothing():
    rc, taps, out = base.run([base.RUNNING], [0], 'rider_dismiss_blocking_dialog pkg\necho "rc=$?"')
    assert rc == 3, out
    assert taps.strip() == "", taps


@case
def test_dismiss_dialog_reports_stuck_dialog():
    rc, taps, out = base.run([BLOCKED], [0], 'rider_dismiss_blocking_dialog pkg\necho "rc=$?"')
    assert rc == 1, out
    assert taps.count("keyevent 4") == 2, taps


@case
def test_run_traces_dialog_as_bug_and_dismisses_before_rider_stop_captures():
    src = run_sh()
    i_dlg = src.index('rider_dismiss_blocking_dialog "$RIDER_PKG"')
    assert i_dlg < src.index("cap rider-stop1-done 23-rider-stop1-done")
    assert 'mark "bug-rider-stop${n}-run-dialog" WARN' in src


# --------------------------------------------------------------- 7. aucune fausse clé en dur
@case
def test_no_hardcoded_fake_secret_left_in_smoke_log_test():
    body = open(os.path.join(HERE, "test_smoke_log.py"), encoding="utf-8").read()
    for lit in ("deadbeefcafebabe1234", "sk-live-0123456789abcdef", "AIzaSy0123456789", "r3fr3sh-s3cr3t",
                "8f3c1d9a4b2e7c55", "abcdEFGH1234", "abcdef1234567890"):
        assert lit not in body, lit


# --------------------------------------------------------------- 8. logcat de l'APP sur la fenêtre du tap
import secrets  # noqa: E402

TOKEN = secrets.token_hex(12)  # jeton factice généré à l'exécution
ADB = r'''
adb() {
  echo "$@" >> "%(taps)s"
  case "$*" in
    *pidof*) echo "4242" ;;
    *"date +"*) echo "11-22 16:30:00.000" ;;
    *logcat*) cat <<LOG
11-22 16:30:01.100  4242  4242 I flutter : url--------------https://x.test/api/driver/rides/complete-stop/42
11-22 16:30:01.200  4242  4242 I flutter : status-----------422
11-22 16:30:01.300  4242  4242 I flutter : complete-stop rejected: http=422 remark=stop_too_far message=You are too far
11-22 16:30:01.350  4242  4242 I flutter : complete-stop POST token=%(token)s
11-22 16:30:01.400  4242  4242 D AndroidRuntime: Shutting down VM
11-22 16:30:01.450  4242  4242 I flutter : ligne-app-connue-sans-motif ride=7
11-22 16:30:01.460  9999  9999 I flutter : ligne-du-rider-autre-pid complete-stop
11-22 16:30:01.500   542   542 E AppOps  : binder noise
LOG
    ;;
  esac
}
'''


def adb_fn(tmp):
    return ADB % dict(taps=os.path.join(tmp, "taps"), token=TOKEN)


@case
def test_app_logcat_since_keeps_app_http_and_hides_noise_and_tokens():
    tmp = tempfile.mkdtemp(prefix="e2e-lc-")
    rc, taps, out = base.harness(tmp, [base.RUNNING], [0], adb_fn(tmp) + 'app_logcat_since "11-22 16:30:00.000"; echo "rc=$?"')
    assert "complete-stop rejected: http=422 remark=stop_too_far" in out, out
    assert "status-----------422" in out and "complete-stop/42" in out, out
    assert "Shutting down VM" not in out and "AppOps" not in out, out  # bruit uiautomator / binder
    assert TOKEN not in out, "jeton non masqué"
    assert "logcat -d -v threadtime -T 11-22 16:30:00.000" in taps, taps
    assert "ligne-app-connue-sans-motif" in out, out        # ligne connue de l'app Driver (tag flutter), sans motif
    assert "ligne-du-rider-autre-pid" not in out, out        # autre pid : exclue


@case
def test_stop_diag_logs_app_lines_and_writes_logcat_file():
    tmp = tempfile.mkdtemp(prefix="e2e-lc-")
    rc, taps, out = base.harness(tmp, [base.RUNNING], [0],
                                 adb_fn(tmp) + 'STOP_TAP_TS="11-22 16:30:00.000"; stop_diag stop1-a1; echo "rc=$?"')
    assert "stop_diag dbg-stop1-a1 app: " in out and "remark=stop_too_far" in out, out
    body = open(os.path.join(tmp, "diag", "dbg-stop1-a1.logcat"), encoding="utf-8").read()
    assert "complete-stop rejected" in body and TOKEN not in body, body


@case
def test_confirm_marks_tap_time_then_logs_screen_after_confirm():
    tmp = tempfile.mkdtemp(prefix="e2e-lc-")
    toast = screen("Confirmer l'arrêt", "You are too far from the pending stop to confirm it.")
    dlg = base.dump(base.node(0, "Confirmer l'arrêt", "[100,1300][900,1380]"), base.node(1, "Annuler", "[100,1400][400,1520]", clickable="true"), base.node(2, "Confirmer", "[500,1400][900,1520]", clickable="true"))
    rc, taps, out = base.harness(tmp, [base.RUNNING, dlg, dlg, toast, base.RUNNING], [0, 0, 0, 0, 0, 0, 0, 0], adb_fn(tmp) + CALL)
    lines = taps.splitlines()
    i_date = next(i for i, l in enumerate(lines) if "date +" in l)
    i_tap = next(i for i, l in enumerate(lines) if "input tap" in l)
    assert i_date < i_tap, "repère temporel pris AVANT le tap"
    assert "ecran stop1-a1-apres-confirmer:" in out, out
    assert "dialogue d'arrêt reconnu" in out, out
    assert "stop_diag dbg-stop1-a1 app: " in out and "remark=stop_too_far" in out, out


@case
def test_dialog_not_confirmed_writes_nodialog_diag():
    tmp = tempfile.mkdtemp(prefix="e2e-lc-")
    rc, taps, out = base.harness(tmp, [base.RUNNING], [0], adb_fn(tmp) + CALL)
    assert "stop_diag dbg-stop1-a1-nodialog:" in out, out
    assert os.path.isfile(os.path.join(tmp, "diag", "dbg-stop1-a1-nodialog.xml"))


@case
def test_tap_timestamp_is_one_quoted_adb_argument():
    # `adb shell date +'%m-%d …'` éclatait en 2 arguments côté appareil (date « bad date » → ts vide → logcat=0)
    src = open(os.path.join(base.ROOT, "ui.sh"), encoding="utf-8").read()
    assert "adb shell \"date +'%m-%d %H:%M:%S.000'\"" in src
    assert "adb shell date +" not in src


@case
def test_invalid_timestamp_falls_back_to_last_lines_and_stats_are_logged():
    tmp = tempfile.mkdtemp(prefix="e2e-lc-")
    rc, taps, out = base.harness(tmp, [base.RUNNING], [0],
                                 adb_fn(tmp) + 'STOP_TAP_TS=""; stop_diag stop1-a1; echo "rc=$?"')
    assert "logcat -d -v threadtime -t 600" in taps, taps
    assert "pid=4242" in out and "brut=" in out and "app=" in out, out
    assert "ligne-app-connue-sans-motif" in out, out


@case
def test_stop_prime_sets_gps_then_waits_for_fresh_driver_ping_on_server():
    tmp = tempfile.mkdtemp(prefix="e2e-lc-")
    taps_path = os.path.join(tmp, "taps")
    body = (adb_fn(tmp).replace("*pidof*)", "*emu*) ;;\n    *pidof*)")
            + 'STOP_LAT=(14.71 14.702); STOP_LNG=(-17.462 -17.455)\n'
            + 'api() { echo "api $* idx=$STOP_INDEX" >> "%s"; [ "$1" = "stop_coords" ] && return 0; echo "PING updated_at=T$RANDOM dist_m=10 age_s=2 PING_OK"; }\n' % taps_path
            + 'stop_prime 2; echo "rc=$?"')
    body = body.replace("complete-stop POST token", "driver/location-update ping token")
    rc, taps, out = base.harness(tmp, [base.RUNNING], [0], body)
    assert "api stop_coords idx=1" in taps, taps   # coords réelles tentées d'abord (repli ici sur le tableau)
    assert "emu geo fix -17.455 14.702" in taps, taps
    assert "api stop_ping_check idx=1" in taps, taps
    assert "api loc_stop" not in taps, taps   # le ping API masquerait le ping du Driver (ordre : test_stop_prime_order.py)
    assert "ping Driver frais vu" in out, out
    rc, taps, out = base.harness(tempfile.mkdtemp(prefix="e2e-lc-"), [base.RUNNING], [0], 'stop_prime 1; echo "rc=$?"')
    assert rc == 0 and "geo fix" not in taps, (rc, taps)


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("ok  ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, str(e)[:400])
    print("%d/%d cas passés" % (len(CASES) - failed, len(CASES)))
    raise SystemExit(1 if failed else 0)
