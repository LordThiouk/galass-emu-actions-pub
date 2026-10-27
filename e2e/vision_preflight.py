#!/usr/bin/env python3
"""Probe every model of the requested vision chain, keep only the alive ones.

Artemis must never be pointed at a route that cannot see an image: a 429/"lacks
vision" mid-run kills the whole UI step. This preflight sends ONE tiny PNG to
each id of the ordered chain and keeps the ids that answer 200, in input order.

usage: vision_preflight.py --chain-file F --out F --log F [--timeout 30]

Credentials come from the environment ONLY (OPENAI_* first, then FREELLM_*):
  base = OPENAI_BASE_URL or FREELLM_BASE_URL
  key  = OPENAI_API_KEY or FREELLM_API_KEY
The key is never written: every response body is redacted before it reaches the
log. An empty alive list is NOT an error — the caller WARNs (Artemis unavailable).
Exit 0 unless the usage/inputs are broken.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from model_chain import MODEL_RE  # noqa: E402  (single allowlist, B6)

PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
           "hKmMIQAAAABJRU5ErkJggg==")
BODY_EXCERPT = 300


def backend():
    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("FREELLM_BASE_URL") or ""
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("FREELLM_API_KEY") or ""
    return base.rstrip("/"), key


def redact(text, key):
    text = str(text or "")
    if key:
        text = text.replace(key, "***")
    return text[:BODY_EXCERPT]


def payload(model):
    return {
        "model": model,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "What color is this image? Answer with one word."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_1x1}},
        ]}],
    }


def probe(base, key, model, timeout=30.0):
    """Return (http_status, body_excerpt). A dead route is data, never an exception."""
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload(model)).encode("utf-8"),
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
            for model in requested:
                if not MODEL_RE.match(model):
                    log.write(f"invalid id skipped: {model!r}\n")
                    continue
                code, body = probe(base, key, model, args.timeout)
                log.write(f"model={model} http={code} body={redact(body, key)}\n")
                log.flush()
                if code == 200:
                    alive.append(model)

    with open(args.out, "w", encoding="utf-8") as out:
        if alive:
            out.write("\n".join(alive) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
