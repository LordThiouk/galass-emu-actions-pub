#!/usr/bin/env python3
"""TDD spec for the offer/accept hardening (run 40205373538).

No emulator, no network. Pins five contracts:
  * the pure rules of e2e/offer_flow.py: classify an offer response, renew an
    expired offer AT MOST ONCE, then return an explicit cause;
  * `wait_fg` is an ACTIVE wait: it returns as soon as fg_pkg matches, its
    default bound is 600 s (`WAIT_FG_SEC`), and no launch waits a fixed 60 s;
  * UI-first order in run.sh: Artemis accept < tap "Accepter la course" < API;
  * the offer budget `OFFER_TIMEOUT_SEC - OFFER_SAFETY_SEC`, where
    OFFER_TIMEOUT_SEC is a READ-ONLY mirror (never exported, never sent);
  * `accept_via` is published in run.log, ui-flags.txt and the report.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
UI = os.path.join(ROOT, "ui.sh")
API = os.path.join(ROOT, "api_flow.py")
sys.path.insert(0, ROOT)

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_text():
    return open(RUN, encoding="utf-8").read()


def ui_text():
    return open(UI, encoding="utf-8").read()


def api_text():
    return open(API, encoding="utf-8").read()


def flow():
    """Import the pure helper lazily: a missing module is one red case, not a crash."""
    import offer_flow
    return offer_flow


def body_between(text, start, end):
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


# ------------------------------------------------------- pure offer rules
@case
def test_classify_offer_responses():
    f = flow()
    cases = [
        (200, {"status": "success", "remark": "offer_accepted"}, f.ACCEPTED),
        (200, {"status": "success"}, f.ACCEPTED),
        (200, {"remark": "offer_expired"}, f.EXPIRED),
        (403, {"remark": "not_authorized"}, f.NOT_AUTHORIZED),
        (409, {"remark": "ride_unavailable"}, f.RIDE_UNAVAILABLE),
        (500, {"error": "boom"}, f.TERMINAL),
        (0, {"error": "timed out"}, f.TERMINAL),
        (200, None, f.TERMINAL),
    ]
    got = [(code, body, f.classify(code, body)) for code, body, _ in cases]
    bad = [(c, b, k) for c, b, want in cases for k in [f.classify(c, b)] if k != want]
    assert not bad, f"classifications fausses: {bad} (attendu {got})"


@case
def test_decide_renews_at_most_once():
    f = flow()
    assert f.decide(f.ACCEPTED, False) == "accept"
    assert f.decide(f.EXPIRED, False) == "renew", "une offre expirée non renouvelée doit être renouvelée"
    assert f.decide(f.EXPIRED, True) == "fail", "jamais deux renouvellements"
    assert f.decide(f.NOT_AUTHORIZED, False) == "retry"
    assert f.decide(f.RIDE_UNAVAILABLE, True) == "retry"
    assert f.decide(f.TERMINAL, False) == "fail"


@case
def test_failure_cause_is_explicit():
    f = flow()
    assert f.failure_cause(f.EXPIRED, True) == "offer_expired"
    assert f.failure_cause(f.EXPIRED, False) == "offer_expired"
    assert f.failure_cause(f.NOT_AUTHORIZED, False) == "not_authorized"
    assert f.failure_cause(f.RIDE_UNAVAILABLE, False) == "ride_unavailable"
    assert f.failure_cause(f.TERMINAL, False) == "terminal"
    assert f.failure_cause(f.ACCEPTED, False) == ""


def _accept_harness(kinds, renew_ok=True, assigned=False, renewed=False, retries=5,
                    tap_proven=False):
    f = flow()
    queue = list(kinds)
    calls = {"respond": 0, "renew": 0, "sleep": 0}
    remarks = {
        f.ACCEPTED: "offer_accepted",
        f.EXPIRED: "offer_expired",
        f.NOT_AUTHORIZED: "not_authorized",
        f.RIDE_UNAVAILABLE: "ride_unavailable",
    }

    def respond():
        calls["respond"] += 1
        kind = queue.pop(0) if queue else f.TERMINAL
        remark = remarks.get(kind)
        return (200 if remark else 500), ({"remark": remark} if remark else {"error": "boom"})

    def _assigned():
        return assigned

    def renew():
        calls["renew"] += 1
        return renew_ok

    def _sleep(_seconds):
        calls["sleep"] += 1

    out = f.run_accept(respond, _assigned, renew, _sleep,
                       retries=retries, delay=1, renewed=renewed,
                       tap_proven=tap_proven)
    return out, calls


@case
def test_expired_then_accepted_renews_once_and_succeeds():
    f = flow()
    out, calls = _accept_harness([f.EXPIRED, f.ACCEPTED])
    assert out["ok"] is True, out
    assert out["renewed"] is True and out["cause"] is None, out
    assert calls["renew"] == 1, calls
    assert calls["respond"] == 2, calls


@case
def test_second_expiration_fails_without_looping():
    f = flow()
    out, calls = _accept_harness([f.EXPIRED, f.EXPIRED, f.EXPIRED])
    assert out["ok"] is False, out
    assert out["cause"] == "offer_expired", out
    assert calls["renew"] == 1, f"renouvellement multiple: {calls}"
    assert calls["respond"] == 2, f"boucle sur la même offre: {calls}"
    assert calls["sleep"] == 0, f"retry sur une offre expirée: {calls}"


@case
def test_already_renewed_expiration_fails_immediately():
    f = flow()
    out, calls = _accept_harness([f.EXPIRED], renewed=True)
    assert out["ok"] is False and out["cause"] == "offer_expired", out
    assert calls["renew"] == 0, calls
    assert calls["respond"] == 1, calls


@case
def test_failed_renewal_is_an_explicit_expiration():
    f = flow()
    out, calls = _accept_harness([f.EXPIRED], renew_ok=False)
    assert out["ok"] is False and out["cause"] == "offer_expired", out
    assert calls["renew"] == 1 and calls["respond"] == 1, calls


@case
def test_transient_kinds_retry_within_the_attempt_budget():
    f = flow()
    out, calls = _accept_harness([f.NOT_AUTHORIZED, f.RIDE_UNAVAILABLE, f.ACCEPTED], retries=5)
    assert out["ok"] is True, out
    assert calls["respond"] == 3 and calls["sleep"] == 2, calls

    out, calls = _accept_harness([f.NOT_AUTHORIZED] * 9, retries=3)
    assert out["ok"] is False and out["cause"] == "not_authorized", out
    assert calls["respond"] == 3, f"budget de tentatives dépassé: {calls}"


@case
def test_terminal_fails_fast_with_cause():
    f = flow()
    out, calls = _accept_harness([f.TERMINAL])
    assert out["ok"] is False and out["cause"] == "terminal", out
    assert calls["respond"] == 1 and calls["renew"] == 0 and calls["sleep"] == 0, calls


@case
def test_already_assigned_driver_is_an_accept():
    f = flow()
    out, calls = _accept_harness([f.NOT_AUTHORIZED], assigned=True)
    assert out["ok"] is True, out
    assert calls["respond"] == 1, calls


@case
def test_assigned_means_ui_only_with_tap_proof():
    """Assigned is success, but the credit is UI only when OUR tap is proven."""
    f = flow()
    out, _ = _accept_harness([f.NOT_AUTHORIZED], assigned=True)
    assert out["ok"] is True and out["via"] == "unknown", out
    assert out["cause"] == "respond_200_assigned", out
    out, _ = _accept_harness([f.NOT_AUTHORIZED], assigned=True, tap_proven=True)
    assert out["ok"] is True and out["via"] == "ui" and out["cause"] is None, out
    out, _ = _accept_harness([f.ACCEPTED])
    assert out["ok"] is True and out["via"] == "api", out
    out, _ = _accept_harness([f.TERMINAL])
    assert out["ok"] is False and out["via"] is None, out


@case
def test_accepted_via_mapping_is_pure():
    f = flow()
    assert f.accepted_via(200, f.ACCEPTED, False, False) == "api"
    assert f.accepted_via(403, f.NOT_AUTHORIZED, True, True) == "ui"
    assert f.accepted_via(403, f.NOT_AUTHORIZED, True, False) == "unknown"
    assert f.accepted_via(403, f.NOT_AUTHORIZED, False, True) is None
    # Assigné sans preuve de tap : jamais un crédit UI inventé.
    assert f.accepted_via(200, f.EXPIRED, True, False) == "unknown"
    assert f.accepted_via(200, f.EXPIRED, False, False) is None
    # Un accepted sur un code non-2xx n'est pas une acceptation API.
    assert f.accepted_via(500, f.ACCEPTED, False, False) is None


# ------------------------------------------------------------- wait_fg
WAIT_UI_ENV = None


def wait_fg_env(fg_pkg):
    """tmp OUT + a stub adb that reports `fg_pkg` as the focused app."""
    tmp = tempfile.mkdtemp(prefix="e2e-waitfg-")
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir)
    adb_log = os.path.join(tmp, "adb.log")
    stub = os.path.join(bin_dir, "adb")
    with open(stub, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\n"
                 "printf '%s\\n' \"$*\" >> \"$ADB_LOG\"\n"
                 "case \"$*\" in\n"
                 "  *dumpsys\\ window*)\n"
                 "    printf 'mCurrentFocus=Window{1 u0 %s/%s.MainActivity}\\n' \"$FG_PKG\" \"$FG_PKG\" ;;\n"
                 "esac\n")
    os.chmod(stub, 0o755)
    env = dict(os.environ,
               OUT=tmp,
               SMOKE_LOG=os.path.join(tmp, "smoke.log"),
               PATH=bin_dir + os.pathsep + os.environ["PATH"],
               ADB_LOG=adb_log, FG_PKG=fg_pkg)
    return tmp, env


@case
def test_wait_fg_returns_as_soon_as_the_app_is_foreground():
    _tmp, env = wait_fg_env("sn.galass.galassrider")
    t0 = time.time()
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; wait_fg sn.galass.galassrider', "_", UI],
        capture_output=True, text=True, env=env)
    elapsed = time.time() - t0
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert elapsed < 20, f"wait_fg n'est pas actif ({elapsed:.1f}s)"


@case
def test_wait_fg_fails_only_after_its_bound():
    _tmp, env = wait_fg_env("sn.galass.galassrider")
    t0 = time.time()
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; wait_fg sn.galass.galassdriver 1', "_", UI],
        capture_output=True, text=True, env=env)
    elapsed = time.time() - t0
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert elapsed >= 1, f"wait_fg n'a pas attendu sa borne ({elapsed:.1f}s)"


@case
def test_wait_fg_default_is_ten_minutes_and_used_by_launches():
    both = run_text() + ui_text()
    m = re.search(r'WAIT_FG_SEC="\$\{WAIT_FG_SEC:-(\d+)\}"', both)
    assert m, "WAIT_FG_SEC avec un défaut introuvable"
    assert int(m.group(1)) == 600, m.group(0)
    fn = re.search(r"^wait_fg\(\) \{(.*?)^\}", ui_text(), re.M | re.S)
    assert fn, "wait_fg() doit vivre dans ui.sh"
    assert "fg_pkg" in fn.group(1) and "return 0" in fn.group(1), fn.group(1)
    assert not re.search(r"\bwait_fg\b[^\n]*\s\b60\b", both), \
        "un lancement attend encore 60 s fixes (wait_fg ... 60)"
    run = run_text()
    for pkg in ("$RIDER_PKG", "$DRIVER_PKG"):
        assert re.search(r'launch "%s"; wait_fg "%s" "\$WAIT_FG_SEC"' % (re.escape(pkg), re.escape(pkg)), run), pkg


# ------------------------------------------------------ UI-first ordering
@case
def test_offer_flow_is_ui_first_then_api():
    body = run_text()
    i_art = body.index("artemis_run driver-accept")
    i_tap = body.index('tap_exact "Accepter la course"')
    i_api = re.search(r"^\s*api accept ", body, re.M).start()  # la COMMANDE, pas un commentaire
    assert i_art < i_tap < i_api, (i_art, i_tap, i_api)
    # Artemis accept is bounded by the UI budget (offer_left minus the API
    # reserve), never by a constant and never by the FULL remaining window.
    section = body[i_art:i_tap]
    assert '"$ui_left"' in section and "offer_left" in body[:i_art], section[:400]
    # The FAIL mark now belongs to the terminal failure branch (see the dedicated
    # case), so the contract is asserted where it lives, not next to `api accept`.
    assert "api fallback: accept" in body, "detail du repli API perdu"
    assert re.search(r'^\s*mark driver-accepted-details FAIL "api fallback: accept"', body, re.M), body


# ------------------------------------------- ISC-5a : réserve du repli API
@case
def test_artemis_never_eats_the_api_fallback_reserve():
    """Artemis (et le wait_ui du tap) vivent sur `ui_left`, jamais sur tout `left`."""
    body = run_text()
    m = re.search(r'API_RESERVE_SEC="\$\{API_RESERVE_SEC:-(\d+)\}"', body)
    assert m, "API_RESERVE_SEC (secondes réservées au repli API) manquant"
    assert int(m.group(1)) >= 1, m.group(0)

    i_art = body.index("artemis_run driver-accept")
    i_tap = body.index('tap_exact "Accepter la course"')
    before, section = body[:i_art], body[i_art:i_tap]
    # ui_left = restant - réserve ; Artemis consomme ui_left, pas left.
    assert re.search(r"ui_left=\$\(\(\s*\$?\(?offer_left\)?\s*-\s*API_RESERVE_SEC\s*\)\)", before) \
        or re.search(r"ui_left=\$\(\(\s*left\s*-\s*API_RESERVE_SEC\s*\)\)", before), before[-800:]
    assert '"$ui_left" "$DRIVER_PKG"' in section, section
    assert not re.search(r'artemis_run driver-accept[\s\S]{0,240}?"\$left"', section), section

    # Les commentaires citent aussi `api accept` : on ancre sur la COMMANDE.
    i_api = re.search(r"^\s*api accept ", body, re.M).start()
    accept = body[i_art:i_api]
    assert re.search(r'wait_ui "\$DRIVER_PKG" "\$ui_left"', accept), accept[-600:]
    assert not re.search(r'wait_ui "\$DRIVER_PKG" "\$\(offer_left\)"', body), \
        "le tap attend encore toute la fenêtre (réserve API mangée)"

    # ui_left <= 0 : Artemis sauté (rc=2), pas de timeout 0 ; la réserve reste au repli.
    skip = re.search(r'if \[ "\$ui_left" -gt 0 \]; then(.*?)^fi$', body, re.M | re.S)
    assert skip, "garde ui_left > 0 autour d'Artemis introuvable"
    assert "ARTEMIS_ACCEPT_RC=2" in skip.group(1), skip.group(1)
    assert "artemis skip: driver-accept" in skip.group(1), skip.group(1)

    # Le budget UI est RECALCULÉ après Artemis : sans ça, Artemis a déjà mangé la réserve.
    recompute = re.search(r"ui_left=\$\(\(\s*\$\(offer_left\)\s*-\s*API_RESERVE_SEC\s*\)\)",
                          body[i_art:i_tap])
    assert recompute, "aucun recalcul de ui_left après Artemis (réserve API mangée)"
    assert i_art < i_art + recompute.start() < i_tap, (i_art, recompute.start(), i_tap)


# ------------------------------------------------- S6 : purge inter-runs
@case
def test_startup_purge_removes_accept_artifacts_from_a_previous_run():
    """Comportemental: le VRAI bloc `rm -rf` de run.sh est exécuté sur un OUT périmé."""
    body = run_text()
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("rm -rf ")), None)
    assert start is not None, "bloc S6 `rm -rf` introuvable dans run.sh"
    end = start
    while lines[end].endswith("\\"):   # continuations de ligne du bloc
        end += 1
    purge = "\n".join(lines[start:end + 1])
    tmp = tempfile.mkdtemp(prefix="e2e-purge-")
    os.makedirs(os.path.join(tmp, "screens"))
    with open(os.path.join(tmp, "accept.json"), "w", encoding="utf-8") as fh:
        fh.write('{"ok": true, "via": "ui"}')
    with open(os.path.join(tmp, "accept-via.txt"), "w", encoding="utf-8") as fh:
        fh.write("ui\n")
    control = os.path.join(tmp, "screens", "x.png")
    with open(control, "wb") as fh:
        fh.write(b"\x89PNG")

    proc = subprocess.run(["bash", "-c", purge], capture_output=True, text=True,
                          env=dict(os.environ, OUT=tmp))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    # Control: le purge a réellement tourné (un vieil écran disparaît aussi).
    assert not os.path.exists(control), "le purge S6 n'a pas tourné (screens/x.png survivant)"
    for name in ("accept.json", "accept-via.txt"):
        assert not os.path.exists(os.path.join(tmp, name)), \
            f"{name} d'un run précédent a survécu au purge S6 (verdict pollué)"
        # La liste doit rester littérale : un refactor qui en retire un doit casser ici.
        assert f'"$OUT"/{name}' in purge, f'"$OUT"/{name} absent de la liste S6'


# ------------------------------------------------ ISC-5b : T0 non optimiste
@case
def test_offer_t0_is_anchored_before_the_pick():
    """T0 vient du created_at de la course, sinon du DÉBUT du bloc (pas du pick)."""
    body = run_text()
    fn = re.search(r"^offer_t0\(\) \{(.*?)^\}", body, re.M | re.S)
    assert fn, "helper offer_t0() introuvable"
    helper = fn.group(1)
    for needle in ("ride_picked", "ride_create", "created_at", "step_start"):
        assert needle in helper, (needle, helper)
    assert "fromisoformat" in helper or "strptime" in helper, \
        "le created_at ISO-8601 doit être accepté"
    assert "/ 1000" in helper or "1e12" in helper, \
        "le created_at en millisecondes doit être accepté"

    # Le repère conservateur est pris AVANT bring_fg/api pick.
    i_step = body.index("OFFER_STEP_START=$SECONDS")
    i_pick = re.search(r"^api pick ", body, re.M).start()
    assert i_step < i_pick, (i_step, i_pick)
    # Plus jamais l'horloge post-pick comme T0.
    assert "OFFER_T0=$SECONDS" not in body[i_pick - 40:i_pick + 600], body[i_pick:i_pick + 600]
    # Une seule ligne d'audit, avec la source.
    assert body.count("offer_t0 source=") == 1, body.count("offer_t0 source=")
    assert re.search(r'log "offer_t0 source=\$OFFER_T0_SOURCE left=\$\(offer_left\)s"', body), \
        "ligne offer_t0 source=... left=...s manquante"


@case
def test_offer_t0_helper_parses_epoch_ms_iso_and_falls_back():
    """Probe comportemental: le helper est évalué pour de vrai, pas seulement greppé."""
    import shlex
    body = run_text()
    fn = re.search(r"^offer_t0\(\) \{(?:.*?)^\}", body, re.M | re.S).group(0)
    tmp = tempfile.mkdtemp(prefix="e2e-t0-")
    now = int(time.time())
    cases = {
        "epoch": (json.dumps({"ride_picked": {"created_at": now - 100}}), True),
        "ms": (json.dumps({"ride_create": {"created_at": (now - 100) * 1000}}), True),
        "iso": (json.dumps({"ride_picked": {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 100))}}), True),
        "absent": (json.dumps({"ride_picked": {}}), False),
        "garbage": (json.dumps({"ride_picked": {"created_at": "pas-une-date"}}), False),
    }
    for name, (payload, dated) in cases.items():
        path = os.path.join(tmp, name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        # SECONDS=500 (horloge du script) ; created_at = il y a ~100 s.
        script = f"SECONDS=500\n{eval_fn(fn)}\noffer_t0 {shlex.quote(path)} 1000\n"
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=dict(os.environ, PY=sys.executable, OUT=tmp))
        assert proc.returncode == 0, (name, proc.stdout, proc.stderr)
        t0, source = proc.stdout.split()
        if dated:
            assert source == "created_at", (name, source)
            assert 390 <= int(t0) <= 400, (name, t0)
        else:
            assert (t0, source) == ("1000", "step_start"), (name, proc.stdout)


def eval_fn(fn):
    """`eval "<fn>"` : une fonction extraite de run.sh reste exécutable telle quelle."""
    import shlex
    return "eval " + shlex.quote(fn)


# -------------------------------- ISC-3 : un succès UI ne doit jamais rester FAIL
@case
def test_details_fail_mark_only_fires_when_the_fallback_fails():
    body = run_text()
    needle = "mark driver-accepted-details FAIL"
    assert body.count(needle) == 1, "un seul marquage FAIL, dans la branche terminale"
    i_fail = body.index(needle)
    i_guard = body.index('if [ "$ACCEPT_OK" != "1" ]; then')
    i_api = re.search(r"^\s*api accept ", body, re.M).start()
    assert i_guard < i_fail, (i_guard, i_fail)
    assert i_api < i_fail, "FAIL marqué avant le repli API : un succès via=ui resterait FAIL"
    blk = re.search(r'if \[ "\$ACCEPT_OK" != "1" \]; then(.*?)^fi$', body, re.M | re.S)
    assert blk and needle in blk.group(1), blk
    assert "api fallback: accept" in blk.group(1), blk.group(1)
    # Sur le succès, seule la capture 35 tranche le point.
    assert re.search(r'cap driver-accepted-details 35-driver-accepted-details', body), body


# --------------------------------------------------------- offer budget
@case
def test_offer_budget_is_read_only_and_bounded():
    body = run_text()
    assert re.search(r'OFFER_TIMEOUT_SEC="\$\{OFFER_TIMEOUT_SEC:-30\}"', body), \
        "OFFER_TIMEOUT_SEC doit refléter le backend (30 s) sans le modifier"
    assert re.search(r'OFFER_SAFETY_SEC="\$\{OFFER_SAFETY_SEC:-8\}"', body), body
    m = re.search(r'OFFER_CAPTURE_SEC="\$\{OFFER_CAPTURE_SEC:-(\d+)\}"', body)
    assert m, "OFFER_CAPTURE_SEC manquant (capture 34 courte)"
    assert 4 <= int(m.group(1)) <= 10, f"capture 34 hors bornes: {m.group(0)}"
    assert "OFFER_TIMEOUT_SEC - OFFER_SAFETY_SEC" in body, \
        "le budget doit être dérivé de la marge de sécurité"
    assert re.search(r'cap driver-offer-with-stops[^\n]*"\$OFFER_CAPTURE_SEC"', body), \
        "la capture 34 doit être bornée par OFFER_CAPTURE_SEC"
    # read-only mirror: never exported, never written, never sent to the backend
    assert "export OFFER_TIMEOUT_SEC" not in body, body
    assert "offer_timeout" not in api_text().lower(), \
        "api_flow.py ne doit jamais envoyer offer_timeout_sec"
    assert not re.search(r'POST[^\n]*general-setting', body), \
        "aucun POST admin/general-setting dans le harnais"


# ----------------------------------------------------------- accept_via
@case
def test_accept_via_is_published_in_log_flags_and_report():
    body = run_text()
    assert "accept_via=$ACCEPT_VIA" in body, "accept_via absent de run.log"
    assert re.search(r"^\s*mark accept-via FAIL ", body, re.M), \
        "ligne de résultats accept-via (échec) manquante"
    assert re.search(r"^\s*mark accept-via PASS ", body, re.M), \
        "accept-via doit aussi être marqué PASS sur le chemin succès"
    flags = re.search(r"^write_flags\(\) \{(.*?)^\}", body, re.M | re.S)
    assert flags and "ACCEPT_VIA=%s" in flags.group(1), flags
    assert "ACCEPT_VIA=" in body.split("write_flags() {")[0], \
        "ACCEPT_VIA doit être initialisé avant tout write_flags (set -u)"

    import report
    out = tempfile.mkdtemp(prefix="e2e-accept-via-")
    with open(os.path.join(out, "results-raw.txt"), "w", encoding="utf-8") as fh:
        fh.write("accept-via|PASS|accept_via=api cause=offer_expired renewed=1\n")
        fh.write("no-prod-calls|PASS|0 hit\n")
    with open(os.path.join(out, "ui-flags.txt"), "w", encoding="utf-8") as fh:
        fh.write("ACCEPT_VIA=api\n")
    res = report.build(out, p0={"no-prod-calls"})
    row = [p for p in res["points"] if p["point"] == "accept-via"]
    assert row, res["points"]
    assert "accept_via=api" in row[0]["detail"], row
    assert res["ui_flags"].get("ACCEPT_VIA") == "api", res["ui_flags"]
    assert "accept-via" in open(os.path.join(out, "summary.md"), encoding="utf-8").read()


@case
def test_failed_accept_flushes_verdict_and_exits_non_zero():
    body = run_text()
    blk = re.search(r'if \[ "\$ACCEPT_OK" != "1" \]; then(.*?)^fi$', body, re.M | re.S)
    assert blk, "bloc d'échec d'acceptation introuvable"
    block = blk.group(1)
    for needle in ("flush_results", "write_flags", "exit 1", "accept_via=api"):
        assert needle in block, (needle, block)


@case
def test_api_flow_writes_accept_json_and_renews_once():
    body = api_text()
    for key in ('"ok"', '"via"', '"cause"', '"renewed"', '"attempts"'):
        assert key in body, (key, body)
    assert "accept.json" in body, body
    assert 'out.get("via")' in body, "accept.json doit relayer le via réel (api/ui)"
    assert re.search(r'elif step == "accept":\s*\n\s*sys\.exit\(0 if accept_offer\(\) else 1\)', body), \
        "le step accept doit sortir en code non nul sur échec"
    renew = re.search(r"^def renew_offer\(\):(.*?)^def ", body, re.M | re.S)
    assert renew, "renew_offer() introuvable"
    block = renew.group(1)
    assert "ride/cancel" in block and "create_ride()" in block, block
    assert 'st["offer_renewed"] = 1' in block, "le renouvellement doit être marqué (une seule fois)"
    assert 'st["ride_source"] = "api"' in block, "la provenance UI est perdue après renouvellement"
    assert "run_accept(" in body, "la règle de renouvellement doit venir d'offer_flow"


# ------------------------------------------------------------------- main
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
    print(f"\n{len(CASES) - failed}/{len(CASES)} offer-flow cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
