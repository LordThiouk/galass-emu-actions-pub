#!/usr/bin/env python3
"""Rewrite the Artemis model id without ever concatenating it into a shell string.

usage: set_model.py <config.jsonc> <model-id>
       set_model.py --check <config.jsonc> <model-id>

The model id can come from a remote /models listing or a workflow input, so it is
untrusted. B6: validate it against a strict allowlist and pass it as argv; the old
`sed -i ... "$VISION_MODEL"` let a crafted id rewrite the sed program itself.

The allowlist regex lives in ONE place — `e2e/model_chain.py` — so the two
writers can never drift. `--check` reads the config, prints how many `"model"`
values match, and writes nothing (rc=1 when the id is absent).
"""
import json
import re
import sys

from model_chain import MODEL_RE

MODEL_KEY_RE = re.compile(r'("model"\s*:\s*)"[^"]*"')
MODEL_VALUE_RE = re.compile(r'"model"\s*:\s*"([^"]*)"')


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _count(text, model):
    return sum(1 for value in MODEL_VALUE_RE.findall(text) if value == model)


def _check(argv):
    if len(argv) != 4:
        sys.stderr.write(f"usage: {argv[0]} --check <config> <model>\n")
        return 2
    path, model = argv[2], argv[3]
    if not MODEL_RE.match(model or ""):
        sys.stderr.write(f"invalid model id (allowed: [A-Za-z0-9._:/-]{{1,80}}): {model!r}\n")
        return 2
    try:
        count = _count(_read(path), model)
    except OSError as exc:
        sys.stderr.write(f"cannot read {path}: {exc}\n")
        return 2
    print(count)
    return 0 if count else 1


def main(argv):
    if len(argv) > 1 and argv[1] == "--check":
        return _check(argv)
    if len(argv) != 3:
        sys.stderr.write(f"usage: {argv[0]} <config> <model>\n")
        return 2
    path, model = argv[1], argv[2]
    if not MODEL_RE.match(model or ""):
        sys.stderr.write(f"invalid model id (allowed: [A-Za-z0-9._:/-]{{1,80}}): {model!r}\n")
        return 2
    try:
        text = _read(path)
    except OSError as exc:
        sys.stderr.write(f"cannot read {path}: {exc}\n")
        return 2
    new, count = MODEL_KEY_RE.subn(lambda m: m.group(1) + json.dumps(model), text)
    if count == 0:
        sys.stderr.write(f"no \"model\" key found in {path}\n")
        return 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
