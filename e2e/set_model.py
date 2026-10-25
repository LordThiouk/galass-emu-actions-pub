#!/usr/bin/env python3
"""Rewrite the Artemis model id without ever concatenating it into a shell string.

usage: set_model.py <config.jsonc> <model-id>

The model id can come from a remote /models listing or a workflow input, so it is
untrusted. B6: validate it against a strict allowlist and pass it as argv; the old
`sed -i ... "$VISION_MODEL"` let a crafted id rewrite the sed program itself.
"""
import json
import re
import sys

MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
MODEL_KEY_RE = re.compile(r'("model"\s*:\s*)"[^"]*"')


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(f"usage: {argv[0]} <config> <model>\n")
        return 2
    path, model = argv[1], argv[2]
    if not MODEL_RE.match(model or ""):
        sys.stderr.write(f"invalid model id (allowed: [A-Za-z0-9._:/-]{{1,80}}): {model!r}\n")
        return 2
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
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
