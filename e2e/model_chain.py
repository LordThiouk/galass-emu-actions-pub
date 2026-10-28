#!/usr/bin/env python3
"""Ordered vision-model chain for Artemis, and the runtime config it implies.

Artemis accepts ONE fallback level per node (`LLMWithFallback.fallback: LLM`), so
the only way to survive a rate-limited route is to (a) spread different models
across nodes and (b) restart the step on the next model. This module owns both:

  * the strict allowlist shared with `set_model.py` (B6: never splice an id into
    a shell string, never let a remote listing become a command);
  * `build_chain`: override first, deduped, validated, ordered;
  * a JSONC loader (comments + trailing commas, strings respected) and an atomic
    deterministic writer;
  * `apply`: the unified {"default","nodes"} config gets an explicit
    model+fallback pair from the chain for EVERY node (explicit offsets for
    planner/hopper/outputter, a stable hash for the rest, probed apart and then
    shifted by `--offset`, so the nodes do not all hammer the same route), plus
    the auxiliary model keys
    `set_model.py` used to rewrite — every other key/value is preserved. The
    vendor file is only ever READ; the output is `$OUT/artemis/artemis.jsonc`;
  * `classify`: the retryable-failure table used by `artemis_run`, judged on the
    log TAIL with non-replayable statuses (400/401/403/404/413/415/422) winning
    over an earlier 429.

CLI (argv only, no shell strings):
  chain   --base "a,b,c" [--override X]                 -> ids, one per line (rc=2 invalid)
  apply   --in F --out F --chain-file F [--offset N]
          [--request-timeout S] [--sdk-retries N]       -> writes nothing on invalid input
  classify --rc N [--log F]                             -> signal on stdout, rc=0 if retryable
  attempt-timeout --remaining S --left N [--cap S]      -> seconds this attempt may use
  attempts-affordable --remaining S --left N [--min S]  -> attempts that can still use >= min s
  request-timeout --share S [--sdk-retries N] [--margin S] [--latency-floor S]
                                                       -> per-request seconds for THIS route
                                                          (0 = share funds no bounded call);
                                                          rc=4 = THIS route refused by the
                                                          floor — rotate, nothing run here
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile

# B6: the ONE allowlist. set_model.py imports this, it must never be duplicated.
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")

# Verified against FreeLLMAPI on 2026-10-27 (each answers 200 with an image).
# llama-3.2-11b-vision is DELIBERATELY absent: 429 "lacks vision".
# 2026-10-28 19:50 UTC: gpt-4.1/gpt-4o/gemini-2.5-flash 429 on every key.
# `auto` = FreeLLMAPI vision-aware router (skips routes that lack vision):
# 4/4 x 200 with a png (2.3-13 s, served by gemini-3-flash-preview /
# glm-4.6v-flash). gemma-4-31b-it: 4/4 x 200 (1.9-14 s). The dashboard
# profile API (/api/profiles) needs an admin session we do not hold.
BASE_CHAIN = [
    "auto",
    "gemma-4-31b-it",
    "gpt-4.1",
    "gemini-2.5-flash",
    "gpt-4o",
]
# 2026-10-28 probes (FreeLLMAPI :3001): every flash-lite route is dead or
# exhausted (2.5/2.0-flash-lite 404; 3.5-flash-lite 502; 3.1/flash-lite 429).
DEAD_MODELS = frozenset({
    "llama-3.2-11b-vision",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite",
})

# Auxiliary model keys the old set_model.py used to rewrite with a single id.
AUX_MODEL_PATHS = (
    ("agent", "flash", "step_summarizer", "model"),
    ("agent", "memory", "chunking", "model"),
)

# Explicit rotation offsets for the nodes that decide what the agent sees and
# says. A pure index rotation put planner (0) and hopper (6) on the SAME pair on
# a 6-model chain, so one rate-limited route took both nodes down. These three
# are pinned apart; every other node gets a stable per-name offset, probed to the
# next free slot, and the whole table is still shifted by `--offset`.
NODE_OFFSETS = {"planner": 0, "hopper": 2, "outputter": 4}


# --------------------------------------------------------------------- chain
def _validate(model):
    if not isinstance(model, str) or not MODEL_RE.match(model):
        raise ValueError(f"invalid model id: {model!r}")
    return model


def _split(base):
    if base is None:
        return []
    if isinstance(base, str):
        return [part.strip() for part in base.split(",") if part.strip()]
    return [str(part).strip() for part in base if str(part).strip()]


def build_chain(base, override=None):
    """Ordered, deduped, validated chain; `override` is the PRIMARY head.

    Empty / "auto" (any case) / whitespace-only override is not an override.
    Raises ValueError on an id that fails MODEL_RE.
    """
    head = ""
    if isinstance(override, str) and override.strip().lower() not in ("", "auto"):
        head = override.strip()
    ordered = ([head] if head else []) + _split(base)
    chain, seen = [], set()
    for model in ordered:
        _validate(model)
        if model in DEAD_MODELS:
            continue
        if model not in seen:
            seen.add(model)
            chain.append(model)
    if not chain:
        raise ValueError("empty model chain")
    return chain


# --------------------------------------------------------------------- JSONC
def strip_jsonc(text):
    """Remove // and /* */ comments, respecting string literals."""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def strip_trailing_commas(text):
    """Drop `,` followed only by whitespace and `}`/`]`, respecting strings."""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def loads_jsonc(text):
    return json.loads(strip_trailing_commas(strip_jsonc(text)))


def load_jsonc(path):
    with open(path, encoding="utf-8") as fh:
        return loads_jsonc(fh.read())


def write_json_atomic(config, path):
    """Deterministic JSON, atomic replace, never a partial file."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    data = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".artemis-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------- apply
def _pair(chain, base, index):
    n = len(chain)
    return chain[(base + index) % n], chain[(base + index + 1) % n]


def _hash_offset(name, size):
    """Stable offset for a node we have no explicit opinion about."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % size


def node_offsets(names, size):
    """One distinct rotation offset per node, in config order.

    Explicit for NODE_OFFSETS, stable-hashed otherwise, probed to the next free
    slot so two nodes never share a route while `size` has room for them.
    """
    taken, offsets = set(), []
    for name in names:
        offset = NODE_OFFSETS.get(name)
        if offset is None:
            offset = _hash_offset(name, size)
        offset %= size
        for _ in range(size):
            if offset not in taken:
                break
            offset = (offset + 1) % size
        taken.add(offset)
        offsets.append(offset)
    return offsets


def _rewrite_model_block(block, chain, base, index):
    model, fallback = _pair(chain, base, index)
    block["provider"] = "openai"
    block["model"] = model
    fb = block.get("fallback")
    fb = dict(fb) if isinstance(fb, dict) else {}
    fb["provider"] = "openai"
    fb["model"] = fallback
    block["fallback"] = fb
    return block


def apply_config(config, chain, offset=0, request_timeout_sec=None):
    """Return a new config with every model-bearing block on the ordered chain."""
    if not chain:
        raise ValueError("empty model chain")
    for model in chain:
        _validate(model)
    if request_timeout_sec is not None:  # validated before any work is done
        timeout_value(request_timeout_sec)
    if not isinstance(config.get("nodes"), dict) or not config["nodes"]:
        raise ValueError("config has no unified 'nodes' object")
    base = int(offset) % len(chain)

    out = json.loads(json.dumps(config))  # deep copy, insertion order kept
    nodes = out["nodes"]

    default = out.get("default")
    default = dict(default) if isinstance(default, dict) else {}
    out["default"] = _rewrite_model_block(default, chain, base, 0)

    presets = out.get("presets")
    if isinstance(presets, dict):
        for index, name in enumerate(list(presets)):
            block = presets[name]
            if isinstance(block, dict):
                presets[name] = _rewrite_model_block(block, chain, base, index)

    node_names = list(nodes)
    for name, node_offset in zip(node_names, node_offsets(node_names, len(chain))):
        block = nodes[name]
        block = dict(block) if isinstance(block, dict) else {}
        nodes[name] = _rewrite_model_block(block, chain, base + node_offset, 0)

    for aux_index, path in enumerate(AUX_MODEL_PATHS, start=len(nodes) + 1):
        cursor, found = out, True
        for key in path[:-1]:
            nxt = cursor.get(key)
            if not isinstance(nxt, dict):
                found = False
                break
            cursor = nxt
        if not found or not isinstance(cursor.get(path[-1]), str):
            continue
        cursor[path[-1]] = chain[(base + aux_index) % len(chain)]

    if request_timeout_sec is not None:
        out = set_request_timeout(out, request_timeout_sec)
    return out


def apply(config_in, config_out, chain, offset=0, request_timeout_sec=None):
    """Validate EVERYTHING first, then write once. No output on invalid input."""
    if not chain:
        raise ValueError("empty model chain")
    for model in chain:
        _validate(model)
    if request_timeout_sec is not None:
        timeout_value(request_timeout_sec)
    config = load_jsonc(config_in)
    out = apply_config(config, chain, offset, request_timeout_sec)
    write_json_atomic(out, config_out)
    return out


# ---------------------------------------------------------------- classify
_RETRY_SIGNALS = (
    ("429", re.compile(r"\b429\b")),
    ("routing_exhausted", re.compile(r"routing_exhausted", re.I)),
    ("resource_exhausted", re.compile(r"resource_exhausted", re.I)),
    ("rate_limit", re.compile(r"rate.?limit", re.I)),
    ("quota", re.compile(r"\bquota\b", re.I)),
    ("overloaded", re.compile(r"overloaded", re.I)),
    ("unavailable", re.compile(r"\bunavailable\b", re.I)),
    ("connection_reset", re.compile(r"connection reset", re.I)),
    ("timed_out", re.compile(r"timed out", re.I)),
)
# The tail is already the error zone: any bare 5xx is worth the next route,
# whatever wording the provider used ("status_code": 500, server returned 502).
_HTTP_5XX = re.compile(r"\b5\d\d\b")
# Statuses a replay can never fix: a different route fails them the same way.
_TERMINAL_STATUS = re.compile(r"\b(?:400|401|403|404|413|415|422)\b")
# Only the END of the log says how the attempt died; the top of a 2000-line log
# can hold an old 429 that a retry already absorbed.
CLASSIFY_TAIL_LINES = 40


def classify(rc, log_text=""):
    """Return the retry signal name, or None when the failure is not retryable.

    Judged on the log TAIL: the terminal error decides, and a non-replayable
    status (400/401/403/404/413/415/422) outranks any earlier 429 — retrying it
    would only burn the next route on a request that cannot succeed.
    """
    try:
        rc = int(rc)
    except (TypeError, ValueError):
        return None
    if rc == 0:
        return None
    if rc == 124:
        return "timeout"
    lines = (log_text or "").splitlines()
    tail = "\n".join(lines[-CLASSIFY_TAIL_LINES:])
    if _TERMINAL_STATUS.search(tail):
        return None
    for name, pattern in _RETRY_SIGNALS:
        if pattern.search(tail):
            return name
    if _HTTP_5XX.search(tail):
        return "5xx"
    return None


# ------------------------------------------------------------------ budget
# Run 40205373538: `artemis_run` gave the FIRST attempt the whole step budget, so
# a hung route ate the offer window and the tap/API fallback started after the
# offer had expired. Each attempt now gets a fair share of what is left, capped
# so one slow route cannot hold the step for minutes.
MODEL_TIMEOUT_SEC = 45
MODEL_TIMEOUT_MIN = 5
MODEL_TIMEOUT_MAX = 600

# ------------------------------------------- per-request budget (relay 502s)
# Run 40602473815: a ~300 KB capture got 200 then 502 from the FreeLLMAPI relay,
# and the OpenAI SDK's own "Retrying request" loop burned the whole attempt
# window on its default 60 s timeout — `ModelEndpoint` has no `max_retries` to
# bound it (router.py:87-113). The one honoured knob is the per-request
# `timeout` (llm.py:91-96 -> router.py:300), so it must be small enough that
# (1 + SDK retries) calls plus a margin still fit the attempt share.
RELAY_SDK_RETRIES = 2
REQUEST_TIMEOUT_MARGIN_SEC = 5
REQUEST_TIMEOUT_MARGIN_RATIO = 0.2   # a flat 5 s margin ate a 6 s share (audit R2)
REQUEST_TIMEOUT_FLOOR_SEC = 1


def _nonneg_int(value, default):
    try:
        got = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return got if got >= 0 else default


def _nonneg_float(value, default):
    try:
        got = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if got != got or got < 0:  # NaN
        return default
    return got


def request_timeout_detail(share, sdk_retries=RELAY_SDK_RETRIES,
                           margin=REQUEST_TIMEOUT_MARGIN_SEC, latency_floor=0):
    """(bound, reason) — reason is "ok", "unfunded" or "floor".

    T=0 has TWO causes and they are NOT the same thing (audit R5: run.sh stamped
    a legacy share case as `latency_floor`):

      * "unfunded" — the HISTORICAL case: the share funds no bounded call, so the
        caller writes NO vendor timeout and the outer timeout governs (a 1 s value
        would kill the request before it could log a line). Exit 0, T=0.
      * "floor" — a REFUSAL: the route was measured slower than what this share
        can fund, so the attempt must not start at all. The CLI exits 4 on it and
        run.sh ledgers `latency_floor`.

    `latency_floor` is the latency MEASURED on THIS route by the vision preflight.
    A bound EQUAL to the floor is refused too: a request needing exactly `bound`
    seconds cannot complete inside `bound` seconds. The bound itself is never
    raised, so the share invariant below is untouched.

    Invariant: for every share, either (1 + retries) * T + margin <= share, or T == 0.
    """
    share_sec = _nonneg_float(share, float(MODEL_TIMEOUT_SEC))
    if share_sec <= 0:
        share_sec = float(MODEL_TIMEOUT_SEC)
    retries = _nonneg_int(sdk_retries, RELAY_SDK_RETRIES)
    margin_sec = min(_nonneg_float(margin, REQUEST_TIMEOUT_MARGIN_SEC),
                     share_sec * REQUEST_TIMEOUT_MARGIN_RATIO)
    calls = 1 + retries
    bound = int((share_sec - margin_sec) // calls)
    if bound < REQUEST_TIMEOUT_FLOOR_SEC:
        sys.stderr.write(
            "request-timeout: %.0fs share funds no bounded call (%d call(s) x >= %ds "
            "+ %.1fs margin) — no vendor timeout, the outer timeout governs\n"
            % (share_sec, calls, REQUEST_TIMEOUT_FLOOR_SEC, margin_sec))
        return 0, "unfunded"
    floor_sec = _nonneg_float(latency_floor, 0)
    if floor_sec > 0 and bound <= floor_sec:
        sys.stderr.write(
            "request-timeout: latency floor %.1fs >= bound=%ds (a %.0fs share funds "
            "%d call(s) + %.1fs margin) — route too slow, no Artemis attempt\n"
            % (floor_sec, bound, share_sec, calls, margin_sec))
        return 0, "floor"
    return bound, "ok"


def request_timeout(share, sdk_retries=RELAY_SDK_RETRIES, margin=REQUEST_TIMEOUT_MARGIN_SEC,
                    latency_floor=0):
    """Per-request `timeout` keeping (1 + retries) calls + margin inside `share`.

    Returns 0 for BOTH T=0 causes (see `request_timeout_detail`); the CLI is the
    only place that needs to tell them apart, and it does so through the exit code.
    """
    return request_timeout_detail(share, sdk_retries, margin, latency_floor)[0]


def timeout_value(seconds):
    """Validated per-request timeout: int when whole, float otherwise."""
    try:
        value = float(str(seconds).strip())
    except (TypeError, ValueError):
        raise ValueError(f"request timeout {seconds!r} is not a number")
    if value != value or value <= 0:
        raise ValueError(f"request timeout {seconds!r} must be > 0")
    if value > MODEL_TIMEOUT_MAX:
        raise ValueError(f"request timeout {seconds!r} exceeds {MODEL_TIMEOUT_MAX}s")
    return int(value) if value.is_integer() else value


def set_request_timeout(config, seconds):
    """Put the vendor `timeout` on every model-bearing block, never a fallback.

    `fallback` sub-dicts are honoured by the base LLM class, which ignores
    `timeout` (llm.py:54-66) — the key there is inert, and a stale one is removed.
    """
    if not isinstance(config.get("nodes"), dict) or not config["nodes"]:
        raise ValueError("config has no unified 'nodes' object")
    value = timeout_value(seconds)
    out = json.loads(json.dumps(config))
    blocks = []
    if isinstance(out.get("default"), dict):
        blocks.append(out["default"])
    presets = out.get("presets")
    if isinstance(presets, dict):
        blocks += [b for b in presets.values() if isinstance(b, dict)]
    blocks += [b for b in out["nodes"].values() if isinstance(b, dict)]
    for block in blocks:
        block["timeout"] = value
        fallback = block.get("fallback")
        if isinstance(fallback, dict):
            fallback.pop("timeout", None)
    return out



def clamp_cap(cap=MODEL_TIMEOUT_SEC):
    """Numeric attempt cap, clamped to [5, 600]; anything unparsable -> 45."""
    try:
        value = int(str(cap).strip())
    except (TypeError, ValueError):
        return MODEL_TIMEOUT_SEC
    return max(MODEL_TIMEOUT_MIN, min(MODEL_TIMEOUT_MAX, value))


def attempt_timeout(remaining, attempts_left, cap=MODEL_TIMEOUT_SEC):
    """Seconds one attempt may use: min(cap, remaining // attempts_left).

    The last attempt gets whatever is left (still capped), because there is no
    next route to protect. Zero means "do not start an attempt": either the step
    budget is gone or no attempt is left. While time remains and an attempt is
    left, the share is never below 1 s (a 0 s `timeout` call would kill Artemis
    before it could log anything).
    """
    try:
        remaining = int(remaining)
        attempts_left = int(attempts_left)
    except (TypeError, ValueError):
        return 0
    cap = max(1, clamp_cap(cap))
    if remaining < 1 or attempts_left < 1:
        return 0
    if attempts_left == 1:
        share = min(cap, remaining)
    else:
        share = min(cap, remaining // attempts_left)
    return max(1, share)


def capped_attempts_left(remaining, attempts_left, min_attempt=MODEL_TIMEOUT_MIN):
    """How many routes can still get a usable share of `remaining` seconds.

    Contract A: dividing the step by every remaining route put a 22 s offer
    window at 3 s per model (6 routes), under the 5 s usability floor, so
    `artemis_run` refused to start ANY attempt on `driver-accept` and Artemis
    never got a chance inside the offer. The count is what must shrink, never the
    floor: `min(attempts_left, max(1, remaining // min_attempt))`, and 0 when the
    window cannot fund a single usable attempt.
    """
    try:
        remaining = int(remaining)
        attempts_left = int(attempts_left)
        min_attempt = max(1, int(min_attempt))
    except (TypeError, ValueError):
        return 0
    if remaining < min_attempt or attempts_left < 1:
        return 0
    return min(attempts_left, max(1, remaining // min_attempt))


# --------------------------------------------------------------------- CLI
def _read_chain_file(path):
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def _cmd_chain(args):
    chain = build_chain(args.base, args.override)
    sys.stdout.write("\n".join(chain) + "\n")
    return 0


def _cmd_apply(args):
    chain = build_chain(_read_chain_file(args.chain_file))
    if args.sdk_retries is not None and _nonneg_int(args.sdk_retries, -1) < 0:
        raise ValueError(f"sdk retries {args.sdk_retries!r} must be >= 0")
    apply(args.config_in, args.config_out, chain, args.offset, args.request_timeout)
    sys.stdout.write(f"{len(chain)} models -> {args.config_out}\n")
    return 0


def _cmd_classify(args):
    log_text = ""
    if args.log:
        try:
            with open(args.log, encoding="utf-8", errors="replace") as fh:
                log_text = fh.read()
        except OSError:
            log_text = ""
    signal = classify(args.rc, log_text)
    if signal is None:
        return 1
    sys.stdout.write(signal + "\n")
    return 0


def _cmd_attempt_timeout(args):
    sys.stdout.write(f"{attempt_timeout(args.remaining, args.left, args.cap)}\n")
    return 0


def _cmd_request_timeout(args):
    bound, reason = request_timeout_detail(
        args.share, args.sdk_retries, args.margin, args.latency_floor)
    sys.stdout.write(f"{bound}\n")
    # ISC-4: exit 4 is the FLOOR REFUSAL and nothing else. T=0 for an unfunded
    # share stays exit 0 (the historical legacy path run.sh must keep).
    return 4 if reason == "floor" else 0


def _cmd_attempts_affordable(args):
    sys.stdout.write(f"{capped_attempts_left(args.remaining, args.left, args.min)}\n")
    return 0


def _parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    chain = sub.add_parser("chain", help="print the ordered chain")
    chain.add_argument("--base", default=",".join(BASE_CHAIN))
    chain.add_argument("--override", default=None)
    chain.set_defaults(func=_cmd_chain)

    apply_p = sub.add_parser("apply", help="write the runtime Artemis config")
    apply_p.add_argument("--in", dest="config_in", required=True)
    apply_p.add_argument("--out", dest="config_out", required=True)
    apply_p.add_argument("--chain-file", required=True)
    apply_p.add_argument("--offset", type=int, default=0)
    apply_p.add_argument("--request-timeout", default=None,
                         help="vendor per-request timeout written on every node")
    apply_p.add_argument("--sdk-retries", default=None,
                         help="SDK-level retries the bound was computed for")
    apply_p.set_defaults(func=_cmd_apply)

    classify_p = sub.add_parser("classify", help="retry signal for a failed attempt")
    classify_p.add_argument("--rc", type=int, required=True)
    classify_p.add_argument("--log", default=None)
    classify_p.set_defaults(func=_cmd_classify)
    timeout_p = sub.add_parser("attempt-timeout",
                               help="seconds this Artemis attempt may use")
    timeout_p.add_argument("--remaining", required=True)
    timeout_p.add_argument("--left", required=True)
    timeout_p.add_argument("--cap", default=MODEL_TIMEOUT_SEC)
    timeout_p.set_defaults(func=_cmd_attempt_timeout)

    relay_p = sub.add_parser(
        "request-timeout",
        help="per-request timeout that fits (1+sdk retries) inside an attempt share")
    relay_p.add_argument("--share", required=True)
    relay_p.add_argument("--sdk-retries", default=RELAY_SDK_RETRIES)
    relay_p.add_argument("--margin", default=REQUEST_TIMEOUT_MARGIN_SEC)
    relay_p.add_argument("--latency-floor", default=0,
                         help="measured route latency (s): refuse a bound below it")
    relay_p.set_defaults(func=_cmd_request_timeout)

    affordable_p = sub.add_parser(
        "attempts-affordable",
        help="how many routes can still get a usable share of the step budget")
    affordable_p.add_argument("--remaining", required=True)
    affordable_p.add_argument("--left", required=True)
    affordable_p.add_argument("--min", default=MODEL_TIMEOUT_MIN)
    affordable_p.set_defaults(func=_cmd_attempts_affordable)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        sys.stderr.write(f"invalid: {exc}\n")
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"invalid input: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
