#!/usr/bin/env python3
"""Le harnais e2e ne doit pas s'auto-tuer sous `set -euo pipefail`.

CI run 43811595159 (job log ~L1416-1427) : `e2e/ci-opencode-run.sh` line 57
declarait `local main="$1" chain="$main" m norm`. Bash expanse TOUS les mots
d'un `local` AVANT d'affecter : `$main` est lu alors qu'il est encore unset
-> "line 57: main: unbound variable" -> la chaine de modeles n'est jamais
construite, zero scenario joue, verdict=FAIL, ~3s apres l'installation APK.

Ce test rejoue build_chain() extrait VERBATIM du vrai script dans un bash
`set -euo pipefail` (sans reseau, sans emulator, sans credentials) et
detecte statiquement la classe entiere : un `local`/`declare` qui lit une
variable qu'il affecte dans le meme statement.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "ci-opencode-run.sh")
MODEL = "opencode/deepseek-v4.1-flash-free"
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def script():
    return open(SCRIPT, encoding="utf-8").read()


def extract_fn(text, name):
    """Bloc `name() { ... }` — jusqu'au premier `}` en début de ligne."""
    m = re.search(r"^%s\(\) \{\n(?:.*\n)*?^\}$" % re.escape(name), text, re.M)
    assert m, f"fonction {name}() introuvable dans {SCRIPT}"
    return m.group(0)


def extract_defaults(text):
    """Les constantes de haut niveau dont build_chain() a besoin."""
    names = ("OUT", "VERDICT", "PROMPT_FILE", "OC_LOG",
             "DEFAULT_MODEL", "DEFAULT_CHAIN", "APK_RIDER", "APK_DRIVER")
    lines = [l for l in text.splitlines()
             if any(l.startswith(n + "=") for n in names)]
    assert lines, "constantes de haut niveau introuvables"
    return "\n".join(lines)


def build_chain_probe(tmpdir):
    """Écrit un bash minimal contenant le VRAI build_chain() et ses dépendances."""
    text = script()
    fail_one_liner = re.search(r"^fail\(\) \{[^\n]*\}$", text, re.M)
    assert fail_one_liner, "fail() introuvable"
    parts = [extract_defaults(text),
             fail_one_liner.group(0),
             extract_fn(text, "normalize_model"),
             extract_fn(text, "validate_model"),
             extract_fn(text, "build_chain")]
    path = os.path.join(tmpdir, "build_chain_probe.sh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts) + "\n")
    return path


def run_probe(probe, script, *args):
    return subprocess.run(["bash", "-c", script, "_", probe, *args],
                          capture_output=True, text=True, timeout=30)


@case
def test_build_chain_survives_set_u():
    """build_chain() doit tourner sous `set -euo pipefail` sans unbound variable."""
    with tempfile.TemporaryDirectory() as d:
        probe = build_chain_probe(d)
        proc = run_probe(probe,
                         'set -euo pipefail; source "$1"; '
                         'chain=$(build_chain "$2"); printf "chain=%s\\n" "$chain"',
                         MODEL)
    assert "unbound variable" not in proc.stderr, \
        f"build_chain() lit une variable non assignee sous set -u : {proc.stderr.strip()}"
    assert proc.returncode == 0, \
        f"build_chain() a echoue (rc={proc.returncode}): {proc.stderr.strip()}"
    assert proc.stdout.startswith(f"chain={MODEL}"), proc.stdout


@case
def test_build_chain_starts_with_the_requested_model():
    """La chaine commence par le modele demande, puis les extras valides."""
    with tempfile.TemporaryDirectory() as d:
        probe = build_chain_probe(d)
        proc = run_probe(probe,
                         'set -euo pipefail; source "$1"; build_chain "$2"',
                         MODEL)
    assert proc.returncode == 0, proc.stderr
    chain = proc.stdout.strip()
    assert chain.split(",")[0] == MODEL, chain
    assert len(chain.split(",")) > 1, f"chaine trop courte : {chain}"
    for model in chain.split(","):
        assert re.match(r"^opencode/[a-z0-9][a-z0-9.-]*-free$", model), model


@case
def test_build_chain_has_no_self_referencing_local():
    """`local main="$1" chain="$main"` : bash expanse avant d'affecter -> BUG."""
    block = extract_fn(script(), "build_chain")
    stmt = re.search(r"^\s*(?:local|declare)\s+.*chain.*$", block, re.M)
    assert stmt, "la declaration de `chain` est introuvable dans build_chain()"
    line = stmt.group(0)
    assigned = set(re.findall(r"(?:^|\s)(\w+)=", line))
    referenced = set(re.findall(r"\$\{?(\w+)", line))
    assert not (assigned & referenced), \
        (f"`{line.strip()}` lit {assigned & referenced} avant affectation "
         "(bash expanse tous les mots du local d'abord) — declarer en deux lignes")


@case
def test_no_self_referencing_local_in_e2e_shell_scripts():
    """Balayage de la classe dans tout l'arbre e2e/ (pas seulement ce fichier)."""
    e2e_dir = ROOT
    hits = []
    for name in sorted(os.listdir(e2e_dir)):
        if not name.endswith(".sh"):
            continue
        path = os.path.join(e2e_dir, name)
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh.read().splitlines(), 1):
                m = re.match(r"\s*(?:local|declare)\s+(.*)$", line)
                if not m:
                    continue
                stmt = m.group(1)
                assigned = set(re.findall(r"(?:^|\s)(\w+)=", stmt))
                referenced = set(re.findall(r"\$\{?(\w+)", stmt))
                for var in sorted(assigned & referenced):
                    hits.append(f"{name}:{lineno} `{line.strip()}` ({var})")
    assert not hits, "local/declare auto-referencant (bash expanse avant d'affecter) : " \
                     + " | ".join(hits)


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} ci-opencode-run cases passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
