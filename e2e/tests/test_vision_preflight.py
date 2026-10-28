#!/usr/bin/env python3
"""TDD spec for the vision preflight MEASUREMENT and the unreachable verdict.

No network beyond 127.0.0.1: a local http.server answers the probe, and the
"relay is down" case points at a closed local port so the connection fails
immediately (no DNS, no timeout to wait on).

Pins:
  * ISC-2: every `stage=liveness` and `stage=realistic` line carries
    `latency_ms=<int>` — including the probes that got NO http status (http=0),
    where the timer IS the measurement;
  * ISC-3: when every ATTEMPTED liveness probe is a network error (http=0), the
    log ends with `verdict=relay_unreachable`, the exit code is 3, and the alive
    file is left EMPTY — the caller must never point Artemis at a route it did
    not prove. THREE CONSECUTIVE http=0 probes short-circuit the rest of the
    chain (the verdict covers what was attempted) ONLY while no route has
    answered 200 yet: a survivor proves the relay is up, so a later streak keeps
    probing the whole chain and never empties the alive file; a mixed run that
    recovers on an HTTP status keeps probing and never gets the verdict;
  * a route that answered anything (429 included) is NOT "relay_unreachable":
    the verdict only names a dead relay, never a refusing one;
  * Anti-redact: the provider key and the raw response body never reach the log.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFLIGHT = os.path.join(ROOT, "vision_preflight.py")

SECRET = "sk-vision-do-not-log-0123456789"
# Port 9 (discard) is closed here: ECONNREFUSED is immediate, so the "unreachable"
# case costs microseconds instead of a probe timeout.
DEAD_BASE = "http://127.0.0.1:9"
CHAIN = ["gpt-4.1", "gemini-2.5-flash", "gpt-4o"]

LATENCY_RE = re.compile(r"(?:^|\s)latency_ms=(\d+)(?:\s|$)")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def text(path):
    return open(path, encoding="utf-8").read()


def write(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def probe_env(base, extra=None):
    env = dict(os.environ, FREELLM_BASE_URL=base, FREELLM_API_KEY=SECRET)
    env.pop("OPENAI_BASE_URL", None)
    env.pop("OPENAI_API_KEY", None)
    env.update(extra or {})
    return env


def run_preflight(chain, base, extra_env=None, argv=()):
    tmp = tempfile.mkdtemp(prefix="e2e-vision-preflight-")
    chain_file = write(os.path.join(tmp, "chain.txt"),
                       "\n".join(chain) + ("\n" if chain else ""))
    out = os.path.join(tmp, "alive.txt")
    log = os.path.join(tmp, "preflight.log")
    proc = subprocess.run(
        [sys.executable, PREFLIGHT, "--chain-file", chain_file, "--out", out,
         "--log", log, *argv],
        capture_output=True, text=True, env=probe_env(base, extra_env))
    return tmp, proc, out, log


def serve(alive, settle=0.0, leak=False):
    class Handler(BaseHTTPRequestHandler):
        seen = []

        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            model = payload.get("model")
            Handler.seen.append((model, self.headers.get("Authorization")))
            if settle:
                time.sleep(settle)
            if model in alive:
                data = json.dumps({"choices": [{"message": {"content": "Red"}}]}).encode()
                code = 200
            else:
                leaky = " body echoes " + SECRET if leak else ""
                data = json.dumps({"error": {"message": "429 rate limit" + leaky}}).encode()
                code = 429
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the probe already gave up, which is the point

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Handler


def stage_lines(log_text):
    """Per-probe lines only (the `stage=… bytes=…` headers carry no measurement)."""
    return [line for line in log_text.splitlines()
            if line.startswith(("stage=liveness ", "stage=realistic "))
            and " model=" in line]


# ------------------------------------------------------------- ISC-2 latency
@case
def test_every_probe_line_carries_latency_ms():
    alive = {"gpt-4.1", "gpt-4o"}
    server, _handler = serve(alive)
    try:
        _tmp, proc, out, log = run_preflight(
            CHAIN, f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        body = text(log)
        lines = stage_lines(body)
        liveness = [l for l in lines if l.startswith("stage=liveness ")]
        realistic = [l for l in lines if l.startswith("stage=realistic ")]
        assert len(liveness) == len(CHAIN), liveness
        survivors = [m for m in CHAIN if m in alive]
        assert len(realistic) == len(survivors) * 2, realistic
        for line in lines:
            match = LATENCY_RE.search(line)
            assert match, f"latency_ms absent de la ligne: {line}"
            assert int(match.group(1)) >= 0, line
        assert [l for l in text(out).splitlines() if l.strip()] == survivors
    finally:
        server.shutdown()
        server.server_close()


@case
def test_latency_is_measured_on_a_probe_that_never_got_a_status():
    """http=0 (timeout) is data too: the timer still produces a latency."""
    server, _handler = serve({"gpt-4.1"}, settle=0.4)
    try:
        _tmp, proc, out, log = run_preflight(
            CHAIN[:2], f"http://127.0.0.1:{server.server_port}",
            argv=("--timeout", "0.05"))
        # every probe timed out -> a dead relay, never a silent success
        assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
        body = text(log)
        lines = stage_lines(body)
        assert lines, body
        for line in lines:
            assert "http=0" in line, line
            match = LATENCY_RE.search(line)
            assert match, f"latency_ms absent d'une sonde http=0: {line}"
            assert int(match.group(1)) >= 1, line
        assert text(out).strip() == "", text(out)
    finally:
        server.shutdown()
        server.server_close()


def serve_selective(alive, drop=()):
    """Alive routes answer 200; `drop` routes are killed mid-request (http=0).

    The connection is shut down before any status line, which is exactly what a
    dead relay does: urllib raises, the probe records http=0.
    """
    drop = set(drop)

    class Handler(BaseHTTPRequestHandler):
        seen = []

        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            model = payload.get("model")
            Handler.seen.append(model)
            if model in drop:
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.connection.close()
                except OSError:
                    pass
                return
            data = json.dumps({"choices": [{"message": {"content": "Red"}}]}).encode()
            try:
                self.send_response(200 if model in alive else 429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Handler


@case
def test_three_consecutive_network_failures_short_circuit_the_probe():
    """ISC-3: 3 consecutive http=0 stop the probing; the rest is never attempted."""
    chain = ["gpt-4.1", "gemini-2.5-flash", "gpt-4o", "gemini-3.5-flash-lite",
             "gemini-3.1-flash-lite", "gemini-flash-lite"]
    server, handler = serve_selective(alive=set(), drop=chain)
    try:
        _tmp, proc, out, log = run_preflight(
            chain, f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
        body = text(log)
        lines = body.rstrip("\n").splitlines()
        assert lines[-1] == "verdict=relay_unreachable", lines[-3:]
        liveness = [l for l in stage_lines(body) if l.startswith("stage=liveness ")]
        assert len(liveness) == 3, liveness
        assert handler.seen == chain[:3], handler.seen
        assert text(out) == "", text(out)
    finally:
        server.shutdown()
        server.server_close()


@case
def test_a_200_before_the_streak_keeps_the_chain_alive_and_keeps_probing():
    """BLOCKER: 3 consecutive http=0 AFTER a 200 must never verdict the relay dead.

    Reproduced on the round-1 code: fast-ok 200 then 3 dead => exit 3 and an EMPTY
    alive file, so run.sh published "relay unreachable" and ARTEMIS_OK=0 while a
    route had been proven alive. A survivor is proof the relay is UP; the streak
    only describes the routes that timed out.
    """
    chain = ["gpt-4.1", "gemini-2.5-flash", "gpt-4o", "gemini-3.5-flash-lite",
             "gemini-3.1-flash-lite"]
    server, handler = serve_selective(alive={"gpt-4.1"}, drop=set(chain[1:]))
    try:
        _tmp, proc, out, log = run_preflight(
            chain, f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        body = text(log)
        assert "verdict=relay_unreachable" not in body, body
        liveness = [l for l in stage_lines(body) if l.startswith("stage=liveness ")]
        # the streak truncated NOTHING: every id of the chain was still probed
        assert len(liveness) == len(chain), liveness
        assert handler.seen[:len(chain)] == chain, handler.seen
        # the proven route survives, so the caller points Artemis at a real one
        assert [l for l in text(out).splitlines() if l.strip()] == ["gpt-4.1"], \
            text(out)
    finally:
        server.shutdown()
        server.server_close()


@case
def test_three_zeros_without_a_single_success_still_verdicts_unreachable():
    """Complement of the fix: ZERO successes => the streak still wins the verdict.

    The unprobed tail here holds a route that WOULD have answered 200 — and the
    verdict must still fire, because nothing proved the relay was up and the
    caller must not spend the step on the remaining certain timeouts.
    """
    tail_alive = "gemini-3.1-flash-lite"
    chain = ["gpt-4.1", "gemini-2.5-flash", "gpt-4o", tail_alive]
    server, handler = serve_selective(alive={tail_alive}, drop=set(chain[:3]))
    try:
        _tmp, proc, out, log = run_preflight(
            chain, f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
        body = text(log)
        lines = body.rstrip("\n").splitlines()
        assert lines[-1] == "verdict=relay_unreachable", lines[-3:]
        liveness = [l for l in stage_lines(body) if l.startswith("stage=liveness ")]
        assert len(liveness) == 3, liveness
        assert handler.seen == chain[:3], "les ids restants ne doivent pas être sondés"
        assert text(out) == "", text(out)
    finally:
        server.shutdown()
        server.server_close()


@case
def test_network_failures_that_recover_keep_probing_and_never_get_the_verdict():
    """ISC-3: 2 network failures then an HTTP status => no verdict, full chain probed."""
    chain = ["gpt-4.1", "gemini-2.5-flash", "gpt-4o", "gemini-3.5-flash-lite"]
    drop = {"gpt-4.1", "gemini-2.5-flash"}
    server, handler = serve_selective(alive={"gpt-4o", "gemini-3.5-flash-lite"},
                                      drop=drop)
    try:
        _tmp, proc, out, log = run_preflight(
            chain, f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        body = text(log)
        assert "verdict=relay_unreachable" not in body, body
        liveness = [l for l in stage_lines(body) if l.startswith("stage=liveness ")]
        assert len(liveness) == len(chain), liveness
        assert [l for l in text(out).splitlines() if l.strip()] == [
            "gpt-4o", "gemini-3.5-flash-lite"], text(out)
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------- ISC-3 relay verdict
@case
def test_all_network_errors_end_with_the_unreachable_verdict_and_exit_3():
    _tmp, proc, out, log = run_preflight(CHAIN, DEAD_BASE)
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    body = text(log)
    lines = body.rstrip("\n").splitlines()
    assert lines[-1] == "verdict=relay_unreachable", lines[-3:]
    assert "http=0" in body, body
    assert os.path.exists(out), "le fichier alive doit exister, vide"
    assert text(out) == "", text(out)
    assert SECRET not in body, "la clé API a fuité dans le journal"
    for line in stage_lines(body):
        assert "sk-vision-do-not-log" not in line, line


@case
def test_one_answering_route_is_never_called_a_dead_relay():
    alive = {"gpt-4o"}
    server, _handler = serve(alive)
    try:
        _tmp, proc, _out, log = run_preflight(
            CHAIN, f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "verdict=relay_unreachable" not in text(log), text(log)
    finally:
        server.shutdown()
        server.server_close()


@case
def test_routes_that_refuse_are_not_a_dead_relay():
    """429 everywhere = the relay answered: today's WARN path, exit 0."""
    server, _handler = serve(set())
    try:
        _tmp, proc, out, log = run_preflight(
            CHAIN, f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        body = text(log)
        assert "verdict=relay_unreachable" not in body, body
        assert "http=0" not in body, body
        assert text(out).strip() == "", text(out)
    finally:
        server.shutdown()
        server.server_close()


@case
def test_empty_chain_stays_a_normal_exit():
    _tmp, proc, out, log = run_preflight([], DEAD_BASE)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "verdict=relay_unreachable" not in text(log), text(log)
    assert text(out).strip() == ""


# ----------------------------------------------------------- Anti-redact
@case
def test_new_lines_carry_numbers_and_model_ids_only():
    server, _handler = serve({"gpt-4.1"}, leak=True)
    try:
        _tmp, proc, _out, log = run_preflight(
            CHAIN[:2], f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        body = text(log)
        assert SECRET not in body, "la clé a fuité"
        for line in stage_lines(body):
            assert "latency_ms=" in line, line
            assert "sk-" not in line, line
            # the measured fields are a status and a timer, never free text
            for token in line.split():
                if token.startswith(("latency_ms=", "http=", "sample=")):
                    _, _, value = token.partition("=")
                    assert value.isdigit(), token
    finally:
        server.shutdown()
        server.server_close()


@case
def test_a_key_cut_by_the_excerpt_boundary_cannot_leak_its_head():
    """Max m7: the excerpt truncates at BODY_EXCERPT, so a key starting just
    before that boundary loses its tail and survives the whole-key replace. Its
    HEAD must be redacted on its own."""
    sys.path.insert(0, ROOT)
    import vision_preflight

    boundary = vision_preflight.BODY_EXCERPT - 12
    body = ("x" * boundary) + SECRET + " tail"
    out = vision_preflight.redact(body, SECRET)
    assert len(out) <= vision_preflight.BODY_EXCERPT, len(out)
    head = SECRET[:8]
    assert head not in out, f"la tête de la clé a fuité: {out[-40:]!r}"
    assert "sk-" not in out, out
    # a whole key inside the excerpt is still fully removed
    whole = vision_preflight.redact("err " + SECRET + " end", SECRET)
    assert SECRET not in whole and "sk-" not in whole, whole


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
    print(f"\n{len(CASES) - failed}/{len(CASES)} vision-preflight cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
