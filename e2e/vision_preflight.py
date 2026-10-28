#!/usr/bin/env python3
"""Probe every model of the requested vision chain, keep only the alive ones.

Artemis must never be pointed at a route that cannot see an image: a 429/"lacks
vision" mid-run kills the whole UI step. The probe is TWO-STAGE, because a 1x1
pin and a real capture are not the same request (run 40602473815: 6/6 routes
"alive" on the pin, then the ~300 KB call got 200 and 502 from the relay):

  stage 1  liveness  — the historical 1x1 PNG, one POST per chain id;
  stage 2  realistic — a ~300 KB PNG of phone dimensions for the survivors of
                       stage 1, in input order. Only these ids are kept.

usage: vision_preflight.py --chain-file F --out F --log F
                           [--timeout 30] [--probe-bytes 307200]

Credentials come from the environment ONLY (OPENAI_* first, then FREELLM_*):
  base = OPENAI_BASE_URL or FREELLM_BASE_URL
  key  = OPENAI_API_KEY or FREELLM_API_KEY
The key is never written: every response body is redacted before it reaches the
log. An empty alive list is NOT an error — the caller WARNs (Artemis unavailable).

Every per-probe line carries `latency_ms=` (ISC-2), because the admission envelope
is the reason run 40669473919 burned every attempt: a route can answer a probe and
still be far slower than the runtime budget. When EVERY ATTEMPTED liveness probe is
a network error (http=0), the relay itself is unreachable: the log ends with
`verdict=relay_unreachable` and the exit code is 3 (ISC-3), so the caller names the
cause instead of printing `served_model=unknown`. Three CONSECUTIVE http=0 probes
are enough to stop probing (the remaining ids are left unprobed) ONLY while no route
has answered 200 yet: a survivor is already proof the relay is up, so a later streak
of timeouts is data about those routes, never a dead-relay verdict — the probe then
runs to the end of the chain. A mixed run (network errors then an HTTP status) keeps
probing and never gets the verdict either.

Exit 0 unless the usage/inputs are broken (2) or the relay never answered (3).
"""
import argparse
import base64
import functools
import json
import os
import random
import struct
import sys
import time
import zlib
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from model_chain import MODEL_RE  # noqa: E402  (single allowlist, B6)

PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
           "hKmMIQAAAABJRU5ErkJggg==")
BODY_EXCERPT = 300
# Head of the key redacted on its own, so a key cut by BODY_EXCERPT cannot ship
# its first characters (Max m7). 8 is identifying, short enough to stay plausible.
KEY_HEAD_CHARS = 8
# ISC-3: three CONSECUTIVE network failures mean the relay is down — but only as
# long as NOT ONE route has answered 200 yet (a survivor proves the relay is up).
# Once a route succeeded, the streak is data about those routes, never a verdict.
RELAY_UNREACHABLE_STREAK = 3

# A real Galass capture is 1080x2400 and lands around 300 KB once encoded: that
# SIZE is what the relay 502s on, so the probe must reach it to be meaningful.
SYNTH_WIDTH = 1080
SYNTH_HEIGHT = 2400
PROBE_BYTES_DEFAULT = 300 * 1024
# Fixed seed: the same probe bytes on every run, so a size change is a real change.
SYNTH_SEED = 0x5EED2400
SYNTH_STRIDE = SYNTH_WIDTH * 3 + 1          # filter byte 0 + RGB per scanline


def _chunk(kind, data):
    """One PNG chunk: length, type, payload, CRC32 over type+payload."""
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _png_with_noise(noise_bytes):
    """A decodable RGB PNG whose IDAT weighs about `noise_bytes` compressed.

    A provider decodes before answering: IHDR+tEXt+IEND is NOT an image (PIL:
    "cannot load this image") and a 400 on it drops a healthy route. So the real
    scanlines are there — filter byte 0 + W*3 RGB bytes per row — and the weight
    comes from deterministic pseudo-random pixels, which do not compress.
    """
    raw = bytearray(SYNTH_HEIGHT * SYNTH_STRIDE)   # filter 0 + black: valid image
    remaining = min(max(0, noise_bytes), SYNTH_HEIGHT * SYNTH_WIDTH * 3)
    if remaining:
        blob = random.Random(SYNTH_SEED).randbytes(remaining)
        pos = 0
        for row in range(SYNTH_HEIGHT):
            if pos >= remaining:
                break
            take = min(SYNTH_WIDTH * 3, remaining - pos)
            start = row * SYNTH_STRIDE + 1
            raw[start:start + take] = blob[pos:pos + take]
            pos += take
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(
                ">IIBBBBB", SYNTH_WIDTH, SYNTH_HEIGHT, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


@functools.lru_cache(maxsize=8)
def synthetic_png(min_bytes=PROBE_BYTES_DEFAULT):
    """A decodable PNG of phone dimensions, at least `min_bytes` long.

    IHDR declares the real capture size and IDAT carries real scanlines; the size
    is driven by incompressible pixel noise, so the request weighs what the real
    ~300 KB capture weighs. Truncating or faking the header would let a route pass
    a request it would fail on the real image, which is the whole bug.
    """
    try:
        target = int(min_bytes)
    except (TypeError, ValueError):
        target = PROBE_BYTES_DEFAULT
    target = max(1, target)
    pixel_bytes = SYNTH_HEIGHT * SYNTH_WIDTH * 3
    noise = min(pixel_bytes, max(0, target - 128))   # 128 ~ PNG framing
    png = _png_with_noise(noise)
    for _ in range(3):
        if len(png) >= target or noise >= pixel_bytes:
            break
        noise = min(pixel_bytes, noise + (target - len(png)) + 1024)
        png = _png_with_noise(noise)
    return png


def payload_bytes(model, image=PNG_1x1):
    return len(json.dumps(payload(model, image)).encode("utf-8"))


def backend():
    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("FREELLM_BASE_URL") or ""
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("FREELLM_API_KEY") or ""
    return base.rstrip("/"), key


def redact(text, key):
    """Redacted, single-line body excerpt.

    CR/LF are folded to spaces: the body comes from the relay, and a raw newline
    would let it fabricate a whole `stage=realistic …` log line (the floor map
    parses those lines positionally — see run.sh vision_floor_map).

    The key's HEAD is redacted too, not just the whole key: a key straddling the
    BODY_EXCERPT boundary is truncated, so the exact-substring replace misses it
    and its first characters would ship in the log (Max m7).
    """
    text = str(text or "")
    if key:
        text = text.replace(key, "***")
        text = text.replace(str(key)[:KEY_HEAD_CHARS], "***")
    text = text.replace("\r", " ").replace("\n", " ")
    return text[:BODY_EXCERPT]


def payload(model, image=PNG_1x1):
    return {
        "model": model,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "What color is this image? Answer with one word."},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + image}},
        ]}],
    }


def probe(base, key, model, timeout=30.0, image=PNG_1x1):
    """Return (http_status, body_excerpt). A dead route is data, never an exception."""
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload(model, image)).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a broken error body must not abort the probe
            body = ""
        return exc.code, body
    except Exception as exc:  # noqa: BLE001 - timeout/DNS/TLS: report, keep going
        return 0, repr(exc)


def read_chain(path):
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Artemis vision preflight")
    parser.add_argument("--chain-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--probe-bytes", type=int, default=PROBE_BYTES_DEFAULT,
                        help="stage-2 payload size in bytes; 0 = liveness only")
    args = parser.parse_args(argv)

    try:
        requested = read_chain(args.chain_file)
    except OSError as exc:
        sys.stderr.write(f"cannot read chain file {args.chain_file}: {exc}\n")
        return 2

    base, key = backend()
    for path in (args.out, args.log):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)

    alive = []
    probe_bytes = max(0, args.probe_bytes)
    with open(args.log, "w", encoding="utf-8") as log:
        log.write(f"chain: {len(requested)} model(s) requested\n")
        # VISION_PROBE_TIMEOUT reaches this flag: a route that hangs is dead,
        # never a reason to burn the whole step budget.
        log.write(f"timeout: {args.timeout:g}s\n")
        if not requested:
            log.write("empty chain — nothing to probe\n")
        elif not base:
            log.write("no base url (OPENAI_BASE_URL / FREELLM_BASE_URL) — cannot probe\n")
        else:
            survivors = []
            liveness_codes = []
            consecutive_zero = 0
            log.write(f"stage=liveness bytes={payload_bytes('probe')}\n")
            for model in requested:
                if not MODEL_RE.match(model):
                    log.write(f"invalid id skipped: {model!r}\n")
                    continue
                started = time.monotonic()
                code, body = probe(base, key, model, args.timeout)
                latency_ms = int((time.monotonic() - started) * 1000)
                liveness_codes.append(code)
                log.write(f"stage=liveness model={model} http={code} "
                          f"latency_ms={latency_ms} body={redact(body, key)}\n")
                log.flush()
                consecutive_zero = consecutive_zero + 1 if code == 0 else 0
                if code == 200:
                    survivors.append(model)
                if consecutive_zero >= RELAY_UNREACHABLE_STREAK and not survivors:
                    # ISC-3 short-circuit: three consecutive network failures are
                    # already proof — but ONLY while nothing has succeeded. Once a
                    # route answered 200 the relay is demonstrably up, so a later
                    # streak is a fact about those routes, not a dead relay: keep
                    # probing and let the survivors through (a false verdict here
                    # emptied the alive file and published "relay unreachable" on a
                    # run whose first route had answered 200).
                    log.write("verdict=relay_unreachable\n")
                    log.flush()
                    with open(args.out, "w", encoding="utf-8"):
                        pass
                    return 3
            if liveness_codes and all(code == 0 for code in liveness_codes):
                # ISC-3: not one id got an HTTP status — the relay is unreachable,
                # not "the models lack vision". Name it and stop: the caller marks
                # llm-vision WARN and skips every Artemis attempt instead of burning
                # the whole step on six certain timeouts.
                log.write("verdict=relay_unreachable\n")
                log.flush()
                with open(args.out, "w", encoding="utf-8"):
                    pass
                return 3
            if survivors and probe_bytes:
                image = base64.b64encode(
                    synthetic_png(probe_bytes)).decode("ascii")
                log.write(f"stage=realistic bytes="
                          f"{payload_bytes('probe', image)} survivors="
                          f"{len(survivors)}\n")
                for model in survivors:
                    # TWO samples per survivor (audit R4): a route that answers
                    # once and 502s on the retry is exactly the relay flake of
                    # run 40602473815 — one sample would call it healthy.
                    codes = []
                    for sample in (1, 2):
                        started = time.monotonic()
                        code, body = probe(base, key, model, args.timeout, image)
                        latency_ms = int((time.monotonic() - started) * 1000)
                        log.write(f"stage=realistic model={model} sample={sample} "
                                  f"http={code} latency_ms={latency_ms} "
                                  f"body={redact(body, key)}\n")
                        log.flush()
                        codes.append(code)
                        if code != 200:
                            break
                    if codes == [200, 200]:
                        alive.append(model)
            else:
                alive = survivors

    with open(args.out, "w", encoding="utf-8") as out:
        if alive:
            out.write("\n".join(alive) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
