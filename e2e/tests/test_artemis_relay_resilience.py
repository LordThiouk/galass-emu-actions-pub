#!/usr/bin/env python3
"""TDD spec — a 502-ing relay must not eat the Artemis step (run 40602473815).

Run 40602473815: `served_model=unknown`, six attempts all rc=124 at ~33 s each.
The preflight (1x1 pixel) saw 6/6 alive, then the REAL call (1080x2400 capture,
~300 KB) got a 200 and a 502 from the FreeLLMAPI relay; the OpenAI SDK's own
"Retrying request" loop then burned the whole attempt window because the vendor
`timeout` was the default 60 s and no retry bound exists on `ModelEndpoint`.

Pins:
  * `apply --request-timeout S` writes the vendor field that is actually honoured
    (`timeout`, float — llm.py:91-96 -> router.py:300) on EVERY node block, never
    on a `fallback` sub-dict (the base LLM class ignores it, llm.py:54-66);
  * the value is BOUNDED upstream: (1 + sdk_retries) x timeout + margin <= the
    attempt share, sdk_retries default 2;
  * `artemis_run` records 502s and "Retrying request" occurrences per attempt in
    its OWN artifact, with a masked tail per attempt, without touching the frozen
    7-field `attempts.txt` schema;
  * the preflight is TWO-STAGE: the 1x1 probe only proves liveness, a realistic
    payload (~300 KB) decides usability — a chain whose routes all 502 on the
    realistic payload goes chain-off BEFORE the run burns its budget.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
RUN = os.path.join(ROOT, "run.sh")
SMOKE_SH = os.path.join(ROOT, "smoke_log.sh")
MODEL_CHAIN = os.path.join(ROOT, "model_chain.py")
PREFLIGHT = os.path.join(ROOT, "vision_preflight.py")
sys.path.insert(0, ROOT)

BASE_CHAIN = ["gpt-4.1", "gemini-2.5-flash", "gpt-4o"]
BASE_STR = ",".join(BASE_CHAIN)
SECRET = "fl-e2e-relay-do-not-log-0123456789"
RELAY_LIMIT = 4096          # bytes: above this the fake relay answers 502
REALISTIC_BYTES = 300 * 1024

CONFIG_JSONC = """\
{
  "default": {
    "provider": "openai",
    "model": "auto",
    "fallback": { "provider": "openai", "model": "gemini-3.5-flash" }
  },
  "presets": {
    "openai-gpt4o": {
      "provider": "openai",
      "model": "gpt-4o",
      "fallback": { "provider": "openai", "model": "gpt-4o-mini" },
    },
  },
  "nodes": {
    "planner": { "thinking_level": "high" },
    "hopper": { "model": "gemini-3.5-flash-lite" },
    "outputter": {},
  },
}
"""


def case(fn):
    CASES.append(fn)
    return fn


CASES = []


def text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def write(path, content):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def py(script, argv=(), env=None, cwd=None):
    return subprocess.run([sys.executable, script, *argv], capture_output=True,
                          text=True, env=env, cwd=cwd)


def bash(script, argv=(), env=None, cwd=None):
    return subprocess.run(["bash", "-c", script, "_", *argv], capture_output=True,
                          text=True, env=env, cwd=cwd)


def extract_fn(path, name):
    """Whole `name() { ... }` definition, brace-counted (braces are balanced here)."""
    body = text(path)
    start = re.search(r"(?m)^%s\(\) \{" % re.escape(name), body)
    assert start, f"{name}() introuvable dans {path}"
    depth = 0
    for i in range(start.end() - 1, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                return body[start.start():i + 1]
    raise AssertionError(f"{name}() non fermée dans {path}")


# ------------------------------------------------------- fake relay (size 502)
def _relay(limit=RELAY_LIMIT, seen=None):
    """200 under `limit` bytes, 502 above it — the FreeLLMAPI failure shape."""
    seen = seen if seen is not None else []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            seen.append(length)
            if length > limit:
                data = json.dumps({"error": {"message": "502 Bad Gateway"}}).encode()
                code = 502
            else:
                data = json.dumps({"choices": [{"message": {"content": "Red"}}]}).encode()
                code = 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _flaky_second_sample(limit=RELAY_LIMIT, flaky=None):
    """200 on the first realistic sample, 502 on the second, for ONE model."""
    flaky = BASE_CHAIN[1] if flaky is None else flaky
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                model = json.loads(raw.decode()).get("model") or ""
            except (ValueError, UnicodeDecodeError):
                model = ""
            code = 200
            if length > limit:
                seen[model] = seen.get(model, 0) + 1
                if model == flaky and seen[model] >= 2:
                    code = 502
            data = json.dumps({"choices": [{"message": {"content": "Red"}}],
                               "model": model}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


# ------------------------------------------- 1. bounded per-request timeout
@case
def test_apply_writes_the_vendor_timeout_on_every_node_never_on_fallback():
    """`timeout` is the field the vendor honours; a fallback dict ignores it."""
    tmp = tempfile.mkdtemp(prefix="e2e-relay-apply-")
    src = write(os.path.join(tmp, "artemis.jsonc"), CONFIG_JSONC)
    chain_file = write(os.path.join(tmp, "chain.txt"), "\n".join(BASE_CHAIN) + "\n")
    out = os.path.join(tmp, "runtime", "artemis.jsonc")

    proc = py(MODEL_CHAIN, ("apply", "--in", src, "--out", out,
                            "--chain-file", chain_file,
                            "--request-timeout", "9", "--sdk-retries", "2"))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    cfg = json.loads(text(out))

    blocks = [("default", cfg["default"])]
    blocks += [(f"presets.{k}", v) for k, v in cfg["presets"].items()]
    blocks += [(f"nodes.{k}", v) for k, v in cfg["nodes"].items()]
    assert len(blocks) == 5, blocks
    for name, block in blocks:
        assert block.get("timeout") == 9, (name, block)
        assert isinstance(block["timeout"], (int, float)), (name, block)
        fallback = block.get("fallback")
        if isinstance(fallback, dict):
            assert "timeout" not in fallback, (name, fallback)

    # unrelated keys and the model rotation are untouched
    assert cfg["nodes"]["planner"]["thinking_level"] == "high"
    assert cfg["default"]["model"] == BASE_CHAIN[0]
    assert text(src) == CONFIG_JSONC, "le fichier source a été modifié"

    # without the flag nothing is invented
    plain = os.path.join(tmp, "plain.json")
    proc = py(MODEL_CHAIN, ("apply", "--in", src, "--out", plain,
                            "--chain-file", chain_file))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    for block in json.loads(text(plain))["nodes"].values():
        assert "timeout" not in block, block

    # a non-positive / garbage bound is refused, and nothing is written
    for bad in ("0", "-3", "abc"):
        proc = py(MODEL_CHAIN, ("apply", "--in", src, "--out", out + ".bad",
                                "--chain-file", chain_file,
                                "--request-timeout", bad))
        assert proc.returncode == 2, (bad, proc.returncode, proc.stdout, proc.stderr)
        assert not os.path.exists(out + ".bad"), f"sortie écrite malgré --request-timeout {bad}"


@case
def test_request_timeout_fits_the_sdk_retries_inside_the_attempt_share():
    """(1 + sdk_retries) calls of `timeout` + margin must fit in the share.

    The margin scales down with the share (min(5 s, 20 %)): a flat 5 s margin on
    a 6 s share ate the budget. And when nothing fits, the answer is 0 — the
    caller then writes NO vendor timeout and the outer timeout governs, instead
    of a 1 s value that kills the call before it can log a line (audit R2).
    """
    proc = py(MODEL_CHAIN, ("request-timeout", "--share", "33", "--sdk-retries", "2"))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "9", proc.stdout

    import model_chain
    # the invariant, for EVERY share and retry count: fits, or 0
    for share in (1, 3, 5, 6, 8, 10, 20, 33, 60, 120):
        for retries in (0, 1, 2, 5):
            got = model_chain.request_timeout(share, retries)
            assert got >= 0, (share, retries, got)
            if got == 0:
                continue
            margin = min(5, share * 0.2)
            assert (1 + retries) * got + margin <= share, (share, retries, got)
    for share, retries, margin in ((33, 2, 5), (10, 0, 0), (60, 5, 5), (20, 2, 5)):
        proc = py(MODEL_CHAIN, ("request-timeout", "--share", str(share),
                                "--sdk-retries", str(retries),
                                "--margin", str(margin)))
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        got = int(proc.stdout.strip())
        assert got >= 1, (share, retries, margin, got)
        assert (1 + retries) * got + margin <= max(share, 1), (share, retries, margin, got)

    # a share that funds nothing: 0, with the reason on stderr, and no 1 s lie
    proc = py(MODEL_CHAIN, ("request-timeout", "--share", "3", "--sdk-retries", "2"))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert int(proc.stdout.strip()) == 0, proc.stdout
    assert "no bounded call" in (proc.stdout + proc.stderr).lower(), (proc.stdout, proc.stderr)
    # 5 s / 2 retries still funds exactly one 1 s call: 3 x 1 + 1 <= 5
    proc = py(MODEL_CHAIN, ("request-timeout", "--share", "5", "--sdk-retries", "2",
                            "--margin", "5"))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert int(proc.stdout.strip()) == 1, proc.stdout
    # sdk retries default is the SDK's own 2; nonsense never crashes the harness
    assert model_chain.RELAY_SDK_RETRIES == 2
    assert model_chain.request_timeout(33) == 9
    assert model_chain.request_timeout("abc") >= 1
    assert model_chain.request_timeout(3) == 0


@case
def test_run_sh_passes_the_bound_and_counts_the_relay_failures():
    fn = extract_fn(RUN, "artemis_run")
    assert "--request-timeout" in fn, "le timeout par requête n'est pas transmis à apply"
    assert "--sdk-retries" in fn, "le budget de réessais SDK n'est pas transmis"
    assert "request-timeout" in fn and "model_chain.py" in fn, fn
    assert 'masked_tail' in text(RUN), "aucune queue masquée par essai"
    assert "relay-attempts.txt" in fn, "les compteurs 502/Retrying ne sont pas persistés"
    assert "Retrying request" in fn, fn
    assert re.search(r"502", fn), fn
    # a 0 from request-timeout writes NO vendor key: the outer timeout governs
    assert re.search(r'\[ "\$req_timeout" -gt 0 \]', fn), fn
    assert "pas de timeout vendor" in fn, fn
    # the frozen ledger schema (test_model_chain.py parses 7 fields) is untouched
    assert "printf '%s|%s|%s|%s|%s|%s|%s\\n'" in fn, fn
    # the blind tail is masked, never published raw
    tail_fn = extract_fn(RUN, "masked_tail")
    assert "sanitize.py" in tail_fn, tail_fn
    assert "[B64]" in tail_fn, "les blobs base64 doivent être masqués dans la queue"
    assert "tail -n" in tail_fn, tail_fn


# --------------------------------------- 2. a 502-ing relay exhausts gracefully
FAKE_RELAY_ARTEMIS = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALLS"
"$PY" - "$RELAY_URL" <<'PYRELAY'
import json, sys, urllib.error, urllib.request
url = sys.argv[1].rstrip("/") + "/chat/completions"
big = "A" * 8192                      # a real 1080x2400 capture, not a 1x1 pin
print("INFO reading screenshot /home/runner/work/galass/e2e-out/screens/42.png")
print("INFO image data:image/png;base64," + "iVBORw0KGgo" + "A" * 400)
for _attempt in range(3):
    request = urllib.request.Request(
        url, method="POST",
        data=json.dumps({"model": "probe", "pad": big}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            print("INFO answered", response.status)
            sys.exit(0)
    except urllib.error.HTTPError as exc:
        print("ERROR relay http=%s on a %sB payload" % (exc.code, len(big)))
        print("Retrying request to /chat/completions in 0.0 seconds")
print("INFO retries exhausted on this route")
sys.exit(1)
PYRELAY
'''


def artemis_tree(tmp, fake_body, chain):
    """Throwaway repo root: fake vendor binary, e2e symlink, runtime config."""
    art = os.path.join(tmp, "vendor", "artemis", ".venv", "bin")
    os.makedirs(art, exist_ok=True)
    fake = os.path.join(art, "artemis")
    write(fake, fake_body)
    os.chmod(fake, 0o755)
    for sub in ("artemis", "screens"):
        os.makedirs(os.path.join(tmp, sub), exist_ok=True)
    os.symlink(ROOT, os.path.join(tmp, "e2e"))
    cfg = write(os.path.join(tmp, "vendor", "artemis", "config", "artemis.jsonc"),
                CONFIG_JSONC)
    chain_file = os.path.join(tmp, "artemis", "vision-chain.txt")
    write(chain_file, "\n".join(chain) + "\n")
    write(os.path.join(tmp, "run.log"), "")
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    env.pop("RELAY_URL", None)
    env.update({
        "OUT": tmp,
        "SMOKE_LOG": os.path.join(tmp, "smoke.log"),
        "PY": sys.executable,
        "ARTEMIS_OK": "1",
        "ARTEMIS_SERIAL": "emulator-5554",
        "ARTEMIS_CHAIN_FILE": chain_file,
        "ARTEMIS_CONFIG": cfg,
        "ARTEMIS_CONFIG_RUNTIME": os.path.join(tmp, "artemis", "artemis.jsonc"),
        "ARTEMIS_BACKOFF": "0 0 0",
        "CALLS": os.path.join(tmp, "calls.txt"),
    })
    return env, chain_file


def run_artemis(env, tmp, tmo="30"):
    script = (
        "set -uo pipefail\n"
        'log() { printf "[e2e] %s\\n" "$*" >> "$OUT/run.log"; }\n'
        "adb() { :; }\n"
        '. "$1"\n'
        'eval "$2"\neval "$3"\neval "$4"\neval "$5"\n'
        'artemis_run smoke "open settings" ' + tmo + "\n"
        'printf "RC=%s\\n" "$?"\n'
    )
    return bash(script, (SMOKE_SH, extract_fn(RUN, "artemis_serial"),
                         extract_fn(RUN, "masked_tail"),
                         extract_fn(RUN, "publish_artemis_log"),
                         extract_fn(RUN, "artemis_run")), env=env, cwd=tmp)


@case
def test_a_502_relay_records_the_counts_and_cuts_the_chain_after_two():
    server, seen = _relay()
    try:
        relay = f"http://127.0.0.1:{server.server_port}"
        tmp = tempfile.mkdtemp(prefix="e2e-relay-artemis-")
        env, _cf = artemis_tree(tmp, FAKE_RELAY_ARTEMIS, BASE_CHAIN)
        env["RELAY_URL"] = relay
        proc = run_artemis(env, tmp)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert proc.stdout.strip().endswith("RC=1"), proc.stdout

        # one attempt per route, each one counted, and none of them a fake 124
        rows = [r.split("|") for r in
                text(os.path.join(tmp, "artemis", "attempts.txt")).strip().splitlines()]
        real = [r for r in rows if not r[6] == "budget"]
        # deux essais 502 consécutifs suffisent: la chaîne est coupée (R3), elle
        # n'est plus usée route par route jusqu'à épuisement du budget.
        assert len(real) == 2, rows
        assert all(len(r) == 7 for r in real), rows
        assert [r[6] for r in real] == ["retryable"] * 2, rows
        assert all(r[5] == "5xx" for r in real), rows

        relay_rows = [r.split("|") for r in
                      text(os.path.join(tmp, "artemis", "relay-attempts.txt"))
                      .strip().splitlines()]
        assert len(relay_rows) == 2, relay_rows
        for name, attempt, count502, retrying, tail in relay_rows:
            assert name == "smoke", relay_rows
            assert int(count502) >= 1, relay_rows
            assert int(retrying) >= 1, relay_rows
            assert os.path.isfile(os.path.join(tmp, tail)), tail

        # the tail is published masked: no absolute path, no base64 blob
        tail = text(os.path.join(tmp, "artemis", "smoke.attempt1.tail.txt"))
        assert "retries exhausted" in tail or "relay http=502" in tail, tail
        assert "/home/runner" not in tail, tail
        assert "iVBORw0KGgoAAA" not in tail, "blob base64 non masqué"
        assert "[B64]" in tail or "[PATH]" in tail, tail
        assert SECRET not in text(os.path.join(tmp, "run.log"))
        log = text(os.path.join(tmp, "run.log"))
        assert "artemis relay:" in log, log
        assert "502=" in log, log
        assert seen and max(seen) > RELAY_LIMIT, "le faux relais n'a jamais vu de gros payload"
    finally:
        server.shutdown()
        server.server_close()


@case
def test_two_consecutive_502s_short_circuit_with_an_honest_chainoff():
    """Two consecutive 502 signatures cut the chain — no third attempt.

    The vendor's PROVIDER_UNAVAILABLE policy sleeps 5+10+20 s between its own
    retries (reliability.py:154-158, frozen and unreachable from config), so on
    the ~33 s share of run 40602473815 the sleep ALONE explains rc=124. A third
    attempt after two 502s cannot produce a different answer: the harness stops,
    writes a verdict, and never marks the point as a success.
    """
    server, _seen = _relay()
    try:
        relay = f"http://127.0.0.1:{server.server_port}"
        tmp = tempfile.mkdtemp(prefix="e2e-relay-shortcircuit-")
        env, _cf = artemis_tree(tmp, FAKE_RELAY_ARTEMIS, BASE_CHAIN)
        env["RELAY_URL"] = relay
        proc = run_artemis(env, tmp)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert proc.stdout.strip().endswith("RC=1"), proc.stdout

        rows = [r.split("|") for r in
                text(os.path.join(tmp, "artemis", "attempts.txt")).strip().splitlines()]
        real = [r for r in rows if r[6] != "budget"]
        assert len(real) <= 2, ("la chaîne n'a pas coupé après 2 essais 502", rows)
        # the frozen 7-field ledger is untouched, budget rows included
        assert all(len(r) == 7 for r in rows), rows

        log = text(os.path.join(tmp, "run.log"))
        assert "artemis chainoff:" in log, log
        off = text(os.path.join(tmp, "artemis", "relay-chainoff.txt"))
        assert off.strip(), "aucun artefact de verdict chain-off"
        assert "relay_502" in off, off
        assert "PASS" not in off, off
        # rc!=0 => the run never claims a served model
        usage = text(os.path.join(tmp, "artemis", "model-usage.txt"))
        assert "served=unknown" in usage, usage
        assert "served_model=unknown" in usage, usage
    finally:
        server.shutdown()
        server.server_close()


@case
def test_a_chain_with_no_route_left_is_chain_off_before_the_budget():
    """ARTEMIS_OK=0 (preflight chain-off) => no attempt, no budget burned."""
    tmp = tempfile.mkdtemp(prefix="e2e-relay-chainoff-")
    env, _cf = artemis_tree(tmp, FAKE_RELAY_ARTEMIS, BASE_CHAIN)
    env["ARTEMIS_OK"] = "0"
    env["RELAY_URL"] = "http://127.0.0.1:9"
    proc = run_artemis(env, tmp)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=2"), proc.stdout
    assert not os.path.exists(env["CALLS"]), "Artemis a lancé un essai sans route vivante"
    log = text(os.path.join(tmp, "run.log"))
    assert "artemis skip" in log, log
    assert "artemis start:" not in log, log


# -------------------------------------------- 3. two-stage realistic preflight
def _tiny_payload(payload):
    return len(json.dumps(payload).encode()) < 2048


@case
def test_preflight_rejects_a_route_that_502s_on_the_realistic_payload():
    server, seen = _relay()
    try:
        tmp = tempfile.mkdtemp(prefix="e2e-preflight-realistic-")
        chain = write(os.path.join(tmp, "chain.txt"), "\n".join(BASE_CHAIN) + "\n")
        out = os.path.join(tmp, "alive.txt")
        log = os.path.join(tmp, "preflight.log")
        env = dict(os.environ, FREELLM_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   FREELLM_API_KEY=SECRET)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_API_KEY", None)
        proc = py(PREFLIGHT, ("--chain-file", chain, "--out", out, "--log", log,
                              "--probe-bytes", str(REALISTIC_BYTES)), env=env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert text(out).strip() == "", \
            "une route qui 502 sur le payload réaliste est restée vivante"
        body = text(log)
        assert "stage=realistic" in body, body
        assert body.count("http=502") >= len(BASE_CHAIN), body
        assert SECRET not in body, body
        # two stages over the whole chain: 1x1 liveness, then the real size
        assert len(seen) == 2 * len(BASE_CHAIN), seen
        assert max(seen) >= REALISTIC_BYTES * 3 // 4, seen
    finally:
        server.shutdown()
        server.server_close()


@case
def test_preflight_keeps_the_route_that_survives_the_realistic_payload():
    server, seen = _relay(limit=10 ** 9)
    try:
        tmp = tempfile.mkdtemp(prefix="e2e-preflight-survivor-")
        chain = write(os.path.join(tmp, "chain.txt"), "\n".join(BASE_CHAIN) + "\n")
        out = os.path.join(tmp, "alive.txt")
        log = os.path.join(tmp, "preflight.log")
        env = dict(os.environ, FREELLM_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   FREELLM_API_KEY=SECRET)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_API_KEY", None)
        proc = py(PREFLIGHT, ("--chain-file", chain, "--out", out, "--log", log,
                              "--probe-bytes", str(REALISTIC_BYTES)), env=env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert text(out).split() == BASE_CHAIN, text(out)
        # the default size is realistic on its own (no flag needed in the run)
        assert max(seen) >= REALISTIC_BYTES * 3 // 4, seen

        # ... and `--probe-bytes 0` degrades to the legacy liveness-only pass
        out2 = os.path.join(tmp, "alive-legacy.txt")
        seen.clear()
        proc = py(PREFLIGHT, ("--chain-file", chain, "--out", out2, "--log", log,
                              "--probe-bytes", "0"), env=env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert text(out2).split() == BASE_CHAIN, text(out2)
        assert len(seen) == len(BASE_CHAIN), seen
    finally:
        server.shutdown()
        server.server_close()


@case
def test_preflight_samples_a_survivor_twice_before_trusting_it():
    """One 200 is not enough: the relay flake of 40602473815 answers once, then 502.

    A survivor must answer BOTH realistic samples; a route that fails the second
    is dead, exactly as it would be mid-run.
    """
    server, seen = _flaky_second_sample()
    try:
        tmp = tempfile.mkdtemp(prefix="e2e-preflight-twice-")
        chain = write(os.path.join(tmp, "chain.txt"), "\n".join(BASE_CHAIN) + "\n")
        out = os.path.join(tmp, "alive.txt")
        log = os.path.join(tmp, "preflight.log")
        env = dict(os.environ, FREELLM_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   FREELLM_API_KEY=SECRET)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_API_KEY", None)
        proc = py(PREFLIGHT, ("--chain-file", chain, "--out", out, "--log", log,
                              "--probe-bytes", str(REALISTIC_BYTES)), env=env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert text(out).split() == [BASE_CHAIN[0], BASE_CHAIN[2]], text(out)
        body = text(log)
        flaky = BASE_CHAIN[1]
        assert f"model={flaky} sample=1 http=200" in body, body
        assert f"model={flaky} sample=2 http=502" in body, body
        assert body.count(f"stage=realistic model={flaky} ") == 2, body
        # survivors that answered twice are kept; the flaky one costs no extra call
        assert seen[flaky] == 2, seen
        assert seen[BASE_CHAIN[0]] == 2 and seen[BASE_CHAIN[2]] == 2, seen
        assert SECRET not in body, body
    finally:
        server.shutdown()
        server.server_close()


@case
def test_run_sh_arms_the_realistic_probe_with_a_realistic_default():
    body = text(RUN)
    assert "VISION_PROBE_BYTES" in body, "aucune taille de payload réaliste armée"
    assert re.search(r'VISION_PROBE_BYTES:-[0-9]+', body), body
    default = int(re.search(r'VISION_PROBE_BYTES:-([0-9]+)', body).group(1))
    assert default >= REALISTIC_BYTES, default
    assert '--probe-bytes "$VISION_PROBE_BYTES"' in body, body
    # chain-off keeps its existing verdict shape
    assert 'mark llm-vision WARN' in body, body
    assert "ARTEMIS_OK=0" in body, body


@case
def test_synthetic_payload_is_a_decodable_png_with_idat():
    """A provider DECODES before answering: no IDAT => 400 => healthy route dropped.

    Verified by Nix: PIL raises `cannot load this image` on IHDR+tEXt+IEND. The
    real payload Artemis sends is `data:image/jpeg;base64` (planner.py:282), so a
    probe that a decoder rejects can turn a healthy chain into a false chain-off.
    """
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, struct, zlib, json\n"
         "sys.path.insert(0, %r)\n"
         "import vision_preflight as v\n"
         "png = v.synthetic_png(v.PROBE_BYTES_DEFAULT)\n"
         "chunks, i = [], 8\n"
         "while i < len(png):\n"
         "    n = struct.unpack('>I', png[i:i+4])[0]\n"
         "    kind = png[i+4:i+8].decode()\n"
         "    chunks.append((kind, png[i+8:i+8+n]))\n"
         "    i += 12 + n\n"
         "idat = b''.join(d for k, d in chunks if k == 'IDAT')\n"
         "ihdr = [d for k, d in chunks if k == 'IHDR'][0]\n"
         "print(json.dumps({'kinds': [k for k, _ in chunks], 'len': len(png),\n"
         "  'w': struct.unpack('>I', ihdr[:4])[0],\n"
         "  'h': struct.unpack('>I', ihdr[4:8])[0],\n"
         "  'raw': len(zlib.decompress(idat))}))\n" % ROOT],
        capture_output=True, text=True)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "IDAT" in info["kinds"], info["kinds"]
    assert info["kinds"][0] == "IHDR", info["kinds"]
    assert info["kinds"][-1] == "IEND", info["kinds"]
    assert (info["w"], info["h"]) == (1080, 2400), info
    # filter byte 0 + W*3 RGB bytes, one scanline per row
    assert info["raw"] == 2400 * (1080 * 3 + 1), info
    assert info["len"] >= REALISTIC_BYTES * 3 // 4, info

    # PIL is optional: absent => a skip with a note, never a hard failure
    pil = subprocess.run(
        [sys.executable, "-c",
         "import sys, io\n"
         "sys.path.insert(0, %r)\n"
         "import vision_preflight as v\n"
         "try:\n"
         "    from PIL import Image\n"
         "except ImportError:\n"
         "    print('SKIP no PIL')\n"
         "    raise SystemExit(0)\n"
         "img = Image.open(io.BytesIO(v.synthetic_png(4096)))\n"
         "img.load()\n"
         "print('PIL ok', img.size)\n" % ROOT],
        capture_output=True, text=True)
    assert pil.returncode == 0, (pil.stdout, pil.stderr)
    assert "PIL ok" in pil.stdout or "SKIP no PIL" in pil.stdout, pil.stdout


@case
def test_synthetic_payload_is_a_real_image_of_phone_dimensions():
    """The probe body must look like a 1080x2400 capture, not a 1x1 pin."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "import vision_preflight as v\n"
         "png = v.synthetic_png(v.PROBE_BYTES_DEFAULT * 3 // 4)\n"
         "print(len(png))\n"
         "print(png[:8].hex())\n"
         "print(v.SYNTH_WIDTH, v.SYNTH_HEIGHT)\n"
         "print(v.PROBE_BYTES_DEFAULT)\n" % ROOT],
        capture_output=True, text=True)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    lines = proc.stdout.split()
    size, magic, width, height, default = (int(lines[0]), lines[1], int(lines[2]),
                                           int(lines[3]), int(lines[4]))
    assert magic == "89504e470d0a1a0a", "ce n'est pas un PNG"
    assert (width, height) == (1080, 2400), (width, height)
    assert size >= REALISTIC_BYTES * 3 // 4, size
    assert default == REALISTIC_BYTES, default
    assert _tiny_payload({"pad": "x" * 10})


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test harness
            failed += 1
            print(f"FAIL - {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok - {fn.__name__}")
    if failed:
        print(f"FAIL ({len(CASES) - failed}/{len(CASES)})")
        return 1
    print(f"PASS ({len(CASES)}/{len(CASES)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
