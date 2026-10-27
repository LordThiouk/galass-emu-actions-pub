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


def extract_fn(name):
    """Whole `name() { ... }` definition, brace-counted (braces are balanced here)."""
    body = run_text()
    start = re.search(r"(?m)^%s\(\) \{" % re.escape(name), body)
    assert start, f"{name}() introuvable"
    depth = 0
    for i in range(start.end() - 1, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                return body[start.start():i + 1]
    raise AssertionError(f"{name}() non fermée")


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
    # Rider : lancement simple + wait_fg. Driver : démarrage PROUVÉ (ISC-4/5) —
    # `launch_fg` puis `wait_app_ui` sur le marqueur d'accueil inchangé. Le littéral
    # `launch "$DRIVER_PKG"; wait_fg ...` du run 40375590679 est ce qui laissait
    # passer un écran Réglages en premier plan : il ne doit plus exister.
    assert re.search(r'launch "\$RIDER_PKG"; wait_fg "\$RIDER_PKG" "\$WAIT_FG_SEC"', run), \
        "$RIDER_PKG"
    assert re.search(r'launch_fg "\$DRIVER_PKG" "\$WAIT_FG_SEC"', run), "$DRIVER_PKG (launch_fg)"
    assert re.search(r'wait_app_ui "\$DRIVER_PKG" "\$\{DRIVER_ONLINE_WAIT_SEC:-120\}" '
                     r'"\$MARK_driver_home_online"', run), "$DRIVER_PKG (wait_app_ui)"
    assert not re.search(r'(?m)^\s*launch "\$DRIVER_PKG"', run), \
        "le Driver ne doit plus être lancé sans preuve de premier plan"


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
    for needle in ("flush_results", "write_flags", "exit 1",
                   'accept_via=$ACCEPT_VIA cause=$ACCEPT_CAUSE'):
        assert needle in block, (needle, block)
    assert "api et ui indisponibles" not in block, block


@case
def test_success_accept_mark_carries_the_measured_cause():
    """The PASS detail must print ACCEPT_CAUSE, never a hardcoded `cause=none`."""
    body = run_text()
    start = body.index('if [ "$ACCEPT_OK" = "1" ]; then')
    m = re.search(r'case "\$ACCEPT_VIA" in(.*?)^\s*esac$', body[start:], re.M | re.S)
    assert m, body[start:start + 600]

    tmp = tempfile.mkdtemp(prefix="e2e-accept-pass-")
    env = dict(os.environ)
    env.update({"OUT": tmp, "ACCEPT_VIA": "api",
                "ACCEPT_CAUSE": "ui_tap_unproven_then_api"})
    script = (
        'mark() { printf "%s|%s|%s\n" "$1" "$2" "$3" >> "$OUT/marks.txt"; }\n'
        'eval "$1"\n'
    )
    proc = subprocess.run(["bash", "-c", script, "_", m.group(0)],
                          capture_output=True, text=True, env=env, cwd=tmp)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    marks = open(os.path.join(tmp, "marks.txt"), encoding="utf-8").read()
    assert "cause=ui_tap_unproven_then_api" in marks, marks
    assert "cause=none" not in marks, marks
    assert marks.startswith("accept-via|PASS|accept_via=api "), marks

    # no measured cause: the honest fallback stays `none`
    tmp = tempfile.mkdtemp(prefix="e2e-accept-pass-")
    env.update({"OUT": tmp, "ACCEPT_CAUSE": ""})
    proc = subprocess.run(["bash", "-c", script, "_", m.group(0)],
                          capture_output=True, text=True, env=env, cwd=tmp)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    marks = open(os.path.join(tmp, "marks.txt"), encoding="utf-8").read()
    assert "cause=none" in marks, marks


@case
def test_fail_detail_reports_only_measured_values_and_one_label():
    body = run_text()
    blk = re.search(r'if \[ "\$ACCEPT_OK" != "1" \]; then(.*?)^fi$', body, re.M | re.S)
    assert blk, "bloc d'échec d'acceptation introuvable"
    detail = re.search(r'mark accept-via FAIL "([^"]*)"', blk.group(1))
    assert detail, blk.group(1)
    assert detail.group(1) == "accept_via=$ACCEPT_VIA cause=$ACCEPT_CAUSE", detail.group(1)
    # exactly one accept_via= label in the logs, one measured value in the marks
    logs = re.findall(r'^\s*log "accept_via=', body, re.M)
    assert len(logs) == 1, logs
    assert re.search(r'mark accept-via (?:PASS|WARN|FAIL) "accept_via=\$ACCEPT_VIA', body), body


def run_api_flow(script, state, extra_env=None):
    """Import the REAL api_flow.py against a temp OUT and run `script`."""
    tmp = tempfile.mkdtemp(prefix="e2e-apiflow-")
    if state is not None:
        with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    env = dict(os.environ)
    env.update({"BASE": "https://staging.example.test", "OUT": tmp, "STEP": "ping",
                "RIDER_TOKEN": "r", "DRIVER_TOKEN": "d", "DEV_TOKEN": "x"})
    env.pop("GALASS_STAGING_URL", None)
    env.pop("E2E_GEOFENCE_OK", None)
    env.update(extra_env or {})
    # STEP=ping runs one HTTP call at import: stub urllib so no socket is opened.
    stub = (
        "import sys, json, urllib.request\n"
        "class _R:\n"
        "    status = 200\n"
        "    def read(self): return b'{\"data\":{}}'\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n"
        "urllib.request.urlopen = lambda *a, **k: _R()\n"
        "sys.path.insert(0, %r)\n"
        "import api_flow as a\n" % ROOT
    )
    driver = stub + script
    proc = subprocess.run([sys.executable, "-c", driver], capture_output=True,
                          text=True, env=env, cwd=os.path.dirname(ROOT))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return tmp, proc.stdout


@case
def test_status_never_keeps_a_previous_rides_stops():
    state = {"ride_id": "R2", "stops": [{"latitude": 1.0, "longitude": 2.0}],
             "stops_ride": "R1"}
    script = (
        "fresh = [{\"latitude\": 14.71, \"longitude\": -17.46}]\n"
        "a.driver_details = lambda: {\"stops\": fresh}\n"
        "a.details = lambda: {\"status\": \"running\"}\n"
        "a.call = lambda *x, **k: (200, {\"data\": {\"total\": 0}})\n"
        "a.status()\n"
        "st = json.load(open(a.state_f))\n"
        "print('STOPS', json.dumps(st['stops']), st.get('stops_ride'))\n"
    )
    _tmp, out = run_api_flow(script, state)
    line = [l for l in out.splitlines() if l.startswith("STOPS ")][0]
    assert "14.71" in line, line
    assert "1.0" not in line, f"les arrêts de la course précédente survivent: {line}"
    assert line.endswith(" R2"), line


@case
def test_driver_details_tags_the_stops_with_the_ride_they_belong_to():
    script = (
        "ride = {\"id\": \"R3\", \"stops\": [{\"latitude\": 14.7, \"longitude\": -17.4}]}\n"
        "a.call = lambda *x, **k: (200, {\"data\": {\"ride\": ride}})\n"
        "a.driver_details()\n"
        "st = json.load(open(a.state_f))\n"
        "print('TAG', st.get('stops_ride'), len(st['stops']))\n"
    )
    _tmp, out = run_api_flow(script, {"ride_id": "R3"})
    line = [l for l in out.splitlines() if l.startswith("TAG ")][0]
    assert line == "TAG R3 1", line


@case
def test_loc_stop_posts_the_state_stops_and_refuses_anything_else():
    """The backend must be sent the SAME stop the geofence verified."""
    state = {"ride_id": "R1", "stops_ride": "R1", "stops": [
        {"latitude": "14.7100", "longitude": "-17.4620"},
        {"latitude": 14.7020, "longitude": -17.4550},
    ]}
    script = (
        "sent = []\n"
        "a.driver_loc = lambda lat, lng: sent.append((lat, lng))\n"
        "ok0 = a.loc_stop(0)\n"
        "ok1 = a.loc_stop(1)\n"
        "print('SENT', json.dumps(sent), ok0, ok1)\n"
        "print('UNKNOWN', a.loc_stop(9), a.loc_stop(-1), a.loc_stop(1.5))\n"
        "print('AFTER', json.dumps(sent))\n"
    )
    _tmp, out = run_api_flow(script, state)
    lines = {l.split(" ", 1)[0]: l.split(" ", 1)[1] for l in out.splitlines()
             if l.split(" ", 1)[0] in ("SENT", "AFTER")}
    sent = json.loads(lines["SENT"].rsplit(" ", 2)[0])
    assert sent == [[14.71, -17.462], [14.702, -17.455]], sent
    assert lines["SENT"].endswith("True True"), lines["SENT"]
    assert "UNKNOWN False False False" in out, out
    # a refused index must not inject a fix
    assert json.loads(lines["AFTER"]) == sent, (lines, sent)


@case
def test_loc_stop_refuses_a_stop_without_numeric_coordinates():
    state = {"ride_id": "R1", "stops_ride": "R1",
             "stops": [{"latitude": "abc", "longitude": None},
                       {"longitude": -17.45}]}
    script = (
        "sent = []\n"
        "a.driver_loc = lambda lat, lng: sent.append((lat, lng))\n"
        "print('RESULT', a.loc_stop(0), a.loc_stop(1), a.loc_stop(0), len(sent))\n"
    )
    _tmp, out = run_api_flow(script, state)
    assert "RESULT False False False 0" in out, out


@case
def test_loc_stop_never_uses_the_hardcoded_stops_list():
    body = api_text()
    fn = re.search(r"^def loc_stop\(index\):(.*?)^def ", body, re.M | re.S)
    assert fn, "loc_stop() introuvable"
    assert "stop_coords(" in fn.group(1), fn.group(1)
    assert "STOPS" not in fn.group(1), fn.group(1)
    helper = re.search(r"^def stop_coords\(index\):(.*?)^def ", body, re.M | re.S)
    assert helper, "stop_coords() introuvable"
    assert 'st.get("stops")' in helper.group(1), helper.group(1)
    assert "STOPS" not in helper.group(1), helper.group(1)


@case
def test_complete_stops_fails_closed_without_the_geofence():
    """STEP=stops|all must never POST a complete-stop it cannot vouch for."""
    state = {"ride_id": "R1", "stops_ride": "R1", "stops": [
        {"latitude": 14.7100, "longitude": -17.4620},
        {"latitude": 14.7020, "longitude": -17.4550},
    ]}
    script = (
        "posts = []\n"
        "sent = []\n"
        "a.call = lambda m, path, *x, **k: (posts.append(path), (200, {'remark': 'stop_completed'}))[1]\n"
        "a.driver_loc = lambda lat, lng: sent.append((lat, lng))\n"
        "print('RESULT', a.complete_stops(), len(posts), len(sent))\n"
    )
    _tmp, out = run_api_flow(script, state)
    assert "RESULT False 0 0" in out, out

    # with the geofence asserted, it completes the STATE stops — not a copy
    script = (
        "posts = []\n"
        "sent = []\n"
        "a.call = lambda m, path, *x, **k: (posts.append(path), (200, {'remark': 'stop_completed'}))[1]\n"
        "a.driver_loc = lambda lat, lng: sent.append((lat, lng))\n"
        "print('RESULT', a.complete_stops(), len(posts), json.dumps(sent))\n"
    )
    tmp = tempfile.mkdtemp(prefix="e2e-apiflow-")
    with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    env = dict(os.environ)
    env.update({"BASE": "https://staging.example.test", "OUT": tmp, "STEP": "ping",
                "RIDER_TOKEN": "r", "DRIVER_TOKEN": "d", "DEV_TOKEN": "x",
                "E2E_GEOFENCE_OK": "1"})
    env.pop("GALASS_STAGING_URL", None)
    stub = (
        "import sys, json, urllib.request\n"
        "class _R:\n"
        "    status = 200\n"
        "    def read(self): return b'{\"data\":{}}'\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n"
        "urllib.request.urlopen = lambda *a, **k: _R()\n"
        "sys.path.insert(0, %r)\n"
        "import api_flow as a\n" % ROOT
    )
    proc = subprocess.run([sys.executable, "-c", stub + script], capture_output=True,
                          text=True, env=env, cwd=os.path.dirname(ROOT))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")][0]
    assert line == 'RESULT True 2 [[14.71, -17.462], [14.702, -17.455]]', line


@case
def test_stop_one_fails_closed_without_the_geofence():
    """`stop_one` completes a stop too: same gate as `stops`, fail closed."""
    state = {"ride_id": "R1", "stops_ride": "R1", "stops": [
        {"latitude": 14.7100, "longitude": -17.4620}]}
    script = (
        "posts = []\n"
        "sent = []\n"
        "a.call = lambda m, path, *x, **k: (posts.append(path), (200, {'remark': 'stop_completed'}))[1]\n"
        "a.driver_loc = lambda lat, lng: sent.append((lat, lng))\n"
        "a.driver_details = lambda: {'stops': [{'id': 11, 'latitude': 14.71, "
        "'longitude': -17.462}]}\n"
        "print('RESULT', a.stop_one(), len(posts), len(sent))\n"
    )
    _tmp, out = run_api_flow(script, state)
    assert "RESULT False 0 0" in out, out

    # with the geofence asserted (run.sh scopes it to this one call) it completes
    _tmp, out = run_api_flow(script, state, extra_env={"E2E_GEOFENCE_OK": "1"})
    assert "RESULT True 1 1" in out, out


@case
def test_stops_pending_fails_closed_without_the_geofence():
    """STEP=stops_pending completes every pending stop: third and last site, same gate."""
    state = {"ride_id": "R1", "stops_ride": "R1", "stops": [
        {"latitude": 14.7100, "longitude": -17.4620}]}
    script = (
        "posts = []\n"
        "sent = []\n"
        "a.call = lambda m, path, *x, **k: (posts.append(path), (200, {'remark': 'stop_completed'}))[1]\n"
        "a.driver_loc = lambda lat, lng: sent.append((lat, lng))\n"
        "a.driver_details = lambda: {'stops': [{'id': 11, 'latitude': 14.71, "
        "'longitude': -17.462}]}\n"
        "print('RESULT', a.complete_pending_stops(), len(posts), len(sent))\n"
    )
    _tmp, out = run_api_flow(script, state)
    assert "RESULT False 0 0" in out, out

    # with the geofence positively asserted by a hand-written flow it completes
    _tmp, out = run_api_flow(script, state, extra_env={"E2E_GEOFENCE_OK": "1"})
    assert "RESULT True 1 1" in out, out


@case
def test_every_complete_stop_post_lives_in_a_gated_function():
    """Class-sweep: no `complete-stop` POST may sit outside a geofence-checking fn."""
    body = api_text()
    # a function body ends at the next `def` OR at any column-0 line (module code:
    # `step = ...`, the `elif` chain) — otherwise the last def swallows the handlers.
    fns = {m.group(1): m.group(3) for m in
           re.finditer(r"^def ([a-z_]+)\((.*?)\):(.*?)(?=^def |^[^\s]|\Z)", body,
                       re.M | re.S)}
    sites = {n: b for n, b in fns.items() if "driver/rides/complete-stop/" in b}
    assert sorted(sites) == ["complete_pending_stops", "complete_stops", "stop_one"], \
        sorted(sites)
    for name, blk in sites.items():
        assert re.search(r'os\.environ\.get\(GEOFENCE_ENV\) != "1"', blk), name
        assert "return False" in blk, name
    total = body.count("driver/rides/complete-stop/")
    assert total == sum(b.count("driver/rides/complete-stop/") for b in sites.values()), \
        "un `complete-stop` POST vit hors d'une fonction géofencée"
    assert re.search(r'elif step == "stops_pending":\s*\n\s*'
                     r'sys\.exit\(0 if complete_pending_stops\(\) else 1\)', body), \
        "le step stops_pending doit sortir en code non nul sur refus"


@case
def test_run_sh_scopes_the_geofence_gate_to_stop_one():
    body = run_text()
    assert len(re.findall(r"E2E_GEOFENCE_OK=1", body)) == 1, body
    assert re.search(r'STOP_INDEX=\$i E2E_GEOFENCE_OK=1 api stop_one', body), body
    assert "export E2E_GEOFENCE_OK" not in body, body


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


def run_label(accept_json=None, **flags):
    """Drive the REAL resolve_accept_label() from run.sh with fakes."""
    tmp = tempfile.mkdtemp(prefix="e2e-label-")
    if accept_json is not None:
        with open(os.path.join(tmp, "accept.json"), "w", encoding="utf-8") as fh:
            json.dump(accept_json, fh)
    env = dict(os.environ)
    env.update({"OUT": tmp, "PY": sys.executable})
    env.update({k: str(v) for k, v in flags.items()})
    script = (
        "set -uo pipefail\n"
        'log() { printf "%s\\n" "$*" >> "$OUT/run.log"; }\n'
        'set_accept_via() { printf "%s\\n" "$1" > "$OUT/accept-via.txt"; }\n'
        'eval "$1"\n'
        "resolve_accept_label\n"
        'printf "VIA=%s CAUSE=%s\\n" "$ACCEPT_VIA" "${ACCEPT_CAUSE:-}"\n'
    )
    # run.sh runs from the repo root: `e2e/offer_flow.py` is relative to it.
    proc = subprocess.run(["bash", "-c", script, "_", extract_fn("resolve_accept_label")],
                          capture_output=True, text=True, env=env,
                          cwd=os.path.dirname(ROOT))
    log = open(os.path.join(tmp, "run.log")).read() if os.path.exists(
        os.path.join(tmp, "run.log")) else ""
    sidecar = open(os.path.join(tmp, "accept-via.txt")).read().strip() if os.path.exists(
        os.path.join(tmp, "accept-via.txt")) else ""
    return proc, log, sidecar


@case
def test_resolve_accept_label_credits_ui_only_with_a_proven_tap():
    proc, log, sidecar = run_label(ACCEPT_OK=1, UI_TAP_PROVEN=1, UI_ACCEPT=1)
    assert proc.stdout.strip() == "VIA=ui CAUSE=", (proc.stdout, proc.stderr)
    assert log.count("accept_via=") == 1, log
    assert "accept_via=ui cause=none" in log, log
    assert sidecar == "ui", sidecar

    proc, log, _ = run_label(ACCEPT_OK=1, UI_TAP_PROVEN=0, UI_ACCEPT=1)
    assert proc.stdout.strip() == "VIA=unknown CAUSE=assigned_without_tap_proof", proc.stdout


@case
def test_resolve_accept_label_prefers_a_2xx_api_answer():
    proc, log, _ = run_label({"via": "api", "cause": None},
                             ACCEPT_OK=1, UI_TAP_PROVEN=1, UI_ACCEPT=1,
                             UI_TAP_FAILED=1)
    assert proc.stdout.strip() == "VIA=api CAUSE=ui_tap_unproven_then_api", proc.stdout
    assert log.count("accept_via=") == 1, log

    proc, _, _ = run_label({"via": "api", "cause": None},
                           ACCEPT_OK=1, UI_TAP_PROVEN=0, UI_ACCEPT=0)
    assert proc.stdout.strip() == "VIA=api CAUSE=api_2xx", proc.stdout


@case
def test_resolve_accept_label_reads_assigned_from_accept_json():
    # assigned by the backend, UI never proven -> assigned_without_tap_proof
    proc, log, _ = run_label({"ok": True, "via": "unknown", "cause": None},
                             ACCEPT_OK=1, UI_TAP_PROVEN=0, UI_ACCEPT=0)
    assert proc.stdout.strip() == "VIA=unknown CAUSE=assigned_without_tap_proof", proc.stdout
    # 4xx + not assigned: the ride is someone else's
    proc, log, _ = run_label({"ok": False, "via": "none", "cause": "respond_403"},
                             ACCEPT_OK=0, UI_TAP_PROVEN=0, UI_ACCEPT=0)
    assert proc.stdout.strip() == "VIA=failed CAUSE=respond_403", proc.stdout
    body = run_text()
    fn = extract_fn("resolve_accept_label")
    assert "--assigned" in fn, fn
    assert '"ok"' in fn, "le --assigned doit venir de accept.json .ok"


@case
def test_resolve_accept_label_reports_failed_with_the_api_cause():
    proc, log, sidecar = run_label({"via": "none", "cause": "offer_expired"},
                                   ACCEPT_OK=0)
    assert proc.stdout.strip() == "VIA=failed CAUSE=offer_expired", proc.stdout
    assert "accept_via=failed cause=offer_expired" in log, log
    assert sidecar == "failed", sidecar


# --------------------------------------------------------- accept label
@case
def test_accept_label_table():
    """One unambiguous label from the proofs we actually hold."""
    f = flow()
    # api wins, and says whether a UI tap failed first
    assert f.accept_label(0, 0, True, True) == {"via": "api", "cause": "api_2xx"}, \
        f.accept_label(0, 0, True, True)
    assert f.accept_label(1, 1, True, True, ui_tap_failed=True) == \
        {"via": "api", "cause": "ui_tap_unproven_then_api"}
    # ui needs BOTH the tap and the screen
    assert f.accept_label(1, 1, False, False, assigned=True) == {"via": "ui", "cause": None}
    # `assigned` (accept.json .ok) separates "ours but unproven" from "not ours"
    assert f.accept_label(0, 1, False, False, assigned=True) == \
        {"via": "unknown", "cause": "assigned_without_tap_proof"}
    assert f.accept_label(0, 1, False, False, assigned=False) == \
        {"via": "unknown", "cause": "not_assigned"}
    assert f.accept_label(1, 0, False, False, assigned=False) == \
        {"via": "unknown", "cause": "not_assigned"}
    assert f.accept_label(0, 0, False, False) == \
        {"via": "unknown", "cause": "not_assigned"}
    # an "ok" that is not 2xx is not an API accept
    assert f.accept_label(0, 0, True, False) == \
        {"via": "unknown", "cause": "not_assigned"}


@case
def test_accept_label_4xx_unassigned_is_not_assigned():
    """A 4xx accept left the ride to someone else: never `assigned_*`."""
    f = flow()
    assert f.accept_label(0, 0, True, False, assigned=False) == \
        {"via": "unknown", "cause": "not_assigned"}
    assert f.accept_label(0, 0, False, False, ui_tap_failed=True, assigned=False) == \
        {"via": "unknown", "cause": "not_assigned"}
    # precedence unchanged: a 2xx api answer still wins over everything
    assert f.accept_label(0, 0, True, True, assigned=False) == \
        {"via": "api", "cause": "api_2xx"}


@case
def test_accept_label_cli():
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "offer_flow.py"), "label",
         "--ui-tap-proven", "1", "--ui-screen-proven", "1",
         "--api-ok", "0", "--api-2xx", "0"],
        capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "via=ui cause=none", proc.stdout

    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "offer_flow.py"), "label",
         "--ui-tap-proven", "0", "--ui-screen-proven", "1",
         "--api-ok", "1", "--api-2xx", "1", "--ui-tap-failed", "1"],
        capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "via=api cause=ui_tap_unproven_then_api", proc.stdout

    # --assigned separates assigned-but-unproven from not assigned
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "offer_flow.py"), "label",
         "--ui-tap-proven", "0", "--ui-screen-proven", "1",
         "--api-ok", "0", "--api-2xx", "0", "--assigned", "1"],
        capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "via=unknown cause=assigned_without_tap_proof", proc.stdout

    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "offer_flow.py"), "label",
         "--ui-tap-proven", "0", "--ui-screen-proven", "0",
         "--api-ok", "0", "--api-2xx", "0", "--assigned", "0"],
        capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "via=unknown cause=not_assigned", proc.stdout


@case
def test_run_sh_logs_the_label_exactly_once():
    body = run_text()
    logs = re.findall(r'^\s*log "accept_via=', body, re.M)
    assert len(logs) == 1, logs
    assert re.search(r'^\s*log "accept_via=\$ACCEPT_VIA cause=', body, re.M), body
    assert "e2e/offer_flow.py label" in body, "le libellé doit venir d'accept_label"
    # the api fallback helper no longer publishes its own accept_via
    fn = re.search(r"^api_accept_ok\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert 'get("via") or "unknown"' in fn, fn
    assert 'log "accept_via' not in fn, fn


@case
def test_accept_json_records_the_http_code():
    body = api_text()
    write = re.search(r"^def write_accept\(out\):(.*?)^def ", body, re.M | re.S)
    assert write, "write_accept() introuvable"
    assert '"code"' in write.group(1), write.group(1)
    assert "out.get(\"code\")" in write.group(1), write.group(1)
    fn = re.search(r"^def run_accept\(.*?^\S", body, re.M | re.S)
    # run_accept lives in offer_flow.py; its result must carry the last code
    import offer_flow
    calls = {"n": 0}

    def respond():
        calls["n"] += 1
        return (403, {"remark": "not_authorized"}) if calls["n"] == 1 else (200, {"remark": "offer_accepted"})

    out = offer_flow.run_accept(respond, lambda: False, lambda: False, lambda _s: None,
                                retries=3, delay=1)
    assert out["code"] == 200, out


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
