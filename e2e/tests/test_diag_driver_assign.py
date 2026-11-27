#!/usr/bin/env python3
"""TDD spec du diagnostic diag-driver-assign — une MESURE, pas une porte.

Runs 44468438500 / 44476021728 (56 min d'intervalle, aucun changement de code) :
même signature {status:0, driver_id:0, stops:2, stops_completed:0,
payment_status:3, ok:false} — la course est créée et payée mais AUCUN chauffeur
n'est assigné, donc aucun écran cash n'est jamais atteint. La cause n'est pas
lisible depuis l'extérieur.

Le diagnostic lit l'état du chauffeur AVANT la fenêtre d'accept (step read-only
`assign_pre`) et le dernier remark backend (api.jsonl, path
`driver/ride/offer/respond`) pour nommer la cause dans le digest. Il ne change
ni un P0, ni un seuil, ni le verdict.

Piège obligatoire : bal=-99999 avec remark=not_authorized => la cause doit être
`no-offer`, jamais `bal-guard` (le seuil n'est pas lisible en public, c'est le
remark `balance_limit` qui porte l'information).
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RUN = os.path.join(ROOT, "run.sh")
FLOW = os.path.join(ROOT, "api_flow.py")
DIAG = os.path.join(ROOT, "diag_assign.py")
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_text():
    return open(RUN, encoding="utf-8").read()


def flow_text():
    return open(FLOW, encoding="utf-8").read()


def diag_text():
    return open(DIAG, encoding="utf-8").read()


def step_block(name):
    """Bloc `elif step == "<name>":` du dispatcheur de api_flow.py."""
    text = flow_text()
    start = text.index(f'elif step == "{name}":')
    rest = text[start + len(f'elif step == "{name}":'):]
    m = re.search(r"\n(?:elif|else) step ==|\nelse:", rest)
    return rest[:m.start()] if m else rest


def make_out(assign_pre="assign_pre bal=100 online=1",
             state="status=0 stops_done=0 payment_status=3", records=(),
             omit_state=False):
    tmp = tempfile.mkdtemp(prefix="e2e-diag-")
    with open(os.path.join(tmp, "assign-pre.txt"), "w", encoding="utf-8") as fh:
        fh.write(assign_pre + "\n")
    if not omit_state:
        with open(os.path.join(tmp, "last-ride-state.txt"), "w", encoding="utf-8") as fh:
            fh.write(state + "\n")
    with open(os.path.join(tmp, "api.jsonl"), "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return tmp


def respond(remark, code=403):
    return {"t": 0, "m": "POST", "path": "driver/ride/offer/respond",
            "code": code, "resp": {"status": "error", "remark": remark}}


def run_diag(out):
    proc = subprocess.run([sys.executable, DIAG, out], capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"une seule ligne attendue, vu {lines!r}"
    verdict, tab, detail = lines[0].partition("\t")
    assert tab == "\t", f"VERDICT<TAB>detail attendu, lu {lines[0]!r}"
    return verdict, detail


# ------------------------------------------------------------- le fichier existe
@case
def test_diag_assign_py_exists():
    assert os.path.isfile(DIAG), DIAG


# ------------------------------------------------- step assign_pre (read-only)
@case
def test_assign_pre_step_exists_and_is_read_only():
    block = step_block("assign_pre")
    assert "driver/dashboard" in block, block
    assert "driver/driver-info" in block, block
    # Ni save() ni go_online() : online-status est un TOGGLE qui fausserait le capteur.
    assert "save()" not in block, block
    assert "go_online()" not in block, block
    # Strictement read-only : deux GET, aucun POST.
    assert block.count('call("GET"') == 2, block
    assert 'call("POST"' not in block, block
    assert "assign_pre bal=" in block, block
    assert "online=" in block, block


# --------------------------------------------------------------- causes backend
@case
def test_trap_balance_is_never_the_cause():
    """Piège : bal=-99999 + remark=not_authorized => no-offer, pas bal-guard."""
    out = make_out(assign_pre="assign_pre bal=-99999 online=1",
                   records=[respond("not_authorized")])
    verdict, detail = run_diag(out)
    assert verdict == "FAIL", (verdict, detail)
    assert "no-offer" in detail, detail
    assert "bal-guard" not in detail, detail


@case
def test_cause_mapping_follows_the_backend_remark():
    mapping = {
        "balance_limit": "bal-guard",
        "driver_offline": "offline",
        "ride_unavailable": "ride-gone",
        "offer_expired": "offer-expired",
        "not_authorized": "no-offer",
    }
    for remark, cause in mapping.items():
        out = make_out(records=[respond(remark)])
        verdict, detail = run_diag(out)
        assert verdict == "FAIL", (remark, verdict, detail)
        assert cause in detail, (remark, cause, detail)


@case
def test_no_attempt_is_no_attempt():
    out = make_out(records=[])
    verdict, detail = run_diag(out)
    assert verdict == "FAIL", (verdict, detail)
    assert "no-attempt" in detail, detail


@case
def test_unknown_remark_is_named_short():
    out = make_out(records=[respond("driver_ineligible")])
    verdict, detail = run_diag(out)
    assert verdict == "FAIL", (verdict, detail)
    assert "remark-driver-ineligible" in detail, detail


@case
def test_attempt_without_remark_is_no_remark():
    rec = {"t": 0, "m": "POST", "path": "driver/ride/offer/respond",
           "code": 500, "resp": {"status": "error"}}
    out = make_out(records=[rec])
    verdict, detail = run_diag(out)
    assert verdict == "FAIL", (verdict, detail)
    assert "no-remark" in detail, detail


@case
def test_only_respond_records_count():
    """Les GET de la sonde (driver/dashboard, driver/driver-info) ne sont pas des tentatives."""
    out = make_out(records=[
        {"t": 0, "m": "GET", "path": "driver/dashboard", "code": 200, "resp": {}},
        {"t": 0, "m": "GET", "path": "driver/driver-info", "code": 200, "resp": {}},
    ])
    verdict, detail = run_diag(out)
    assert "no-attempt" in detail, detail


# --------------------------------------------------------------------- verdicts
@case
def test_status_nonzero_is_pass():
    out = make_out(state="status=3 stops_done=2 payment_status=1")
    verdict, detail = run_diag(out)
    assert verdict == "PASS", (verdict, detail)
    assert "assigned" in detail, detail


@case
def test_unreadable_state_is_warn_never_fail():
    out = make_out(omit_state=True)
    verdict, detail = run_diag(out)
    assert verdict == "WARN", (verdict, detail)
    assert "state-unreadable" in detail, detail


@case
def test_detail_is_capped_at_140():
    out = make_out(records=[respond("accept_failed_" + "x" * 300)])
    _verdict, detail = run_diag(out)
    assert len(detail) <= 140, (len(detail), detail)


@case
def test_diag_never_reads_a_balance_threshold():
    """Le seuil negative_balance_driver n'est pas public : jamais de comparaison bal/seuil."""
    text = diag_text()
    assert "negative_balance_driver" not in text, text
    # la cause bal-guard ne peut venir que du remark, jamais d'un calcul sur bal
    assert "bal-guard" in text, text


@case
def test_diag_is_read_only():
    text = diag_text()
    assert 'open(' in text, text
    assert not re.search(r"open\([^)]*['\"][wa]['\"]", text), text
    assert "state.json" not in text, text
    assert "api.jsonl" in text, text


# ------------------------------------------------------------- intégration run.sh
@case
def test_marker_placed_exactly_once():
    assert run_text().count("mark diag-driver-assign") == 1, run_text().count(
        "mark diag-driver-assign")


@case
def test_marker_is_not_named_assign_diag():
    assert "assign-diag" not in run_text(), "le marqueur doit s'appeler diag-driver-assign"


@case
def test_early_accept_sleep_is_five():
    # B7 45934624889 : sleep 11 trop tard vs offer_timeout ~30s
    assert "(sleep 5; STEP=accept" in run_text(), run_text()


@case
def test_accept_offer_aborts_on_offer_expired():
    src = flow_text()
    assert '"offer_expired"' in src and "ACCEPT abort" in src, src


@case
def test_api_accept_still_two_occurrences():
    n = len(re.findall(r"\bapi accept\b", run_text()))
    assert n == 2, n


@case
def test_diag_is_neither_p0_nor_ui_point():
    import report
    assert "diag-driver-assign" not in report.P0_DEFAULT, report.P0_DEFAULT
    assert "diag-driver-assign" not in report.UI_POINTS, report.UI_POINTS


@case
def test_sonde_writes_assign_pre_and_cleans_it():
    text = run_text()
    assert "api assign_pre" in text, text
    assert 'assign-pre.txt' in text, text
    # la sonde écrit une ligne par défaut si le step n'a rien rendu
    assert re.search(r'ASSIGN_PRE_LINE="assign_pre bal=\? online=\?"', text), text
    # S6 : le fichier de sonde ne doit pas fuir d'un run à l'autre
    s6 = re.search(r"rm -rf.*?verdict\.txt", text, re.S)
    assert s6 and "assign-pre.txt" in s6.group(0), text[:400]


@case
def test_sonde_is_between_b2_and_s10():
    text = run_text()
    sonde = text.index("api assign_pre")
    assert text.index("bug-b2-offer-stops") < sonde, "sonde PRE avant la fin du bloc B2"
    assert sonde < text.index("# S10:"), "sonde PRE après le commentaire S10"


@case
def test_marker_between_declines_and_rider_capture():
    text = run_text()
    mark = text.index("mark diag-driver-assign")
    assert text.index("[ \"$declines_pre\" -lt 0 ]") < mark, "marqueur avant la fenêtre S10"
    assert mark < text.index("cap rider-driver-assigned"), "marqueur après la capture rider"
    # les deux seules tentatives api accept restent avant le marqueur
    assert max(i for i in range(len(text)) if text.startswith("api accept", i)) < mark


@case
def test_reprise_is_a_log_after_the_report_never_a_mark():
    text = run_text()
    report = text.rindex('"$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"')
    reprise = text.index("diag-driver-assign:", report)
    assert "asg=" in text[reprise:reprise + 300], text[reprise:reprise + 300]
    # jamais de mark après le dernier flush : le digest est déjà écrit
    assert text.rindex("flush_results") > text.index("mark diag-driver-assign")
    assert text.count("diag-driver-assign:") == 1, text.count("diag-driver-assign:")


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test harness
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} diag-driver-assign cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
