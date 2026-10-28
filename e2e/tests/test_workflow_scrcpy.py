#!/usr/bin/env python3
"""ISC-5 / F3 TDD spec — the runner must install scrcpy >= 3.0 before the emulator starts.

Run 40602473815, smoke.log line 92:
    Failed to start scrcpy recording: [Errno 2] No such file or directory: 'scrcpy'
`scrcpy` is absent from the GitHub runner image and
`.github/workflows/artemis-staging-e2e.yml` had no install step, so the run
produced no screens-reel artifact and the screen grader never ran.

Second, verified trap: Ubuntu noble's apt ships scrcpy **1.25**, while Artemis
invokes `--video-bit-rate` and `--capture-orientation=@` (v3.0+ flags,
vendor/artemis/artemis/utils/video.py:63-75). An `apt-get install scrcpy` step
therefore logs a healthy `scrcpy --version` and still produces no recording —
a false green. The install must come from the upstream release and must ASSERT
a >= 3.0 floor.

Pins, against the workflow FILE CONTENT only (no runner, no network):
  * the install step provisions scrcpy from the official upstream release into
    /usr/local (apt is allowed for deps, never for the `scrcpy` package unless a
    version floor guards it);
  * the download is pinned to an exact tag, integrity-checked, and fails on a
    missing asset (`curl -f`);
  * that step proves the install with `scrcpy --version` and does NOT swallow
    the failure (`|| true`, `|| echo`, `; true` would hide a broken install);
  * a HARD floor assert parses the printed version and exits 1 below 3.0;
  * the install is positioned BEFORE the emulator-start step
    (`reactivecircus/android-emulator-runner`), otherwise the recording still
    fails;
  * the edit is SURGICAL: exactly one step added, every pre-existing step
    identity still present at its baseline count, job/emulator contracts intact.
"""
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
WORKFLOW = os.path.join(REPO, ".github", "workflows", "artemis-staging-e2e.yml")

# Step identities observed in the workflow BEFORE this change (baseline).
# Duplicates are meaningful (3x checkout@v4, 2x upload-artifact@v4): the
# assertion is a multiset, so removing or duplicating a step fails.
BASELINE_STEPS = [
    "uses: actions/checkout@v4",
    "name: Garde staging",
    "uses: actions/checkout@v4",
    "uses: actions/checkout@v4",
    "name: Libérer disque",
    "uses: actions/setup-java@v4",
    "uses: subosito/flutter-action@v2",
    "name: Licences Android + NDK",
    "name: Keystore debug fixe (JKS, SHA-1 stable pour la clé Maps)",
    "name: Build APK (staging, debug, même job, pas d'upload)",
    "name: Vérifier la signature des APK (apksigner)",
    "name: Activer KVM (règle udev officielle)",
    "uses: astral-sh/setup-uv@v5",
    "name: Installer Artemis (vendor)",
    "name: Tests du harnais (syntaxe + contrats)",
    "name: E2E multistop (Artemis + API fallback)",
    "name: Chiffrer les preuves",
    "uses: actions/upload-artifact@v4",
    "name: Publier le smoke log (masqué)",
    "name: Résumé (PASS/FAIL seulement)",
]
BASELINE_COUNT = len(BASELINE_STEPS)  # 20
EMULATOR_MARK = "reactivecircus/android-emulator-runner"
RELEASE_MARK = "github.com/Genymobile/scrcpy/releases/download"
FLOOR_MAJOR = 3  # --capture-orientation / --video-bit-rate appear in scrcpy v3.0

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def workflow_text():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return fh.read()


def step_blocks(text=None):
    """Split the `steps:` list into blocks, returning (identity, body) pairs.

    A step starts on a 6-space `- ` line; its identity is the first
    `name:`/`uses:` seen on the item line or at 8-space indent inside it.
    """
    lines = (workflow_text() if text is None else text).splitlines()
    starts = [i for i, l in enumerate(lines) if re.match(r"^      - ", l)]
    blocks = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        body = lines[s:e]
        m = re.match(r"^      - (uses|name): (.+)$", body[0])
        ident = None
        if m:
            ident = f"{m.group(1)}: {m.group(2)}"
        else:
            for l in body[1:]:
                mm = re.match(r"^        (uses|name): (.+)$", l)
                if mm:
                    ident = f"{mm.group(1)}: {mm.group(2)}"
                    break
        blocks.append((ident, "\n".join(body)))
    return blocks


def install_blocks():
    """The step provisioning scrcpy: upstream release download or apt package."""
    return [(i, b) for i, (_, b) in enumerate(step_blocks())
            if RELEASE_MARK in b or re.search(r"apt-get install[^\n]*\bscrcpy\b", b)]


def apt_scrcpy_lines(body):
    """apt-get install lines that request the scrcpy PACKAGE (noble ships 1.25)."""
    return [l for l in body.splitlines()
            if "apt-get install" in l and re.search(r"(^|\s)scrcpy(\s|$)", l)]


def floor_assert_lines(body):
    """Lines that hard-fail the step when the installed scrcpy is below the floor."""
    return [l for l in body.splitlines()
            if re.search(r"-(ge|lt)\s+%d\b" % FLOOR_MAJOR, l)
            or re.search(r">=\s*%d(\.0)?" % FLOOR_MAJOR, l)]


def parses_printed_version(body):
    """The floor must read the version scrcpy actually prints, not assume it."""
    return bool(re.search(r"\$\(scrcpy --version[^)]*\)", body)) or bool(
        re.search(r"scrcpy --version\s*\|\s*(awk|sed|grep)", body))


@case
def test_install_step_provisions_scrcpy_from_the_upstream_release():
    found = install_blocks()
    assert found, ("aucun step n'installe scrcpy (smoke.log l.92: scrcpy introuvable, "
                   "et l'apt de noble ne fournit que 1.25 — flags Artemis non supportés)")
    _, body = found[0]
    assert RELEASE_MARK in body, (
        f"l'install doit venir de la release upstream (>= {FLOOR_MAJOR}.0), "
        f"pas du paquet apt 1.25:\n{body}")
    assert re.search(r"/usr/local/bin/scrcpy|install -m 0755 .*scrcpy", body), \
        f"le binaire doit atterrir sur le PATH (/usr/local/bin):\n{body}"
    # ISC-5 « autodeclared homebrew abort »: the runner is Linux, apt is the
    # only declared path — a brew install here would be a silent no-op.
    assert "brew" not in body, f"chemin homebrew interdit sur ce runner:\n{body}"


@case
def test_release_download_is_pinned_and_integrity_verified():
    found = install_blocks()
    assert found, "step d'installation scrcpy absent"
    _, body = found[0]
    assert "/latest/" not in body, "téléchargement non déterministe (/latest/) interdit"
    m = re.search(r"releases/download/(v\d+\.\d+\.\d+)/", body)
    assert m, f"tag de release non épinglé (attendu vX.Y.Z):\n{body}"
    tag = m.group(1)
    assert re.search(r"scrcpy-linux-x86_64-\$\{?SCRCPY_VERSION\}?\.tar\.gz|"
                     r"scrcpy-linux-x86_64-%s\.tar\.gz" % re.escape(tag), body), \
        f"asset linux-x86_64 attendu pour {tag}:\n{body}"
    assert re.search(r"curl\b[^\n]*-[a-zA-Z]*f", body), \
        "curl doit échouer sur un asset manquant (-f), pas écrire une page 404"
    assert re.search(r"sha256sum -c|SHA256SUMS", body), \
        "intégrité du téléchargement non vérifiée (sha256sum -c / SHA256SUMS)"
    assert re.search(r"\b[0-9a-f]{64}\b|SHA256SUMS\.txt", body), \
        "aucune empreinte sha256 (constante épinglée ou fichier de sommes) dans le step"


@case
def test_apt_never_ships_the_scrcpy_package_without_a_version_floor():
    found = install_blocks()
    assert found, "step d'installation scrcpy absent"
    _, body = found[0]
    offending = apt_scrcpy_lines(body)
    if not offending:
        return  # Option A: le paquet vient de la release, l'apt ne sert qu'aux deps
    assert floor_assert_lines(body), (
        "apt fournit scrcpy 1.25 sur noble (< 3.0 : --video-bit-rate et "
        "--capture-orientation=@ refusés par Artemis) sans assert de plancher "
        f"de version — faux vert garanti:\n" + "\n".join(offending))


@case
def test_install_step_enforces_a_hard_version_floor_3_0():
    found = install_blocks()
    assert found, "step d'installation scrcpy absent"
    _, body = found[0]
    assert parses_printed_version(body), (
        f"le plancher doit parser la version imprimée par `scrcpy --version`:\n{body}")
    guards = floor_assert_lines(body)
    assert guards, (
        f"aucun assert dur de version >= {FLOOR_MAJOR}.0 : un scrcpy trop ancien "
        f"passerait le step en silence (faux vert):\n{body}")
    assert re.search(r"\bexit 1\b", body), \
        f"l'assert de plancher doit sortir en 1:\n{body}"
    for line in guards:
        assert "|| true" not in line and "|| :" not in line, \
            f"assert de plancher neutralisé: {line.strip()}"


@case
def test_install_step_logs_scrcpy_version():
    found = install_blocks()
    assert found, "step d'installation scrcpy absent"
    _, body = found[0]
    ver = [l for l in body.splitlines() if re.search(r"(^|\s)scrcpy --version\b", l)]
    assert ver, f"`scrcpy --version` absent du step d'installation:\n{body}"


@case
def test_version_check_fails_loudly_when_install_is_broken():
    found = install_blocks()
    assert found, "step d'installation scrcpy absent"
    _, body = found[0]
    for line in body.splitlines():
        if not re.search(r"(^|\s)scrcpy --version\b", line):
            continue
        # `bash -e` (GitHub default) must be free to abort the step.
        for swallow in ("|| true", "|| echo", "; true", "|| :", "&& true"):
            assert swallow not in line, \
                f"le check `scrcpy --version` avale l'échec ({swallow!r}): {line.strip()}"
        return
    raise AssertionError("aucune ligne `scrcpy --version` à vérifier")


@case
def test_install_step_runs_before_the_emulator_starts():
    blocks = step_blocks()
    install_at = [i for i, (_, b) in enumerate(blocks)
                  if RELEASE_MARK in b or re.search(r"apt-get install[^\n]*\bscrcpy\b", b)]
    emu_at = [i for i, (_, b) in enumerate(blocks) if EMULATOR_MARK in b]
    assert emu_at, "step android-emulator-runner introuvable — le workflow a changé de forme"
    assert install_at, "step d'installation scrcpy absent"
    assert install_at[0] < emu_at[0], (
        f"scrcpy doit être installé AVANT l'émulateur (install #{install_at[0]}, "
        f"émulateur #{emu_at[0]})")


@case
def test_install_step_uses_sudo_like_the_other_privileged_steps():
    blocks = step_blocks()
    sudo_steps = [b for _, b in blocks if re.search(r"^\s+sudo ", b, re.M)]
    assert sudo_steps, "aucun step privilégié — contexte root inattendu, revoir la prémisse"
    found = install_blocks()
    assert found, "step d'installation scrcpy absent"
    _, body = found[0]
    assert re.search(r"sudo (apt-get|tar|ln|install|mkdir)", body), \
        "les autres steps privilégiés utilisent sudo: l'install doit suivre le même contexte"


@case
def test_exactly_one_step_added_and_no_preexisting_step_lost():
    identities = [ident for ident, _ in step_blocks()]
    assert len(identities) == BASELINE_COUNT + 1, (
        f"attendu {BASELINE_COUNT + 1} steps (baseline {BASELINE_COUNT} + install scrcpy), "
        f"trouvé {len(identities)} — l'insertion doit être chirurgicale")
    baseline = collections.Counter(BASELINE_STEPS)
    current = collections.Counter(i for i in identities if i is not None)
    missing = baseline - current
    assert not missing, f"steps préexistants perdus ou renommés: {dict(missing)}"
    extra = current - baseline
    assert len(extra) == 1, f"un seul step doit être ajouté, extras: {dict(extra)}"
    only = next(iter(extra))
    assert "scrcpy" in only, f"le step ajouté n'est pas celui de scrcpy: {only!r}"


@case
def test_preexisting_step_names_all_still_present():
    text = workflow_text()
    for ident in BASELINE_STEPS:
        _, _, value = ident.partition(": ")
        assert value in text, f"ancre de step disparue: {ident}"


@case
def test_emulator_step_contract_is_untouched():
    blocks = step_blocks()
    emu = [b for _, b in blocks if EMULATOR_MARK in b]
    assert len(emu) == 1, f"attendu 1 step émulateur, trouvé {len(emu)}"
    body = emu[0]
    for needle in ("name: E2E multistop (Artemis + API fallback)",
                   "timeout-minutes: 100",
                   "api-level: 34",
                   "arch: x86_64",
                   "script: bash e2e/run.sh"):
        assert needle in body, f"contrat de l'étape émulateur cassé: {needle!r}"


@case
def test_job_level_contract_is_untouched():
    text = workflow_text()
    for needle in ("runs-on: ubuntu-latest",
                   "timeout-minutes: 150",
                   'RUN_DEADLINE_MIN: "90"',
                   "watchdog 90 < step 100 < job 150",
                   "for t in e2e/tests/test_*.py; do echo \"== $t\"; python3 \"$t\"; done"):
        assert needle in text, f"contrat de job/harnais cassé: {needle!r}"


@case
def test_install_step_is_self_contained():
    found = install_blocks()
    assert found, "step d'installation scrcpy absent"
    _, body = found[0]
    assert "${{ steps." not in body, "pas de dépendance circulaire à un output de step"
    assert "if:" not in body, "l'installation ne doit pas être conditionnelle au succès d'un autre step"


@case
def test_workflow_still_parses_as_yaml():
    text = workflow_text()
    assert "\t" not in text, "tabulation dans le YAML (indentation invalide)"
    try:
        import yaml  # noqa: PLC0415 - optionnel, stdlib-first
    except ImportError:
        blocks = step_blocks(text)
        assert len(blocks) == BASELINE_COUNT + 1, "repli sans PyYAML: comptage des steps incohérent"
        return
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "le document YAML ne charge pas en mapping"
    steps = doc["jobs"]["e2e"]["steps"]
    assert isinstance(steps, list), "jobs.e2e.steps n'est pas une liste"
    assert len(steps) == BASELINE_COUNT + 1, f"PyYAML voit {len(steps)} steps"
    names = [s.get("name") for s in steps if isinstance(s, dict)]
    assert any(n and "scrcpy" in n for n in names), \
        f"aucun step nommé scrcpy dans le YAML chargé: {names}"


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
