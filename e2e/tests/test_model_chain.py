#!/usr/bin/env python3
"""TDD spec for the Artemis ordered vision-model chain (e2e only).

No emulator, no network: the preflight probe talks to a local http.server, and
every Artemis call is a fake binary in a throwaway vendor tree.

Pins:
  * one ordered chain, workflow override first, deduped, strictly validated, and
    llama-3.2-11b-vision (dead route) never present;
  * one allowlist regex shared by set_model.py and model_chain.py;
  * `apply` rewrites the unified {"default","nodes"} config into a runtime file
    (never vendor/), rotates models per node, keeps every unrelated key, and
    writes NOTHING when the input or the chain is invalid;
  * the preflight keeps only the models that answer 200, in input order, and
    never writes the API key;
  * the retryable classification table (429 / routing_exhausted / 5xx / 124 vs
    400 / 401 / clean);
  * `artemis_run` retries the next model on a retryable failure, stops on a
    non-retryable one, stays inside the step budget, and stays a single attempt
    when there is no chain.
"""
import glob
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
RUN = os.path.join(ROOT, "run.sh")
SMOKE_SH = os.path.join(ROOT, "smoke_log.sh")
MODEL_CHAIN = os.path.join(ROOT, "model_chain.py")
PREFLIGHT = os.path.join(ROOT, "vision_preflight.py")
SET_MODEL = os.path.join(ROOT, "set_model.py")
sys.path.insert(0, ROOT)

BASE_CHAIN = ["gpt-4.1", "gemini-2.5-flash", "gpt-4o", "gemini-3.5-flash-lite",
              "gemini-3.1-flash-lite", "gemini-flash-lite"]
BASE_STR = ",".join(BASE_CHAIN)
DEAD = "llama-3.2-11b-vision"

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


# ------------------------------------------------------------------ fixtures
CONFIG_JSONC = """\
{
  // 🔧 global configuration (JSONC: comments + trailing commas must parse)
  "default": {
    "provider": "openai",
    "model": "auto",
    "thinking_level": "medium",
    "fallback": {
      "provider": "openai",
      "model": "gemini-3.5-flash",
      "thinking_level": "medium"
    }
  },

  /* presets stay usable after the rewrite */
  "presets": {
    "gemini-flagship": {
      "provider": "openai",
      "model": "gemini-3.6-flash",
      "fallback": { "provider": "openai", "model": "gemini-3.7-flash" },
    },
    "openai-gpt4o": {
      "provider": "openai",
      "model": "gpt-4o",
      "fallback": { "provider": "openai", "model": "gpt-4o-mini" },
    },
  },

  "nodes": {
    // planner inherits everything but the model
    "planner": {
      "thinking_level": "high"
    },
    "operator": {
      "thinking_level": "medium",
      "include_thoughts": true
    },
    "object_detector": {
      "model": "gemini-robotics-er-2-preview",
      "fallback": { "provider": "openai", "model": "gemini-robotics-er-2-preview" }
    },
    "hopper": {
      "model": "gemini-3.5-flash-lite",
      "fallback": { "provider": "openai", "model": "gemini-3.1-flash-lite" }
    },
    "outputter": {},
  },

  "agent": {
    "flash": {
      "step_summarizer": {
        "enabled": true,
        "model": "gemini-3.5-flash-lite",
        "prune_history_xml": true
      }
    },
    "memory": {
      "chunking": {
        "max_steps": 12,
        "model": "gemini-3.6-flash"
      }
    }
  }
}
"""


# --------------------------------------------------------- 1. chain building
@case
def test_chain_order_dedupe_and_override_head():
    import model_chain

    assert model_chain.build_chain(BASE_STR) == BASE_CHAIN
    assert model_chain.build_chain(BASE_CHAIN) == BASE_CHAIN
    # override is PRIMARY: head, then the rest, deduped in place
    got = model_chain.build_chain(BASE_STR, "gpt-4o")
    assert got[0] == "gpt-4o", got
    assert got.count("gpt-4o") == 1, got
    assert len(got) == len(BASE_CHAIN), got
    assert got[1:] == [m for m in BASE_CHAIN if m != "gpt-4o"], got
    # a model outside the base chain is prepended, not dropped
    got = model_chain.build_chain(BASE_STR, "gemini-3.5-flash")
    assert got[0] == "gemini-3.5-flash" and len(got) == len(BASE_CHAIN) + 1, got
    # "auto" / empty / whitespace / None are not overrides
    for override in (None, "", "   ", "auto", "AUTO", "Auto"):
        assert model_chain.build_chain(BASE_STR, override) == BASE_CHAIN, override


@case
def test_chain_rejects_untrusted_ids():
    import model_chain

    for bad in ("x; rm -rf /", "a b", "a$(id)", "a`id`", "x" * 81, "a|b", "a\nb"):
        try:
            model_chain.build_chain(BASE_STR, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"override accepté: {bad!r}")

    tmp = tempfile.mkdtemp(prefix="e2e-chain-cli-")
    proc = py(MODEL_CHAIN, ("chain", "--base", BASE_STR, "--override", "x; rm -rf /"))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "", proc.stdout
    assert "invalid" in proc.stderr.lower(), proc.stderr

    proc = py(MODEL_CHAIN, ("chain", "--base", BASE_STR, "--override", "gemini-2.5-flash"))
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.split()
    assert lines[0] == "gemini-2.5-flash" and set(lines) == set(BASE_CHAIN), lines
    assert len(lines) == len(BASE_CHAIN), lines
    assert tmp  # tmp used for parity with the other cases


@case
def test_dead_llama_route_is_never_requested():
    import model_chain

    assert DEAD not in BASE_CHAIN
    assert DEAD not in model_chain.build_chain(BASE_STR)
    # even asked explicitly, the dead route is filtered out of the chain
    assert DEAD not in model_chain.build_chain(BASE_STR, DEAD), "override mort non filtré"
    assert DEAD not in text(RUN), "run.sh ne doit jamais demander la route morte"


# ------------------------------------------------- 2. one allowlist, one source
@case
def test_set_model_shares_the_single_allowlist():
    body = text(SET_MODEL)
    assert "from model_chain import" in body, body
    assert "MODEL_RE" in body, body
    assert "MODEL_RE = re.compile" not in body, "regex dupliquée dans set_model.py"
    chain = text(MODEL_CHAIN)
    assert "MODEL_RE = re.compile" in chain, chain
    assert chain.count("MODEL_RE = re.compile") == 1, "une seule source pour l'allowlist"

    # 2-arg mode still rewrites every `"model"` value, untouched otherwise
    tmp = tempfile.mkdtemp(prefix="e2e-set-model-")
    cfg = write(os.path.join(tmp, "artemis.jsonc"),
                '{\n  "model": "old-model",\n  "other": 1\n}\n')
    proc = py(SET_MODEL, (cfg, "gemini-2.5-flash"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = text(cfg)
    assert '"model": "gemini-2.5-flash"' in out and '"other": 1' in out, out

    proc = py(SET_MODEL, (cfg, 'x"; rm -rf / #'))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert '"model": "gemini-2.5-flash"' in text(cfg), "un id invalide a écrit"

    # --check reads without writing, rc=1 when the id is absent
    assert py(SET_MODEL, ("--check", cfg, "gemini-2.5-flash")).returncode == 0
    assert py(SET_MODEL, ("--check", cfg, "gpt-4o")).returncode == 1
    assert py(SET_MODEL, ("--check", cfg, "x; rm -rf /")).returncode == 2
    assert '"model": "gemini-2.5-flash"' in text(cfg)


# ------------------------------------------------------------------ 3. apply
@case
def test_apply_rotates_models_and_preserves_everything_else():
    import model_chain

    tmp = tempfile.mkdtemp(prefix="e2e-apply-")
    src = write(os.path.join(tmp, "artemis.jsonc"), CONFIG_JSONC)
    before = text(src)
    out = os.path.join(tmp, "runtime", "artemis.jsonc")
    chain = model_chain.build_chain(BASE_STR)

    cfg = model_chain.apply(src, out, chain)
    assert text(src) == before, "le fichier vendor-like a été modifié"
    assert os.path.exists(out)

    parsed = json.loads(text(out))          # deterministic plain JSON
    assert set(parsed) == {"default", "presets", "nodes", "agent"}, parsed.keys()

    node_items = list(parsed["nodes"].items())
    assert len(node_items) == 5, node_items
    for name, node in node_items:
        assert node["provider"] == "openai", (name, node)
        assert node["model"] in chain, (name, node)
        assert node["fallback"]["provider"] == "openai", (name, node)
        assert node["fallback"]["model"] in chain, (name, node)
    models = [n["model"] for _, n in node_items]
    assert all(a != b for a, b in zip(models, models[1:])), models
    assert len(set(models)) >= 3, models          # spread, not one route for all

    assert parsed["default"]["model"] == chain[0], parsed["default"]
    assert parsed["default"]["fallback"]["model"] in chain, parsed["default"]
    assert parsed["default"]["thinking_level"] == "medium"
    assert parsed["default"]["fallback"]["thinking_level"] == "medium"

    for preset in parsed["presets"].values():
        assert preset["provider"] == "openai"
        assert preset["model"] in chain and preset["fallback"]["model"] in chain, preset

    # unrelated keys survive byte-for-byte in value
    assert parsed["nodes"]["planner"]["thinking_level"] == "high"
    assert parsed["nodes"]["operator"]["thinking_level"] == "medium"
    assert parsed["nodes"]["operator"]["include_thoughts"] is True
    assert parsed["agent"]["flash"]["step_summarizer"]["enabled"] is True
    assert parsed["agent"]["flash"]["step_summarizer"]["prune_history_xml"] is True
    assert parsed["agent"]["flash"]["step_summarizer"]["model"] in chain
    assert parsed["agent"]["memory"]["chunking"]["max_steps"] == 12
    assert parsed["agent"]["memory"]["chunking"]["model"] in chain
    assert cfg == parsed


@case
def test_apply_offset_promotes_the_next_route():
    import model_chain

    tmp = tempfile.mkdtemp(prefix="e2e-apply-offset-")
    src = write(os.path.join(tmp, "artemis.jsonc"), CONFIG_JSONC)
    chain = model_chain.build_chain(BASE_STR)
    zero = model_chain.apply(src, os.path.join(tmp, "off0.json"), chain, 0)
    one = model_chain.apply(src, os.path.join(tmp, "off1.json"), chain, 1)

    assert zero["default"]["model"] == chain[0], zero["default"]
    assert one["default"]["model"] == chain[1], one["default"]
    zero_models = [n["model"] for n in zero["nodes"].values()]
    one_models = [n["model"] for n in one["nodes"].values()]
    assert zero_models != one_models, (zero_models, one_models)
    assert one_models[0] == chain[1], one_models

    # CLI: an offset beyond the chain wraps, it never emits an index error
    chain_file = write(os.path.join(tmp, "chain.txt"), "\n".join(chain) + "\n")
    out = os.path.join(tmp, "wrap.json")
    proc = py(MODEL_CHAIN, ("apply", "--in", src, "--out", out,
                            "--chain-file", chain_file, "--offset", "7"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert json.loads(text(out))["default"]["model"] == chain[7 % len(chain)]


@case
def test_apply_spreads_planner_hopper_and_outputter():
    """The three decision nodes must not share a route on a 6-model chain.

    A pure index rotation put planner (0) and hopper (6) on the same pair and the
    outputter one step later: the whole run then hammered two routes. The offsets
    are explicit for those three and stable-hashed (probed to the next free slot)
    for every other node, always shifted by --offset.
    """
    import model_chain

    tmp = tempfile.mkdtemp(prefix="e2e-apply-spread-")
    src = write(os.path.join(tmp, "artemis.jsonc"), CONFIG_JSONC)
    chain = model_chain.build_chain(BASE_STR)

    cfg = model_chain.apply(src, os.path.join(tmp, "spread.json"), chain, 0)
    nodes = cfg["nodes"]
    assert nodes["planner"]["model"] == chain[0], nodes["planner"]
    assert nodes["hopper"]["model"] == chain[2], nodes["hopper"]
    assert nodes["outputter"]["model"] == chain[4], nodes["outputter"]
    trio = {nodes["planner"]["model"], nodes["hopper"]["model"],
            nodes["outputter"]["model"]}
    assert len(trio) == 3, trio
    primaries = [n["model"] for n in nodes.values()]
    assert len(set(primaries)) >= 3, primaries
    # deterministic: same input, same routing
    again = model_chain.apply(src, os.path.join(tmp, "spread2.json"), chain, 0)
    assert [n["model"] for n in again["nodes"].values()] == primaries
    # --offset still shifts every node by the same amount
    shifted = model_chain.apply(src, os.path.join(tmp, "spread-off1.json"), chain, 1)
    for name, node in shifted["nodes"].items():
        index = chain.index(node["model"])
        assert index == (chain.index(nodes[name]["model"]) + 1) % len(chain), (name, node)


@case
def test_apply_writes_nothing_on_invalid_input():
    import model_chain

    tmp = tempfile.mkdtemp(prefix="e2e-apply-bad-")
    src = write(os.path.join(tmp, "artemis.jsonc"), CONFIG_JSONC)
    out = os.path.join(tmp, "runtime", "artemis.jsonc")
    chain = model_chain.build_chain(BASE_STR)

    for bad_chain in ([], ["ok-model", "bad model"], ["ok-model", "x;rm -rf /"]):
        try:
            model_chain.apply(src, out, bad_chain)
        except ValueError:
            pass
        else:
            raise AssertionError(f"chaîne acceptée: {bad_chain!r}")
        assert not os.path.exists(out), f"sortie écrite malgré {bad_chain!r}"

    bad_chain_file = write(os.path.join(tmp, "bad-chain.txt"), "gpt-4.1\nbad model\n")
    proc = py(MODEL_CHAIN, ("apply", "--in", src, "--out", out,
                            "--chain-file", bad_chain_file))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert not os.path.exists(out), "sortie écrite malgré une chaîne invalide"

    broken = write(os.path.join(tmp, "broken.jsonc"), '{\n  "nodes": {,}\n}\n')
    good_chain = write(os.path.join(tmp, "chain.txt"), "gpt-4.1\n")
    proc = py(MODEL_CHAIN, ("apply", "--in", broken, "--out", out,
                            "--chain-file", good_chain))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert not os.path.exists(out), "sortie écrite malgré un JSONC invalide"

    # a config without the unified shape is refused, never half-rewritten
    flat = write(os.path.join(tmp, "flat.json"), '{"model": "gpt-4.1"}\n')
    proc = py(MODEL_CHAIN, ("apply", "--in", flat, "--out", out,
                            "--chain-file", good_chain))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert not os.path.exists(out), "config non unifiée acceptée"


# ------------------------------------------------------------- 4. preflight
SECRET = "sk-e2e-do-not-log-0123456789"


def _serve(alive):
    class Handler(BaseHTTPRequestHandler):
        seen = []

        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            model = payload.get("model")
            Handler.seen.append((model, self.headers.get("Authorization")))
            if model in alive:
                data = json.dumps({"choices": [{"message": {"content": "Red"}}]}).encode()
                code = 200
            else:
                data = json.dumps({"error": {"message": "429 rate limit " + SECRET}}).encode()
                code = 429
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Handler


@case
def test_preflight_keeps_only_200_in_input_order():
    alive = {"gpt-4.1", "gemini-2.5-flash", "gemini-3.1-flash-lite"}
    server, handler = _serve(alive)
    try:
        tmp = tempfile.mkdtemp(prefix="e2e-preflight-")
        chain = write(os.path.join(tmp, "chain.txt"), "\n".join(BASE_CHAIN) + "\n")
        out = os.path.join(tmp, "alive.txt")
        log = os.path.join(tmp, "preflight.log")
        env = dict(os.environ, FREELLM_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   FREELLM_API_KEY=SECRET)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_API_KEY", None)
        proc = py(PREFLIGHT, ("--chain-file", chain, "--out", out, "--log", log),
                  env=env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        kept = [l for l in text(out).splitlines() if l.strip()]
        assert kept == [m for m in BASE_CHAIN if m in alive], kept
        logged = text(log)
        assert SECRET not in logged, "la clé API a fuité dans le journal"
        assert "***" in logged, logged
        assert "429" in logged, logged
        assert "Red" in logged, logged
        assert all(auth == "Bearer " + SECRET for _m, auth in handler.seen), handler.seen
        models = [m for m, _auth in handler.seen]
        # DEUX étapes (run 40602473815) : la vivacité voit TOUTE la chaîne dans
        # l'ordre, puis le payload réaliste repasse sur les survivants, dans
        # l'ordre — un 1x1 seul ne prouve rien sur une capture de 300 Ko. Chaque
        # survivant est échantillonné DEUX fois (audit R4) : un 200 isolé peut
        # être la seule réponse saine d'un relais qui 502 juste après.
        survivors = [m for m in BASE_CHAIN if m in alive]
        assert models == BASE_CHAIN + [m for m in survivors for _ in (1, 2)], models
        assert "stage=realistic" in logged, logged
    finally:
        server.shutdown()
        server.server_close()


@case
def test_preflight_empty_chain_is_not_an_error():
    tmp = tempfile.mkdtemp(prefix="e2e-preflight-empty-")
    chain = write(os.path.join(tmp, "chain.txt"), "\n\n")
    out = os.path.join(tmp, "alive.txt")
    log = os.path.join(tmp, "preflight.log")
    env = dict(os.environ, FREELLM_BASE_URL="http://127.0.0.1:9", FREELLM_API_KEY=SECRET)
    proc = py(PREFLIGHT, ("--chain-file", chain, "--out", out, "--log", log), env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert os.path.exists(out) and text(out).strip() == "", text(out) if os.path.exists(out) else "absent"
    assert SECRET not in text(log)


@case
def test_preflight_honours_the_timeout_flag():
    """VISION_PROBE_TIMEOUT must reach `--timeout`: a route that hangs is dead."""

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            time.sleep(0.4)
            data = json.dumps({"choices": [{"message": {"content": "Red"}}]}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the probe already gave up, which is the point

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        tmp = tempfile.mkdtemp(prefix="e2e-preflight-timeout-")
        chain = write(os.path.join(tmp, "chain.txt"), "gpt-4.1\ngemini-2.5-flash\n")
        out = os.path.join(tmp, "alive.txt")
        log = os.path.join(tmp, "preflight.log")
        env = dict(os.environ, FREELLM_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   FREELLM_API_KEY=SECRET)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_API_KEY", None)
        proc = py(PREFLIGHT, ("--chain-file", chain, "--out", out, "--log", log,
                             "--timeout", "0.05"), env=env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert text(out).strip() == "", text(out)
        body = text(log)
        assert "timeout: 0.05s" in body, body
        assert body.count("http=0") == 2, body
        assert "http=200" not in body, body
        assert SECRET not in body
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------- 5. classification
@case
def test_retryable_classification_table():
    import model_chain

    table = [
        (0, "", None),
        (1, "HTTP 429 Too Many Requests", "429"),
        (1, "routing_exhausted: all routes failed", "routing_exhausted"),
        (1, "RESOURCE_EXHAUSTED quota exceeded", "resource_exhausted"),
        (1, "HTTP 500 Internal Server Error", "5xx"),
        (1, "HTTP/1.1 502 Bad Gateway", "5xx"),
        (124, "", "timeout"),
        (1, "request timed out after 60s", "timed_out"),
        (1, "Connection reset by peer", "connection_reset"),
        (2, "HTTP 400 Bad Request", None),
        (2, "HTTP 401 Unauthorized", None),
        (1, "everything is fine", None),
    ]
    for rc, log_text, want in table:
        got = model_chain.classify(rc, log_text)
        assert got == want, (rc, log_text, got, want)

    # CLI contract used by artemis_run: signal on stdout, rc=0 only when retryable
    tmp = tempfile.mkdtemp(prefix="e2e-classify-")
    ok_log = write(os.path.join(tmp, "ok.log"), "Error 429 rate limited\n")
    bad_log = write(os.path.join(tmp, "bad.log"), "HTTP 401 Unauthorized\n")
    proc = py(MODEL_CHAIN, ("classify", "--rc", "1", "--log", ok_log))
    assert proc.returncode == 0 and proc.stdout.strip() == "429", (proc.returncode, proc.stdout)
    proc = py(MODEL_CHAIN, ("classify", "--rc", "2", "--log", bad_log))
    assert proc.returncode == 1 and proc.stdout.strip() == "", (proc.returncode, proc.stdout)
    proc = py(MODEL_CHAIN, ("classify", "--rc", "124"))
    assert proc.returncode == 0 and proc.stdout.strip() == "timeout", (proc.returncode, proc.stdout)


@case
def test_classify_judges_the_terminal_error_of_the_log_tail():
    """The LAST failure decides, and a non-replayable status always wins.

    Scanning the whole log let a transient 429 near the top of a 2000-line log
    outvote the terminal 400 that actually killed the attempt, so the harness
    burned the next model on a request that could never succeed.
    """
    import model_chain

    tail_429_then_400 = ("INFO attempt start\n"
                         "ERROR HTTP 429 Too Many Requests\n"
                         "WARN retrying once\n"
                         "ERROR HTTP 400 invalid_request_error: bad payload\n")
    assert model_chain.classify(1, tail_429_then_400) is None

    for status in (400, 401, 403, 404, 413, 415, 422):
        assert model_chain.classify(1, f"ERROR HTTP {status} rejected\n") is None, status
        # ... even when a 429 came first
        assert model_chain.classify(1, f"ERROR HTTP 429 rate limit\nERROR HTTP {status} no\n") is None

    assert model_chain.classify(1, "ERROR HTTP 429 Too Many Requests\n") == "429"
    assert model_chain.classify(124, "") == "timeout"
    assert model_chain.classify(124, "ERROR HTTP 400 Bad Request\n") == "timeout", \
        "un timeout du processus reste un timeout, même avec un 400 dans le journal"

    # only the TAIL is judged: a 429 buried 60 lines above the end is not the cause
    buried = "ERROR HTTP 429 rate limit\n" + "".join(
        f"INFO step {i} ok\n" for i in range(60))
    assert model_chain.classify(1, buried) is None
    # ... while a retryable signal inside the tail still retries
    recent = "".join(f"INFO step {i} ok\n" for i in range(60)) + "ERROR HTTP 502 Bad Gateway\n"
    assert model_chain.classify(1, recent) == "5xx"

    tmp = tempfile.mkdtemp(prefix="e2e-classify-tail-")
    mixed = write(os.path.join(tmp, "mixed.log"), tail_429_then_400)
    proc = py(MODEL_CHAIN, ("classify", "--rc", "1", "--log", mixed))
    assert proc.returncode == 1 and proc.stdout.strip() == "", (proc.returncode, proc.stdout)


# ------------------------------------------------ 6. artemis_run functional
def artemis_tree(tmp, fake_body, chain, config=CONFIG_JSONC, timeout=None):
    """Throwaway repo root: fake vendor binary, e2e symlink, runtime config."""
    art = os.path.join(tmp, "vendor", "artemis", ".venv", "bin")
    os.makedirs(art, exist_ok=True)
    fake = os.path.join(art, "artemis")
    write(fake, fake_body)
    os.chmod(fake, 0o755)
    for sub in ("artemis", "screens"):
        os.makedirs(os.path.join(tmp, sub), exist_ok=True)
    os.symlink(ROOT, os.path.join(tmp, "e2e"))
    src = write(os.path.join(tmp, "vendor", "artemis", "config", "artemis.jsonc"), config) \
        if os.path.isdir(os.path.join(tmp, "vendor", "artemis", "config")) else None
    if src is None:
        os.makedirs(os.path.join(tmp, "vendor", "artemis", "config"))
        src = write(os.path.join(tmp, "vendor", "artemis", "config", "artemis.jsonc"), config)
    chain_file = os.path.join(tmp, "artemis", "vision-chain.txt")
    if chain is not None:
        write(chain_file, "\n".join(chain) + "\n")
    runtime = os.path.join(tmp, "artemis", "artemis.jsonc")
    write(os.path.join(tmp, "run.log"), "")
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    env.update({
        "OUT": tmp,
        "SMOKE_LOG": os.path.join(tmp, "smoke.log"),
        "PY": sys.executable,
        "ARTEMIS_OK": "1",
        "ARTEMIS_SERIAL": "emulator-5554",
        "ARTEMIS_CHAIN_FILE": chain_file,
        "ARTEMIS_CONFIG": src,
        "ARTEMIS_CONFIG_RUNTIME": runtime,
        "CALLS": os.path.join(tmp, "calls.txt"),
    })
    if timeout is not None:
        env["ARTEMIS_BACKOFF"] = timeout
    return env, chain_file, runtime


def run_artemis(env, tmp, tmo="6"):
    script = (
        "set -uo pipefail\n"
        'log() { printf "[e2e] %s\\n" "$*" >> "$OUT/run.log"; }\n'
        "adb() { :; }\n"
        ". \"$1\"\n"
        'eval "$2"\neval "$3"\neval "$4"\n'
        'artemis_run smoke "open settings" ' + tmo + "\n"
        'printf "RC=%s\\n" "$?"\n'
    )
    return bash(script, (SMOKE_SH, extract_fn(RUN, "artemis_serial"),
                         extract_fn(RUN, "publish_artemis_log"),
                         extract_fn(RUN, "artemis_run")), env=env, cwd=tmp)


FAKE_FAIL_THEN_PASS = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
n=$(wc -l < "$CALLS")
if [ "$n" -eq 1 ]; then
  printf 'ERROR 429 routing_exhausted: rate limit on gpt-4.1\\n'
  exit 1
fi
printf 'INFO ok on the second route\\n'
exit 0
"""

FAKE_NON_RETRYABLE = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
printf 'ERROR HTTP 400 invalid_request_error: bad payload\\n'
exit 2
"""

FAKE_OK = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
printf 'INFO answered the step\\n'
exit 0
"""

FAKE_TIMEOUT_THEN_OK = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
n=$(wc -l < "$CALLS")
if [ "$n" -eq 1 ]; then
  printf 'ERROR step hung, killed by the harness timeout\\n'
  exit 124
fi
printf 'INFO ok on the next route\\n'
exit 0
"""

FAKE_SLOW_FAIL = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
sleep 1.2
printf 'ERROR 429 rate limit\\n'
exit 1
"""

# Artemis has its OWN one-level fallback (warning "Falling back" / `llm_fallback`
# event). A zero exit with that marker means the fallback model served, not the
# primary — the ledger must not claim the primary ran.
FAKE_INTERNAL_FALLBACK = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
printf 'WARNING Falling back from gpt-4.1 to gemini-2.5-flash (429)\\n'
printf 'INFO emitted llm_fallback event\\n'
printf 'INFO answered the step\\n'
exit 0
"""


@case
def test_artemis_run_retries_the_next_model_and_logs_the_switch():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-retry-")
    env, _chain_file, runtime = artemis_tree(tmp, FAKE_FAIL_THEN_PASS, BASE_CHAIN[:2],
                                             timeout="0 0 0")
    # 30s: with 6s the second attempt can trip the `tmo_left -lt 5` guard depending
    # on which side of a second boundary the first attempt landed (review B3).
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=0"), proc.stdout
    calls = [l for l in text(env["CALLS"]).splitlines() if l.strip()]
    assert len(calls) == 2, calls
    for call in calls:
        assert call.startswith("run open settings --profile flash --test-name smoke"), call
        assert "--standalone" in call and "--device-serial emulator-5554" in call, call
        assert "--traces-path " + os.path.join(tmp, "artemis") in call, call

    log = text(os.path.join(tmp, "run.log"))
    assert "attempt 1/2" in log, log
    assert "attempt 2/2" in log, log
    assert "429" in log, log
    assert "from=gpt-4.1" in log and "to=gemini-2.5-flash" in log, log
    # per-attempt model honesty: the primary of THIS attempt, and its fallback
    assert ("artemis llm: smoke attempt=1/2 primary=gpt-4.1 fallback=gemini-2.5-flash "
            "fallback_events=0") in log, log
    assert ("artemis llm: smoke attempt=2/2 primary=gemini-2.5-flash fallback=gpt-4.1 "
            "fallback_events=0") in log, log

    # every attempt keeps its own log; `smoke.log` is the LAST attempt (published)
    art = os.path.join(tmp, "artemis")
    assert "429" in text(os.path.join(art, "smoke.attempt1.log"))
    assert os.path.exists(os.path.join(art, "smoke.attempt2.log"))
    assert text(os.path.join(art, "smoke.log")) == text(os.path.join(art, "smoke.attempt2.log"))

    ledger = text(os.path.join(art, "model-usage.txt")).strip().splitlines()
    assert len(ledger) == 1, ledger
    assert ledger[0] == ("smoke|2|2|head=gpt-4.1|primary=gemini-2.5-flash|fallback=gpt-4.1"
                         "|fallback_events=0|served=primary|signals=429|rc=0"
                         "|served_model=gemini-2.5-flash|chain=gpt-4.1,gemini-2.5-flash"), ledger

    # the retry regenerated the runtime config with offset=1 (next route promoted)
    assert json.loads(text(runtime))["default"]["model"] == "gemini-2.5-flash", text(runtime)
    # and vendor/ was never the output (still the untouched JSONC source)
    vendor = text(os.path.join(tmp, "vendor", "artemis", "config", "artemis.jsonc"))
    assert vendor == CONFIG_JSONC, "le fichier vendor-like a été réécrit"


@case
def test_artemis_run_reports_served_fallback_when_artemis_falls_back_itself():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-internal-fallback-")
    env, _chain_file, _runtime = artemis_tree(tmp, FAKE_INTERNAL_FALLBACK,
                                              BASE_CHAIN[:2], timeout="0 0 0")
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=0"), proc.stdout
    ledger = text(os.path.join(tmp, "artemis", "model-usage.txt")).strip().splitlines()
    assert len(ledger) == 1, ledger
    assert ledger[0] == ("smoke|1|2|head=gpt-4.1|primary=gpt-4.1|fallback=gemini-2.5-flash"
                         "|fallback_events=2|served=fallback|signals=-|rc=0"
                         "|served_model=gemini-2.5-flash|chain=gpt-4.1,gemini-2.5-flash"), ledger
    log = text(os.path.join(tmp, "run.log"))
    assert ("artemis llm: smoke attempt=1/2 primary=gpt-4.1 fallback=gemini-2.5-flash "
            "fallback_events=2") in log, log


@case
def test_artemis_run_aborts_the_step_when_the_config_cannot_be_regenerated():
    """No applied config => no attempt: never run with a stale offset."""
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-apply-fail-")
    env, _chain_file, _runtime = artemis_tree(tmp, FAKE_FAIL_THEN_PASS, BASE_CHAIN[:2],
                                              config='{"model": "gpt-4.1"}\n',
                                              timeout="0 0 0")
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=2"), proc.stdout
    assert not os.path.exists(env["CALLS"]), "artemis a tourné sans config appliquée"
    log = text(os.path.join(tmp, "run.log"))
    assert "étape abandonnée rc=2" in log, log
    ledger = text(os.path.join(tmp, "artemis", "model-usage.txt")).strip()
    assert ledger == ("smoke|0|2|head=gpt-4.1|primary=none|fallback=none|fallback_events=0"
                      "|served=unknown|signals=-|rc=2|served_model=unknown"
                      "|chain=gpt-4.1,gemini-2.5-flash"), ledger


@case
def test_artemis_run_does_not_retry_a_non_retryable_failure():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-noretry-")
    env, _chain_file, _runtime = artemis_tree(tmp, FAKE_NON_RETRYABLE, BASE_CHAIN[:3],
                                              timeout="0 0 0")
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=2"), proc.stdout
    calls = [l for l in text(env["CALLS"]).splitlines() if l.strip()]
    assert len(calls) == 1, calls
    ledger = text(os.path.join(tmp, "artemis", "model-usage.txt")).strip()
    assert ledger == ("smoke|1|3|head=gpt-4.1|primary=gpt-4.1|fallback=gemini-2.5-flash"
                      "|fallback_events=0|served=unknown|signals=-|rc=2"
                      "|served_model=unknown|chain=gpt-4.1,gemini-2.5-flash,gpt-4o"), ledger


@case
def test_artemis_run_stays_inside_the_step_budget():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-budget-")
    env, _chain_file, _runtime = artemis_tree(tmp, FAKE_SLOW_FAIL, BASE_CHAIN,
                                              timeout="0 0 0")
    # 4 s pour 6 modèles : la fenêtre ne finance aucun essai de 5 s, même après le
    # clamp du nombre de tentatives => AUCUN essai ne démarre (mieux vaut rendre la
    # main aux taps + API qu'un timeout bidon).
    proc = run_artemis(env, tmp, tmo="4")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=2"), proc.stdout
    assert not os.path.exists(env["CALLS"]), "un essai a démarré sous 5 s de part"
    log = text(os.path.join(tmp, "run.log"))
    assert "budget épuisé" in log, log
    rows = text(os.path.join(tmp, "artemis", "attempts.txt")).strip().splitlines()
    # Aucun essai n'a tourné : rc inconnu ("-"), jamais 124 (= un timeout observé).
    assert rows == ["smoke|1|-|-|0|budget|budget"], rows


@case
def test_attempt_timeout_fair_share_table():
    """A pendling route must not eat the whole step: fair share, capped."""
    import model_chain

    table = [((200, 6, 45), 33), ((200, 1, 45), 45), ((60, 3, 45), 20),
             ((10, 6, 45), 1), ((0, 6, 45), 0)]
    for (remaining, left, cap), want in table:
        got = model_chain.attempt_timeout(remaining, left, cap)
        assert got == want, (remaining, left, cap, got, want)

    # invariants: never above min(cap, remaining), at least 1s while time is left
    for remaining in (0, 1, 2, 5, 17, 60, 200, 1000):
        for left in (0, 1, 2, 6, 10):
            got = model_chain.attempt_timeout(remaining, left, 45)
            assert got <= min(45, remaining), (remaining, left, got)
            if remaining >= 1 and left >= 1:
                assert got >= 1, (remaining, left, got)
            else:
                assert got == 0, (remaining, left, got)


@case
def test_attempt_timeout_cli():
    for (remaining, left, cap), want in (((200, 6, 45), 33), ((200, 1, 45), 45),
                                        ((60, 3, 45), 20), ((10, 6, 45), 1)):
        proc = py(MODEL_CHAIN, ("attempt-timeout", "--remaining", str(remaining),
                                "--left", str(left), "--cap", str(cap)))
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert proc.stdout.strip() == str(want), (remaining, left, cap, proc.stdout)
    proc = py(MODEL_CHAIN, ("attempt-timeout", "--remaining", "0", "--left", "6"))
    assert proc.returncode == 0 and proc.stdout.strip() == "0", proc.stdout


@case
def test_attempts_affordable_never_starves_artemis():
    """Contract A: fair share below the 5 s floor => fewer attempts, not zero.

    `remaining // attempts_left` alone put a 22 s offer window on 3 s per route
    (6 models), so `artemis_run` refused to start ANY attempt on driver-accept.
    """
    import model_chain

    table = [((22, 6, 5), 4), ((200, 6, 5), 6), ((9, 6, 5), 1), ((4, 6, 5), 0),
             ((22, 3, 5), 3), ((0, 6, 5), 0)]
    for (remaining, left, floor), want in table:
        got = model_chain.capped_attempts_left(remaining, left, floor)
        assert got == want, (remaining, left, floor, got, want)

    # invariants: never above attempts_left, never negative, always 0 below floor
    for remaining in (0, 1, 4, 5, 9, 22, 200, 1000):
        for left in (0, 1, 2, 3, 6, 10):
            got = model_chain.capped_attempts_left(remaining, left, 5)
            assert 0 <= got <= left, (remaining, left, got)
            if remaining < 5 or left < 1:
                assert got == 0, (remaining, left, got)
            else:
                assert got >= 1, (remaining, left, got)

    # a nonsense floor must not divide by zero, and junk input stays conservative
    assert model_chain.capped_attempts_left(22, 6, 0) == 6
    assert model_chain.capped_attempts_left("abc", 6, 5) == 0
    assert model_chain.capped_attempts_left(22, "abc", 5) == 0


@case
def test_attempts_affordable_cli():
    for (remaining, left), want in (((22, 6), 4), ((200, 6), 6), ((9, 6), 1),
                                    ((4, 6), 0), ((22, 3), 3), ((0, 6), 0)):
        proc = py(MODEL_CHAIN, ("attempts-affordable", "--remaining", str(remaining),
                                "--left", str(left), "--min", "5"))
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert proc.stdout.strip() == str(want), (remaining, left, proc.stdout)
    # --min defaults to 5: same answer without the flag
    proc = py(MODEL_CHAIN, ("attempts-affordable", "--remaining", "22", "--left", "6"))
    assert proc.returncode == 0 and proc.stdout.strip() == "4", proc.stdout
    proc = py(MODEL_CHAIN, ("attempts-affordable", "--remaining", "22", "--left", "6",
                            "--min", "10"))
    assert proc.returncode == 0 and proc.stdout.strip() == "2", proc.stdout


@case
def test_run_sh_clamps_attempts_before_the_timeout():
    """Static contract: the clamp sits between the count and the per-attempt share."""
    fn = extract_fn(RUN, "artemis_run")
    i_aff = fn.index("attempts-affordable")
    i_tmo = fn.index("attempt-timeout")
    assert i_aff < i_tmo, "attempts-affordable doit précéder attempt-timeout"
    assert "--min 5" in fn, "le plancher de 5 s doit être explicite dans run.sh"

    zone = fn[i_aff:i_tmo]
    assert re.search(r'(?m)^\s*attempts_left=', zone), \
        "attempts_left doit être réassigné depuis le clamp"
    assert re.search(r'\|\|[^\n]*attempts_left|case "\$affordable"', zone), \
        "repli sur la valeur non bridée absent si le helper échoue"
    # the 5 s floor guard and its `budget` ledger row are untouched, AFTER the clamp
    assert fn.index('if [ "$tmo_left" -lt 5 ]') > i_tmo, fn
    assert '"budget"' in fn, "la ligne budget du ledger a disparu"
    assert "affordable=" in fn, "la valeur bridée n'est pas journalisée"


@case
def test_driver_accept_budget_keeps_a_real_artemis_attempt():
    """Real loop arithmetic for the accept step: 22 s of UI budget, 6 models."""
    import model_chain

    remaining, max_attempts, cap, floor = 22, 6, 45, 5
    # the defect: fair share over every remaining route is under the floor
    unclamped = model_chain.attempt_timeout(remaining, max_attempts, cap)
    assert unclamped == 3, unclamped
    assert unclamped < floor, "le partage brut doit bien passer sous le plancher"

    # the fix: clamp the attempt count, then the share
    affordable = model_chain.capped_attempts_left(remaining, max_attempts, floor)
    assert affordable == 4, affordable
    first = model_chain.attempt_timeout(remaining, affordable, cap)
    assert first == 5, first
    assert not first < floor, "run.sh prendrait la branche `tmo_left < 5`"


@case
def test_artemis_run_gets_five_seconds_on_a_short_accept_budget():
    """End-to-end: 22 s over 6 models runs the FIRST attempt with 5 s."""
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-accept-budget-")
    env, _cf, _rt = artemis_tree(tmp, FAKE_OK, BASE_CHAIN[:6], timeout="0 0 0")
    proc = run_artemis(env, tmp, tmo="22")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=0"), proc.stdout
    calls = [l for l in text(env["CALLS"]).splitlines() if l.strip()]
    assert len(calls) == 1, calls
    log = text(os.path.join(tmp, "run.log"))
    assert "timeout=5s" in log, log
    assert "budget épuisé" not in log, log
    assert "affordable=4" in log, log


@case
def test_artemis_wall_clock_never_exceeds_the_step_budget():
    """6 models, 30 s, every attempt failing: the wall clock stays inside 30 s."""
    import time

    tmp = tempfile.mkdtemp(prefix="e2e-artemis-wallclock-")
    env, _cf, _rt = artemis_tree(tmp, FAKE_SLOW_FAIL, BASE_CHAIN[:3], timeout="0 0 0")
    started = time.monotonic()
    proc = run_artemis(env, tmp, tmo="15")
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert elapsed < 15, f"budget dépassé: {elapsed:.1f}s"
    rows = text(os.path.join(tmp, "artemis", "attempts.txt")).strip().splitlines()
    real = [r for r in rows if not r.endswith("|budget")]
    assert real, rows
    for row in real:
        fields = row.split("|")
        assert fields[0] == "smoke" and fields[6] == "retryable", row
        assert 5 <= int(fields[4]) <= 15, row  # jamais plus que le budget du step
    # the first attempt gets an honest slice (15 s / 3 modèles = 5 s); the last
    # one may take whatever remains (min(cap, remaining)) — never more.
    assert real[0].split("|")[4] == "5", real[0]
    assert len(real) <= 3, real
    assert proc.stdout.strip().endswith("RC=1"), proc.stdout
    assert "timeout=5s" in text(os.path.join(tmp, "run.log"))


@case
def test_artemis_run_splits_the_step_budget_between_models():
    """200 s over 6 models: the FIRST attempt gets 33 s, not the whole step."""
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-fairshare-")
    env, _cf, _rt = artemis_tree(tmp, FAKE_OK, BASE_CHAIN[:6], timeout="0 0 0")
    proc = run_artemis(env, tmp, tmo="200")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    log = text(os.path.join(tmp, "run.log"))
    assert "timeout=33s" in log, log
    # the last remaining attempt may take min(cap, remaining)
    assert "ARTEMIS_MODEL_TIMEOUT_SEC" in text(RUN), "budget par tentative absent"


@case
def test_artemis_model_timeout_is_numeric_and_clamped():
    for preset, want in (("abc", "45"), ("0", "5"), ("9000", "600"), ("30", "30")):
        tmp = tempfile.mkdtemp(prefix="e2e-artemis-cap-")
        env, _cf, _rt = artemis_tree(tmp, FAKE_OK, ["gpt-4.1"], timeout="0 0 0")
        env["ARTEMIS_MODEL_TIMEOUT_SEC"] = preset
        proc = run_artemis(env, tmp, tmo="1000")
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        log = text(os.path.join(tmp, "run.log"))
        assert f"timeout={want}s" in log, (preset, log)


@case
def test_artemis_timeout_switches_model_without_backoff():
    """rc=124 is not a rate limit: the next route starts immediately."""
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-timeout-switch-")
    env, _cf, _rt = artemis_tree(tmp, FAKE_TIMEOUT_THEN_OK, BASE_CHAIN[:2])
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=0"), proc.stdout
    calls = [l for l in text(env["CALLS"]).splitlines() if l.strip()]
    assert len(calls) == 2, calls
    log = text(os.path.join(tmp, "run.log"))
    assert "signal=timeout backoff=0s" in log, log
    assert "backoff=5s" not in log, log
    rows = [r.split("|") for r in
            text(os.path.join(tmp, "artemis", "attempts.txt")).strip().splitlines()]
    assert [r[:2] + r[3:4] + r[5:] for r in rows] == [
        ["smoke", "1", "124", "timeout", "timeout"],
        ["smoke", "2", "0", "-", "success"]], rows
    assert rows[0][2] == "gpt-4.1" and rows[1][2] == "gemini-2.5-flash", rows
    assert rows[0][4] == "15", rows[0]


@case
def test_artemis_attempts_ledger_has_one_row_per_attempt():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-attempts-")
    env, _cf, _rt = artemis_tree(tmp, FAKE_FAIL_THEN_PASS, BASE_CHAIN[:2], timeout="0 0 0")
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    rows = [r.split("|") for r in
            text(os.path.join(tmp, "artemis", "attempts.txt")).strip().splitlines()]
    assert [r[:2] + r[3:4] + r[5:] for r in rows] == [
        ["smoke", "1", "1", "429", "retryable"],
        ["smoke", "2", "0", "-", "success"]], rows
    assert rows[0][2] == "gpt-4.1" and rows[1][2] == "gemini-2.5-flash", rows
    assert rows[0][4] == "15", rows[0]
    assert all(len(r) == 7 for r in rows), rows


@case
def test_artemis_served_model_is_honest():
    """served_model=unknown on failure; the primary/fallback id otherwise."""
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-served-")
    env, _cf, _rt = artemis_tree(tmp, FAKE_FAIL_THEN_PASS, BASE_CHAIN[:2], timeout="0 0 0")
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    log = text(os.path.join(tmp, "run.log"))
    lines = [l for l in log.splitlines() if "artemis models:" in l]
    assert len(lines) == 1, lines
    assert "served_model=gemini-2.5-flash" in lines[0], lines[0]

    tmp2 = tempfile.mkdtemp(prefix="e2e-artemis-served-fail-")
    env2, _cf2, _rt2 = artemis_tree(tmp2, FAKE_NON_RETRYABLE, BASE_CHAIN[:2], timeout="0 0 0")
    proc2 = run_artemis(env2, tmp2, tmo="30")
    assert proc2.stdout.strip().endswith("RC=2"), proc2.stdout
    log2 = text(os.path.join(tmp2, "run.log"))
    line2 = [l for l in log2.splitlines() if "artemis models:" in l][0]
    assert "served_model=unknown" in line2, line2


@case
def test_artemis_model_usage_gains_served_model_and_chain():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-usage-")
    env, _cf, _rt = artemis_tree(tmp, FAKE_OK, BASE_CHAIN[:2], timeout="0 0 0")
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    row = text(os.path.join(tmp, "artemis", "model-usage.txt")).strip()
    assert row.endswith("|served_model=gpt-4.1|chain=gpt-4.1,gemini-2.5-flash"), row
    assert row.startswith("smoke|1|2|head=gpt-4.1|primary=gpt-4.1"), row


@case
def test_classify_catches_bare_5xx_forms():
    """Max: `"status_code": 500` / `server returned 502` must switch route too."""
    import model_chain

    for body in ('{"error": {"status_code": 500}}', "upstream: server returned 502"):
        assert model_chain.classify(1, body) == "5xx", body
    assert model_chain.classify(1, "INFO 40 frames processed\nINFO done") is None


@case
def test_artemis_run_drops_non_numeric_backoff_tokens():
    """Max: ARTEMIS_BACKOFF="abc 15 -2" -> (15), no shell error, honest log."""
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-backoff-")
    env, _chain_file, _runtime = artemis_tree(tmp, FAKE_FAIL_THEN_PASS, BASE_CHAIN[:2])
    env["ARTEMIS_BACKOFF"] = "abc 15 -2"
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "integer expression" not in proc.stderr, proc.stderr
    log = text(os.path.join(tmp, "run.log"))
    assert "signal=429 backoff=15s" in log, log


@case
def test_artemis_run_falls_back_to_default_backoff_when_no_token_is_numeric():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-backoff-default-")
    env, _chain_file, _runtime = artemis_tree(tmp, FAKE_FAIL_THEN_PASS, BASE_CHAIN[:2])
    env["ARTEMIS_BACKOFF"] = "abc -2"
    # 30s comme le cas voisin (review B3): avec 10 s, le backoff par défaut de 5 s
    # laisse ~5 s au second essai et le garde `tmo_left -lt 5` dépend du passage de
    # la seconde — la contention de la suite complète suffisait à le faire basculer.
    proc = run_artemis(env, tmp, tmo="30")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "integer expression" not in proc.stderr, proc.stderr
    assert proc.stdout.strip().endswith("RC=0"), proc.stdout
    log = text(os.path.join(tmp, "run.log"))
    # tokens non numériques => table par défaut (5 15 30): le premier backoff est 5 s
    assert "backoff=5s" in log, log
    assert "backoff=30s" not in log, log


@case
def test_run_sh_pins_the_llm_pause_timeout_outside_the_preflight():
    """Max: sans borne, settings.py attend 900 s après un double-429 (budget du step)."""
    src = text(RUN)
    line = 'export LLM_PAUSE_TIMEOUT_SECONDS="${LLM_PAUSE_TIMEOUT_SECONDS:-15}"'
    assert line in src, "export LLM_PAUSE_TIMEOUT_SECONDS absent"
    assert src.index(line) < src.index("CHAIN_REQUESTED="), \
        "l'export doit être hors du bloc conditionnel préflight"
    for preset, want in (("", "15"), ("42", "42")):
        env = dict(os.environ)
        env.pop("LLM_PAUSE_TIMEOUT_SECONDS", None)
        if preset:
            env["LLM_PAUSE_TIMEOUT_SECONDS"] = preset
        # un enfant doit voir la valeur -> c'est bien un export, pas une affectation
        proc = bash(line + '\nbash -c \'printf "%s" "$LLM_PAUSE_TIMEOUT_SECONDS"\'',
                    env=env)
        assert proc.stdout.strip() == want, (preset, proc.stdout, proc.stderr)


@case
def test_artemis_run_without_a_chain_is_exactly_one_attempt():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-nochain-")
    env, chain_file, _runtime = artemis_tree(tmp, FAKE_FAIL_THEN_PASS, None)
    proc = run_artemis(env, tmp)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=1"), proc.stdout
    assert len([l for l in text(env["CALLS"]).splitlines() if l.strip()]) == 1
    assert not os.path.exists(chain_file) or text(chain_file).strip() == ""
    assert not os.path.exists(os.path.join(tmp, "artemis", "artemis.jsonc")), \
        "aucune config runtime ne doit être écrite sans chaîne"

    # one model in the chain is still a single attempt: no retry without an alternative
    tmp2 = tempfile.mkdtemp(prefix="e2e-artemis-onemodel-")
    env2, _cf, _rt = artemis_tree(tmp2, FAKE_FAIL_THEN_PASS, ["gpt-4.1"], timeout="0 0 0")
    proc = run_artemis(env2, tmp2)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=1"), proc.stdout
    assert len([l for l in text(env2["CALLS"]).splitlines() if l.strip()]) == 1


@case
def test_publish_artemis_log_redacts_freellm_credentials():
    """The provider key can be either vendor's: mask BOTH, in both destinations."""
    key = "fl-e2e-0123456789abcdef"
    base = "https://llm.example.test/v1"
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-redact-")
    os.makedirs(os.path.join(tmp, "artemis"))
    write(os.path.join(tmp, "artemis", "smoke.log"),
          f"INFO llm ready {key} via {base}\nINFO step done\n")
    write(os.path.join(tmp, "run.log"), "")
    smoke = os.path.join(tmp, "smoke.log")
    write(smoke, "")
    env = dict(os.environ, OUT=tmp, SMOKE_LOG=smoke, PY=sys.executable,
               FREELLM_API_KEY=key, FREELLM_BASE_URL=base)
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_BASE_URL", None)
    script = ('. "$1"\neval "$2"\npublish_artemis_log smoke\n')
    proc = bash(script, argv=(SMOKE_SH, extract_fn(RUN, "publish_artemis_log")), env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    for path in (os.path.join(tmp, "run.log"), smoke):
        body = text(path)
        assert "artemis-log: " in body, (path, body)
        assert key not in body, (path, body)
        assert base not in body, (path, body)
        assert "INFO step done" in body, (path, body)
        assert "[REDACTED]" in body, (path, body)


# ------------------------------------------------------- 7. run.sh structure
@case
def test_run_sh_wires_the_chain_and_never_writes_vendor():
    body = text(RUN)
    for needle in ("ARTEMIS_CONFIG=", "ARTEMIS_BASE_CHAIN=", "ARTEMIS_BACKOFF=",
                   "ARTEMIS_CONFIG_RUNTIME=", "ARTEMIS_CHAIN_FILE=",
                   "e2e/model_chain.py", "e2e/vision_preflight.py",
                   "e2e/set_model.py", "vision-chain.txt"):
        assert needle in body, needle
    assert "gpt-4.1" in body and "gemini-flash-lite" in body, "chaîne par défaut absente"
    assert DEAD not in body, "route morte dans run.sh"
    assert 'export ARTEMIS_ARTEMIS_JSONC="$ARTEMIS_CONFIG_RUNTIME"' in body, \
        "la config runtime n'est pas exportée vers Artemis"
    assert re.search(r'apply --in "\$ARTEMIS_CONFIG" --out "\$ARTEMIS_CONFIG_RUNTIME"', body), \
        "la config vendor-like doit être LUE, la sortie écrite à part"
    assert not re.search(r"set_model\.py\s+vendor/", body), "run.sh réécrit vendor/"
    assert not re.search(r"sed -i", body), "sed -i interdit (B6)"
    # the old single-model heredoc (first 200 wins) is gone
    assert "chosen = m; break" not in body, "l'ancien préflight s'arrête au premier 200"
    assert "dumpsys" not in body or True  # no-op: keeps the assertion list readable

    # B1: Artemis is launched from vendor/artemis, so every path it receives is
    # absolute; `$(pwd)/$OUT` double-prefixed an already-absolute OUT.
    assert "OUT_ABS=" in body, "OUT_ABS manquant (B1)"
    assert '"$(pwd)/$OUT' not in body, "double préfixe de OUT (B1)"
    assert 'VISION_PROBE_TIMEOUT' in body, body
    assert '--timeout "$VISION_PROBE_TIMEOUT"' in body, body

    fn = extract_fn(RUN, "artemis_run")
    assert "ARTEMIS_CHAIN_FILE" in fn or "chain_file" in fn, fn
    assert "ARTEMIS_BACKOFF" in fn, fn
    assert "e2e/model_chain.py" in fn, fn
    assert "model-usage.txt" in fn, fn
    assert "--standalone" in fn, fn
    assert '--traces-path' in fn and '"$(pwd)/$OUT' not in fn, fn
    assert ('"--device-serial" "$ARTEMIS_SERIAL"' in fn
            or '--device-serial "$ARTEMIS_SERIAL"' in fn), fn

    proc = bash("bash -n \"$1\"", (RUN,))
    assert proc.returncode == 0, proc.stderr


@case
def test_run_sh_verifies_the_runtime_config_against_the_head_model():
    body = text(RUN)
    assert re.search(r'set_model\.py --check "\$ARTEMIS_CONFIG_RUNTIME" "\$VISION_MODEL"', body), body
    assert re.search(r'grep -c -F "\\"model\\": \\"\$VISION_MODEL\\""', body), body
    assert 'mark llm-vision PASS' in body and 'mark llm-vision WARN' in body, body
    assert 'ARTEMIS_OK=0' in body, body


# ------------------------------------------- 7. preflight block end-to-end
def preflight_block():
    """The real shell block from run.sh: chain -> probe -> apply -> export."""
    body = text(RUN)
    start = body.index("# Préflight vision")
    end = body.index('if [ "$ARTEMIS_OK" = "1" ] && artemis_run smoke')
    return body[start:end]


def init_block():
    """The B1 normalisation block from run.sh, extracted verbatim."""
    body = text(RUN)
    start = body.index("# >>> B1")
    end = body.index("# <<< B1")
    return body[start:body.index("\n", end) + 1]


def run_preflight_block(env, cwd=REPO):
    script = (
        "set -uo pipefail\n"
        'log() { printf "LOG %s\\n" "$*" >> "$OUT/run.log"; }\n'
        'mark() { printf "MARK %s %s %s\\n" "$1" "$2" "$3" >> "$OUT/marks.txt"; }\n'
        "ARTEMIS_OK=1\n"
        'mkdir -p "$OUT/artemis"\n'
        + init_block() + "\n"
        + preflight_block() +
        'printf "CHAIN=%s\\n" "$(paste -sd, "$ARTEMIS_CHAIN_FILE" 2>/dev/null)"\n'
        'printf "RUNTIME=%s\\n" "$ARTEMIS_CONFIG_RUNTIME"\n'
        'printf "CHAINFILE=%s\\n" "$ARTEMIS_CHAIN_FILE"\n'
        'printf "EXPORT=%s\\n" "${ARTEMIS_ARTEMIS_JSONC:-}"\n'
        'printf "OK=%s\\n" "$ARTEMIS_OK"\n'
    )
    return bash(script, env=env, cwd=cwd)


def preflight_tree(tmp, base_url, override=None, out="e2e-out"):
    """Throwaway repo root, and OUT stays RELATIVE on purpose (review B1).

    Artemis runs from `vendor/artemis`; a relative runtime path is resolved from
    there and silently misses, so the whole chain goes inert in CI.
    """
    os.makedirs(os.path.join(tmp, "vendor", "artemis", "config"), exist_ok=True)
    write(os.path.join(tmp, "vendor", "artemis", "config", "artemis.jsonc"), CONFIG_JSONC)
    os.symlink(ROOT, os.path.join(tmp, "e2e"))
    env = dict(os.environ)
    for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "ARTEMIS_ARTEMIS_JSONC",
                "ARTEMIS_CONFIG", "ARTEMIS_CONFIG_RUNTIME", "ARTEMIS_CHAIN_FILE"):
        env.pop(key, None)
    env.update({
        "OUT": out,
        "PY": sys.executable,
        "ARTEMIS_CONFIG": "vendor/artemis/config/artemis.jsonc",
        "ARTEMIS_BASE_CHAIN": BASE_STR,
        "FREELLM_BASE_URL": base_url,
        "FREELLM_API_KEY": SECRET,
    })
    if override is not None:
        env["ARTEMIS_DEFAULT_MODEL"] = override
    return env


def stdout_field(out, name):
    for line in out.splitlines():
        if line.startswith(name + "="):
            return line[len(name) + 1:]
    raise AssertionError(f"{name}= absent de la sortie: {out}")


@case
def test_init_makes_every_artemis_path_absolute():
    tmp = tempfile.mkdtemp(prefix="e2e-b1-init-")
    script = ("set -uo pipefail\n"
              "OUT=e2e-out\n"
              "ARTEMIS_CONFIG=vendor/artemis/config/artemis.jsonc\n"
              "ARTEMIS_CONFIG_RUNTIME=rel/runtime.jsonc\n"
              "ARTEMIS_CHAIN_FILE=rel/chain.txt\n"
              + init_block() +
              'printf "%s\\n" "$OUT_ABS" "$ARTEMIS_CONFIG" "$ARTEMIS_CONFIG_RUNTIME" '
              '"$ARTEMIS_CHAIN_FILE"\n')
    proc = bash(script, cwd=tmp)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    lines = proc.stdout.splitlines()
    assert len(lines) == 4, lines
    root = os.path.realpath(tmp)
    for line in lines:
        assert os.path.isabs(line), line
        assert line.startswith(root + os.sep), (line, root)
    assert lines[0] == os.path.join(root, "e2e-out"), lines
    assert lines[2] == os.path.join(root, "rel", "runtime.jsonc"), lines


@case
def test_preflight_block_writes_runtime_config_and_exports_it():
    alive = {"gpt-4o", "gpt-4.1"}
    server, handler = _serve(alive)
    try:
        tmp = tempfile.mkdtemp(prefix="e2e-preflight-block-")
        env = preflight_tree(tmp, f"http://127.0.0.1:{server.server_port}", override="gpt-4o")
        proc = run_preflight_block(env, cwd=tmp)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        out = proc.stdout
        art = os.path.join(tmp, "e2e-out", "artemis")

        requested = [l for l in text(os.path.join(art, "vision-chain-requested.txt")).splitlines() if l]
        assert requested[0] == "gpt-4o", requested
        expected = ["gpt-4o"] + [m for m in BASE_CHAIN if m != "gpt-4o"]
        assert requested == expected, "l'override passe en tête, la base suit dédoublonnée"
        alive_order = [m for m in requested if m in alive]
        # vivacité sur toute la chaîne demandée, puis payload réaliste sur les
        # survivants, chacun DEUX fois (audit R4) — les deux étapes dans l'ordre
        assert [m for m, _ in handler.seen] == requested + [m for m in alive_order
                                                            for _ in (1, 2)], \
            "la sonde doit voir TOUTE la chaîne demandée, puis les survivants (2x)"

        assert "CHAIN=" + ",".join(alive_order) in out, out
        marks = text(os.path.join(tmp, "e2e-out", "marks.txt"))
        assert f"MARK llm-vision PASS chaîne vision ({len(alive_order)})" in marks, marks
        assert "OK=1" in out, out

        runtime = os.path.join(art, "artemis.jsonc")
        cfg = json.loads(text(runtime))
        assert cfg["default"]["model"] == "gpt-4o", cfg["default"]
        assert cfg["default"]["fallback"]["model"] == "gpt-4.1", cfg["default"]["fallback"]
        assert cfg["nodes"]["planner"]["model"] == "gpt-4o", cfg["nodes"]["planner"]
        assert stdout_field(out, "RUNTIME") == runtime, out
        assert stdout_field(out, "CHAINFILE") == os.path.join(art, "vision-chain.txt"), out

        # B1 regression: Artemis resolves ARTEMIS_ARTEMIS_JSONC with Path.exists()
        # from ITS cwd (vendor/artemis). A relative value silently falls back to the
        # bundled config, so the export must be absolute and resolvable from there.
        export = stdout_field(out, "EXPORT")
        assert export == runtime, out
        assert os.path.isabs(export), export
        previous = os.getcwd()
        try:
            os.chdir(os.path.join(tmp, "vendor", "artemis"))
            assert pathlib.Path(export).is_absolute()
            assert pathlib.Path(export).exists(), export
        finally:
            os.chdir(previous)

        assert SECRET not in text(os.path.join(art, "vision-preflight.txt"))
        assert "timeout=" in text(os.path.join(tmp, "e2e-out", "run.log")), \
            text(os.path.join(tmp, "e2e-out", "run.log"))
        assert text(os.path.join(tmp, "vendor", "artemis", "config", "artemis.jsonc")) == CONFIG_JSONC, \
            "vendor/ a été réécrit"
    finally:
        server.shutdown()
        server.server_close()


@case
def test_preflight_block_with_every_route_down_degrades_to_warn():
    server, _handler = _serve(set())
    try:
        tmp = tempfile.mkdtemp(prefix="e2e-preflight-down-")
        env = preflight_tree(tmp, f"http://127.0.0.1:{server.server_port}")
        proc = run_preflight_block(env, cwd=tmp)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        art = os.path.join(tmp, "e2e-out", "artemis")
        marks = text(os.path.join(tmp, "e2e-out", "marks.txt"))
        assert "MARK llm-vision WARN" in marks, marks
        assert "OK=0" in proc.stdout, proc.stdout
        assert stdout_field(proc.stdout, "CHAIN") == "", proc.stdout
        assert stdout_field(proc.stdout, "EXPORT") == "", proc.stdout
        assert os.path.isabs(stdout_field(proc.stdout, "RUNTIME")), proc.stdout
        assert not os.path.exists(os.path.join(art, "artemis.jsonc")), \
            "pas de config runtime sans modèle vivant"
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------- runner
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
    print(f"\n{len(CASES) - failed}/{len(CASES)} model-chain cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
