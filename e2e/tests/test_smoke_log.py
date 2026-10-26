#!/usr/bin/env python3
"""TDD spec for the sanitized public smoke log.

Raw details stay in $OUT/run.log, which is shipped only inside the encrypted
evidence archive. Every copy that reaches stderr or e2e/smoke.log must be
sanitized first: tokens, passwords, keys and phone numbers are masked.

Run with `python3 e2e/tests/test_smoke_log.py`. No emulator, no network.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sanitize import sanitize  # noqa: E402

WORKFLOW = os.path.join(ROOT, "..", ".github", "workflows", "artemis-staging-e2e.yml")
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def text(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


# ------------------------------------------------------------------ masking
SECRETS = [
    ("Authorization: Bearer abcdef1234567890", ["abcdef1234567890"]),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", ["dXNlcjpwYXNzd29yZA=="]),
    ("dev-token=deadbeefcafebabe1234", ["deadbeefcafebabe1234"]),
    ('{"access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"}',
     ["eyJhbGciOiJIUzI1NiJ9"]),
    ('{"refresh_token":"r3fr3sh-s3cr3t","password": "hunter2"}', ["r3fr3sh-s3cr3t", "hunter2"]),
    ("api_key: sk-live-0123456789abcdef", ["sk-live-0123456789abcdef"]),
    ("client_secret=abcdEFGH1234", ["abcdEFGH1234"]),
    ('"user_phone_number": "+221700000106"', ["+221700000106", "700000106"]),
    ("phone=770123456", ["770123456"]),
    ("contact +221 77 123 45 67", ["77 123 45 67"]),
    ('{"fcm_token":"fZ:APA91b-XYZ","remember_token":"abc123"}', ["fZ:APA91b-XYZ", "abc123"]),
    ("X-Api-Key: AIzaSy0123456789", ["AIzaSy0123456789"]),
    ('{"uid": "8f3c1d9a4b2e7c55"}', ["8f3c1d9a4b2e7c55"]),
    ("contact 221700000106", ["221700000106"]),
]

PHONES = ["+221700000106", "770123456", "+221 77 123 45 67"]


@case
def test_secrets_are_masked():
    for raw, leaked in SECRETS:
        out = sanitize(raw)
        for secret in leaked:
            assert secret not in out, (raw, out, secret)
        assert "[REDACTED]" in out or "*" in out, (raw, out)


@case
def test_phone_digits_are_mostly_stars():
    out = sanitize('"user_phone_number": "+221700000106"')
    assert "+221" in out, out
    assert out.count("*") >= 5, out
    assert out.rstrip('"').endswith("106"), out


@case
def test_masking_is_idempotent():
    for raw, _ in SECRETS:
        once = sanitize(raw)
        assert sanitize(once) == once, (raw, once, sanitize(once))


@case
def test_realistic_api_record_is_clean_but_still_useful():
    rec = {"t": 1761400000.1, "m": "GET", "path": "/api/ride/details", "code": 200,
           "resp": {"uid": "8f3c1d9a4b2e7c55", "rider_phone": "+221700000106",
                    "fcm_token": "fZ:APA91b-XYZ", "pickup_location": "E2E Pickup Plateau",
                    "name": "E2E Rider"}}
    out = sanitize(json.dumps(rec))
    for leaked in ("8f3c1d9a4b2e7c55", "+221700000106", "fZ:APA91b-XYZ"):
        assert leaked not in out, (leaked, out)
    assert "E2E Pickup Plateau" in out, out


@case
def test_cli_append_requires_a_path():
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "sanitize.py"), "--append"],
        input="", capture_output=True, text=True)
    assert proc.returncode != 0, proc


@case
def test_ordinary_log_content_survives():
    keep = [
        "RESULT rider-home-loaded = PASS (ui cap=07-home.png marker ok)",
        "ride_state: status=running stops=1/2",
        "records_uid=7 delta_pkg=2",
        "t=1761400000.123 path=/api/rides code=200",
        "arrêt 1 confirmé par l'UI (écran suivant prouvé)",
    ]
    for line in keep:
        assert sanitize(line) == line, (line, sanitize(line))


# --------------------------------------------------- blockers Max (2026-10-26)
@case
def test_uid_in_prose_is_masked():
    """Bloquant 1 — run.sh publie « … portant l'uid <uid> » : la prose doit être masquée."""
    uid = "8f3c1d9a4b2e7c55"
    for raw in (
        f"notif: 0 notification portant l'uid {uid}",
        "uid 123",
        f"ride uid {uid}",
        f"l'uid {uid}",
    ):
        out = sanitize(raw)
        assert uid not in out, (raw, out)
        assert "123" not in out if raw == "uid 123" else True
    # un compteur dont la clé se termine par _uid n'est pas un identifiant
    assert sanitize("records_uid=7 delta_pkg=2") == "records_uid=7 delta_pkg=2"


@case
def test_api_record_is_whitelisted_and_paths_masked():
    """Bloquant 2 — api_flow ne publie plus resp entier ; les ids numériques des routes sont masqués."""
    from sanitize import mask_path, redact_record

    paths = {
        "/api/ride/details/123": "/api/ride/details/[REDACTED]",
        "ride/details/123": "ride/details/[REDACTED]",
        "driver/rides/details/45": "driver/rides/details/[REDACTED]",
        "driver/rides/start/45": "driver/rides/start/[REDACTED]",
        "driver/rides/end/45": "driver/rides/end/[REDACTED]",
        "driver/rides/complete-stop/45": "driver/rides/complete-stop/[REDACTED]",
        "driver/rides/received-cash-payment/45": "driver/rides/received-cash-payment/[REDACTED]",
        "ride/cancel/45": "ride/cancel/[REDACTED]",
        "ride/payment/45": "ride/payment/[REDACTED]",
    }
    for raw, want in paths.items():
        assert mask_path(raw) == want, (raw, mask_path(raw))
    for keep in ("ride/list", "general-setting", "/api/general-setting"):
        assert mask_path(keep) == keep, keep

    uid = "8f3c1d9a4b2e7c55"
    rec = {"t": 1761400000.1, "m": "GET", "path": "/api/ride/details/123", "code": 200,
           "resp": {"status": "success", "remark": "ok",
                    "data": {"ride": {
                        "id": 45, "uid": uid, "otp": 4821, "driver_id": 23, "ride_id": 45,
                        "payment_status": 1, "amount": 1500, "distance": 3.2,
                        "completed": False, "new_field": {"secret": "zzz"},
                        "stops": [{"completed": True, "completed_at": "2026-10-26T01:00:00Z"}]}}}}
    out = redact_record(rec)
    blob = json.dumps(out)
    for leaked in (uid, "4821", "zzz", "2026-10-26T01:00:00Z"):
        assert leaked not in blob, (leaked, blob)
    assert out["t"] == rec["t"] and out["m"] == "GET" and out["code"] == 200
    assert out["path"] == "/api/ride/details/[REDACTED]", out["path"]
    ride = out["resp"]["data"]["ride"]
    for kept in ("driver_id", "ride_id", "payment_status", "amount", "distance", "completed"):
        assert ride[kept] == rec["resp"]["data"]["ride"][kept], (kept, ride)
    assert ride["stops"][0]["completed"] is True
    assert ride["uid"] == "[REDACTED]" and ride["otp"] == "[REDACTED]"
    assert ride["new_field"] == "[REDACTED]", ride["new_field"]


@case
def test_path_ids_are_masked_in_free_text():
    """Durcissement — toute ligne copiée vers stderr/smoke masque les ids de route, pas seulement `path`."""
    for raw, leaked in (
        ("path=/api/ride/details/123", "123"),
        ("GET /api/ride/cancel/45?x=1", "45"),
        ("driver/rides/complete-stop/4567#top", "4567"),
    ):
        out = sanitize(raw)
        assert leaked not in out, (raw, out)
        assert "[REDACTED]" in out, (raw, out)
    for keep in ("path=/api/rides", "path=/api/ride/details", "ride/list"):
        assert sanitize(keep) == keep, (keep, sanitize(keep))


@case
def test_mask_path_covers_trailing_query_and_fragment():
    """Durcissement — un id suivi d'une query/fragment est masqué, pas seulement d'un `/` ou de la fin."""
    from sanitize import mask_path

    for raw, want in (
        ("/api/ride/details/123?x=1", "/api/ride/details/[REDACTED]?x=1"),
        ("/api/ride/details/123#top", "/api/ride/details/[REDACTED]#top"),
    ):
        assert mask_path(raw) == want, (raw, mask_path(raw))


@case
def test_numeric_counters_survive_but_credentials_do_not():
    """Bloquant 3 — records_token=0/12, records_uid=7 restent lisibles ; otp/password/key masqués."""
    for line in ("records_token=0", "records_token=12", "delta_pkg=2", "records_uid=7"):
        assert sanitize(line) == line, (line, sanitize(line))
    for raw, value in (("otp=4821", "4821"), ("password=1234", "1234"),
                       ("secret=123", "123"), ("key=123", "123"),
                       ("token=abc123", "abc123")):
        out = sanitize(raw)
        assert value not in out and "[REDACTED]" in out, (raw, out)


@case
def test_api_flow_err_is_published_and_truncated():
    """Bloquant 4 — /api-flow.err nettoyé rejoint smoke.log + stderr, puis est tronqué."""
    tmp = tempfile.mkdtemp(prefix="e2e-err-")
    smoke = os.path.join(tmp, "smoke.log")
    err = os.path.join(tmp, "api-flow.err")
    with open(err, "w", encoding="utf-8") as fh:
        fh.write("dev-token=deadbeefcafebabe1234 user +221700000106\n"
                 "\n"
                 "ConnectionResetError: ride uid 8f3c1d9a4b2e7c55\n")
    env = dict(os.environ, SMOKE_LOG=smoke)
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; smoke_emit_file "$2"', "_",
         os.path.join(ROOT, "smoke_log.sh"), err],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    body = open(smoke, encoding="utf-8").read()
    for leaked in ("deadbeefcafebabe1234", "+221700000106", "8f3c1d9a4b2e7c55"):
        assert leaked not in body, (leaked, body)
    assert "ConnectionResetError" in body, body
    assert "\n\n" not in "\n" + body, "les lignes vides doivent être ignorées"
    assert os.path.getsize(err) == 0, "le fichier doit être tronqué"
    assert "[REDACTED]" in body, body

    # câblage : après chaque ride_state + dans flush_results, et exports pour le watchdog
    run = text("run.sh")
    ride = re.search(r"^ride_state\(\) \{(.*?)^\}", run, re.M | re.S)
    assert ride and "smoke_emit_file" in ride.group(1), "ride_state doit publier api-flow.err"
    flush = re.search(r"^flush_results\(\) \{(.*?)^\}", run, re.M | re.S)
    assert flush and "smoke_emit_file" in flush.group(1), "flush_results doit publier api-flow.err"
    smoke_sh = text("smoke_log.sh")
    assert "smoke_emit_file()" in smoke_sh, smoke_sh
    assert "export SMOKE_LOG SMOKE_SANITIZER" in smoke_sh, smoke_sh
    assert "export -f smoke_emit smoke_emit_file" in smoke_sh, smoke_sh


# ------------------------------------------------------------------ shell wiring
def smoke_emit(line, extra_env=None):
    tmp = tempfile.mkdtemp(prefix="e2e-smoke-")
    smoke = os.path.join(tmp, "smoke.log")
    env = dict(os.environ, SMOKE_LOG=smoke)
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; smoke_emit "$2"', "_",
         os.path.join(ROOT, "smoke_log.sh"), line],
        capture_output=True, text=True, env=env)
    body = open(smoke, encoding="utf-8").read() if os.path.exists(smoke) else ""
    return proc, body


@case
def test_shell_emit_writes_sanitized_stderr_and_file():
    proc, body = smoke_emit("dev-token=deadbeefcafebabe1234 user +221700000106")
    assert proc.returncode == 0, proc.stderr
    assert "deadbeefcafebabe1234" not in proc.stderr, proc.stderr
    assert "+221700000106" not in proc.stderr, proc.stderr
    assert "deadbeefcafebabe1234" not in body, body
    assert "+221700000106" not in body, body
    assert "[REDACTED]" in body, body
    assert proc.stderr.strip() == body.strip(), (proc.stderr, body)


@case
def test_run_sh_log_keeps_raw_file_and_emits_smoke():
    body = text("run.sh")
    fn = re.search(r"^log\(\) \{(.*?)^\}", body, re.M | re.S)
    assert fn, "log() introuvable dans run.sh"
    block = fn.group(1)
    assert '>> "$OUT/run.log"' in block, block
    assert "smoke_emit" in block, block


@case
def test_ui_sh_ulog_emits_smoke():
    body = text("ui.sh")
    fn = re.search(r"^_ulog\(\) \{(.*?)^\}", body, re.M | re.S)
    assert fn, "_ulog() introuvable dans ui.sh"
    assert "smoke_emit" in fn.group(1), fn.group(1)
    assert "tee" not in fn.group(1), "stderr doit passer par le masquage, pas tee"


@case
def test_api_flow_logs_through_sanitizer():
    body = text("api_flow.py")
    assert re.search(r"^def log\(", body, re.M), "api_flow doit exposer log()"
    assert "smoke_emit" in body, "api_flow doit copier vers le smoke log masqué"
    assert "log.write(" not in body, "les appels doivent passer par log()"


@case
def test_workflow_publishes_smoke_artifact():
    body = open(WORKFLOW, encoding="utf-8").read()
    assert "staging-e2e-smoke-log" in body, body
    assert "e2e/smoke.log" in body, body
    assert "staging-e2e-evidence-encrypted" in body, "l'artefact chiffré doit rester"


# ------------------------------------------------------------------ runner
def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} smoke-log cases passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
