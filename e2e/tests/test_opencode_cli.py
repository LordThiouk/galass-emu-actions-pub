#!/usr/bin/env python3
"""Le e2e CI est piloté par le CLI opencode (sans clé API, sans FreeLLM)."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, "..", ".github", "workflows", "artemis-staging-e2e.yml")
SCRIPT = os.path.join(ROOT, "ci-opencode-run.sh")
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def wf():
    return open(WORKFLOW, encoding="utf-8").read()


def script():
    return open(SCRIPT, encoding="utf-8").read()


@case
def test_workflow_has_no_llm_api_key_or_freellm():
    body = wf()
    for needle in ("OPENCODE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                   "FREELLMAPI", "freellmapi", "FreeLLM", "Garde clé opencode"):
        assert needle not in body, f"reste interdit dans le workflow: {needle}"


@case
def test_workflow_installs_same_opencode_cli_as_fullrun():
    body = wf()
    assert 'OPENCODE_VERSION: "1.19.4"' in body, body
    assert 'npm install -g --no-fund --no-audit "opencode-ai@${OPENCODE_VERSION}"' in body
    assert "actions/setup-node@v4" in body
    assert 'node-version: "24"' in body
    assert "bash e2e/ci-opencode-run.sh run" in body
    assert 'default: "opencode/deepseek-v4.1-flash-free"' in body
    assert "ARTEMIS_DISABLE: \"1\"" in body


@case
def test_script_drives_adb_and_harness():
    body = script()
    for needle in ("adb", "e2e/run.sh", "e2e/api_flow.py", "e2e/ui.sh",
                   "verdict.txt", "opencode run", "PASS", "FAIL"):
        assert needle in body, f"manque dans ci-opencode-run.sh: {needle}"
    assert "FreeLLM" in body or "pas de FreeLLM" in body or "pas de FreeLLM" in open(
        os.path.join(ROOT, "ci-opencode-run.sh"), encoding="utf-8").read()


@case
def test_validate_model_accepts_opencode_free_and_rejects_others():
    def run(model):
        return subprocess.run(["bash", SCRIPT, "validate-model", model],
                              cwd=os.path.dirname(ROOT),
                              capture_output=True, text=True)
    ok = run("opencode/deepseek-v4.1-flash-free")
    assert ok.returncode == 0, ok.stderr
    assert "model=opencode/deepseek-v4.1-flash-free" in ok.stdout
    bare = run("mimo-v2.6-flash-free")
    assert bare.returncode == 0, bare.stderr
    assert "model=opencode/mimo-v2.6-flash-free" in bare.stdout
    for bad in ("gemini-2.5-flash", "opencode/gemini-2.5-flash", "freellm/foo-free",
                "openai/deepseek-v4.1-flash-free"):
        proc = run(bad)
        assert proc.returncode != 0, bad
        assert "refusé" in (proc.stderr + proc.stdout)


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} opencode-cli cases passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
