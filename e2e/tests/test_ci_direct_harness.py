#!/usr/bin/env python3
"""Run 43956819722 : le harnais deterministe doit passer AVANT l'agent.

L'agent opencode n'est qu'un wrapper autour de `bash e2e/run.sh` et
`run_opencode` bouclait `command timeout 90m` par modele de la chaine (90 min x N)
face a un step CI de 100 min. Mesure du run : modele 1 stalle jusqu'a rc=124, modele
2 demarre, job tue — le secours place APRES la boucle n'etait jamais atteint.
Correctif : `cmd_run` execute d'abord `bash e2e/run.sh` (autosuffisant, budget
interne `deadline_check` + RUN_DEADLINE_MIN), puis n'appelle `run_opencode`
que si le harnais n'a rendu ni verdict ni resultats (jambe de reparation).

Aucun affaiblissement : derive_verdict reste la seule source du verdict, lisant
report.json/verdict.txt produits par report.py.

Tout est simule (faux opencode, faux adb, faux e2e/run.sh) : aucun emulateur,
aucun reseau, aucun credential.

argv[1] optionnel rejoue la meme spec contre une version anterieure du script
(demonstration rouge) : python3 test_ci_direct_harness.py /tmp/avant.sh
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SCRIPT = os.path.join(ROOT, "ci-opencode-run.sh")
SCRIPT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SCRIPT
DIRECT_LOG = "harnais déterministe direct"
REPAIR_LOG = "jambe de réparation opencode"
SKIP_AGENT_LOG = "agent opencode non appelé"
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# --- faux binaires / faux harnais -------------------------------------------

# Compteur d'invocations de l'agent : ecrit a cote du faux binaire.
OC_COUNT = (
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/oc-calls.txt"\n'
)

# Modele gratuit qui ne rend jamais de verdict (timeout 90m -> rc=124).
OC_TIMEOUT = (
    "#!/usr/bin/env bash\n"
    + OC_COUNT
    + "exit 124\n"
)

# Modele qui STALLE (comme le run 43956819722) ET qui IGNORE SIGTERM : il ne
# rend jamais la main et ne sort que KILLe par le plafond DUR de la jambe de
# reparation. C'est lui qui doit etre coupe par le plafond global (R2) :
# sans SIGKILL, GNU timeout attend le delai indefiniment quand l'enfant
# ignore SIGTERM (mesure : budget 6 s -> 90 s de blocage, script jamais
# rendu, derive_verdict jamais atteint).
OC_STALL = (
    "#!/usr/bin/env bash\n"
    + OC_COUNT
    + "trap '' TERM\n"
    + "while :; do sleep 1; done\n"
)

# Modele qui a deja tranche : il ecrit verdict.txt lui-meme.
OC_VERDICT_PASS = (
    "#!/usr/bin/env bash\n"
    + OC_COUNT
    + 'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    "{\n"
    "  echo PASS\n"
    "  echo 'fake opencode: verdict deja rendu'\n"
    '} > "$OUT/verdict.txt"\n'
    "exit 0\n"
)

# adb minimal : boot deja termine, installs OK.
# `shell cat /proc/uptime` renvoie l'uptime de l'emulateur (mesure du temps
# deja ecoule AVANT le script) — 0.00 par defaut (emulateur demarre a l'instant).
ADB_FAKE = (
    "#!/usr/bin/env bash\n"
    'case "${1:-}" in\n'
    "  shell)\n"
    '    case "${2:-}" in\n'
    '      cat) printf \'%s\\n\' "${EMU_UPTIME_S:-0.00}" ;;\n'
    "      *) printf '1\\n' ;;\n"
    "    esac\n"
    "    ;;\n"
    "  *) exit 0 ;;\n"
    "esac\n"
)

# e2e/run.sh deterministe simule : ecrit results.txt + report.json (PASS),
# laisse une trace d'invocation dans e2e/calls.txt et enregistre le
# RUN_DEADLINE_MIN recu (proof that the effective budget is really passed
# to the harness, and not only logged).
RUN_FAKE = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    # comme le vrai e2e/run.sh : le journal est REINITIALISE au demarrage
    ': > "$OUT/run.log"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'printf "%s\\n" "${RUN_DEADLINE_MIN:-unset}" >> "$(dirname "${BASH_SOURCE[0]}")/deadline.txt"\n'
    'cat > "$OUT/results.txt" <<\'EOF\'\n'
    "POINT|P0|PASS\n"
    "EOF\n"
    'cat > "$OUT/report.json" <<\'EOF\'\n'
    '{"verdict":"PASS","p0_pass":30,"p0_total":30,"fail_count":0,"p0_nonpass":[]}\n'
    "EOF\n"
    "exit 0\n"
)

# e2e/run.sh qui atteint SA deadline interne (rc=124) : le watchdog interne a
# flush le rapport partiel, puis le script sort en 124. Le verdict doit etre
# DEDUIT de report.json, et l'agent opencode ne doit PAS etre appele.
RUN_DEADLINE_124 = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'printf "%s\\n" "${RUN_DEADLINE_MIN:-unset}" >> "$(dirname "${BASH_SOURCE[0]}")/deadline.txt"\n'
    'printf "rider-stop1-done|P0|PASS\\n" > "$OUT/results.txt"\n'
    "printf '%s\\n' '{\"verdict\":\"FAIL\",\"p0_pass\":12,\"p0_total\":30,"
    "\"fail_count\":3,\"p0_nonpass\":[\"rider-stop2-done\"]}' > \"$OUT/report.json\"\n"
    "exit 124\n"
)

# e2e/run.sh muet : ne produit ni verdict ni resultats -> declenche la jambe
# de reparation opencode. Trace l'appel pour prouver qu'il a bien tourne.
RUN_SILENT = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    "exit 0\n"
)

# e2e/run.sh qui rend un P0 en echec : le verdict doit rester FAIL.
RUN_FAIL = (
    "#!/usr/bin/env bash\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'printf "POINT|P0|FAIL\\n" > "$OUT/results.txt"\n'
    "printf '%s\\n' '{\"verdict\":\"FAIL\",\"p0_pass\":29,\"p0_total\":30,"
    "\"fail_count\":1,\"p0_nonpass\":[\"P0-7\"]}' > \"$OUT/report.json\"\n"
    "exit 1\n"
)


# report.py en CI : ARTEMIS_DISABLE=1 force les points NON-P0 (llm-vision,
# artemis-smoke) en SKIP -> report.py rend PASS_WITH_WARNINGS alors que les
# 30 P0 sont PASS. derive_verdict doit dire PASS : la substance est complete.
RUN_WARNINGS = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'cat > "$OUT/results.txt" <<\'EOF\'\n'
    "POINT|P0|PASS\n"
    "EOF\n"
    'cat > "$OUT/report.json" <<\'EOF\'\n'
    '{"verdict":"PASS_WITH_WARNINGS","fail_count":0,"warn_count":0,"skip_count":2,'
    '"p0_pass":30,"p0_total":30,"p0_nonpass":[]}\n'
    "EOF\n"
    "exit 0\n"
)

# p0_nonpass non vide, compteurs P0 coherents : la clause "p0_nonpass vide"
# est INDEPENDANTE de p0_pass == p0_total, elle seule doit rendre FAIL.
RUN_P0_NONPASS = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'cat > "$OUT/results.txt" <<\'EOF\'\n'
    "POINT|P0|SKIP\n"
    "EOF\n"
    'cat > "$OUT/report.json" <<\'EOF\'\n'
    '{"verdict":"FAIL","fail_count":0,"p0_pass":30,"p0_total":30,'
    '"p0_nonpass":["rider-stop2-done"]}\n'
    "EOF\n"
    "exit 1\n"
)

# Variante realiste : un P0 en SKIP/WARN laisse fail_count a 0 et
# p0_pass < p0_total. Meme verdict attendu (FAIL).
RUN_P0_NONPASS_REAL = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'cat > "$OUT/results.txt" <<\'EOF\'\n'
    "POINT|P0|SKIP\n"
    "EOF\n"
    'cat > "$OUT/report.json" <<\'EOF\'\n'
    '{"verdict":"FAIL","fail_count":0,"p0_pass":29,"p0_total":30,'
    '"p0_nonpass":["rider-stop2-done"]}\n'
    "EOF\n"
    "exit 1\n"
)

# (R4a) rapport FORGE : verdict=PASS et compteurs P0 propres, mais
# fail_count > 0 et p0_nonpass VIDE. Seule la clause "fail_count == 0" peut
# alors rendre FAIL : le mutant "suppression de la clause fail_count" doit
# etre TUYE par ce cas (sinon : faux PASS/0 sur rapport corrompu).
RUN_FAIL_COUNT = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'printf "POINT|P0|PASS\\n" > "$OUT/results.txt"\n'
    'cat > "$OUT/report.json" <<\'EOF\'\n'
    '{"verdict":"PASS","fail_count":2,"p0_pass":30,"p0_total":30,'
    '"p0_nonpass":[]}\n'
    "EOF\n"
    "exit 0\n"
)

# (R4b) rapport TRONQUE : le JSON est coupe en plein (write interrompu,
# artefact corrompu). Un rapport illisible ne doit JAMAIS produire de faux
# PASS : le verdict doit etre FAIL et le rc 1.
RUN_TRUNCATED = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'printf "POINT|P0|PASS\\n" > "$OUT/results.txt"\n'
    'printf \'{"verdict":"PASS","p0_pass":30,"p0_to\' > "$OUT/report.json"\n'
    "exit 0\n"
)

# (R4b) rapport a RACINE NON-OBJET : JSON valide, mais ce n'est pas un
# objet (liste de nombres). Meme verdict attendu : FAIL/1.
RUN_NON_OBJECT = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'OUT="${OUT:-e2e-out}"\n'
    'mkdir -p "$OUT"\n'
    'printf "1\\n" >> "$(dirname "${BASH_SOURCE[0]}")/calls.txt"\n'
    'printf "POINT|P0|PASS\\n" > "$OUT/results.txt"\n'
    'printf \'[1,2,3]\\n\' > "$OUT/report.json"\n'
    "exit 0\n"
)


def write_exec(path, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, 0o755)


def make_sandbox(tmpdir, oc_body, run_body=RUN_FAKE, script_text=None):
    """Copie le VRAI script dans un bac a sable jetable + faux outils.

    `script_text` permet de rejouer une VARIANTE du script (ex. sans le
    `|| true`) pour la demonstration rouge.
    """
    os.makedirs(os.path.join(tmpdir, "e2e"))
    os.makedirs(os.path.join(tmpdir, "bin"))
    if script_text is None:
        shutil.copyfile(SCRIPT, os.path.join(tmpdir, "e2e", "ci-opencode-run.sh"))
    else:
        write_exec(os.path.join(tmpdir, "e2e", "ci-opencode-run.sh"), script_text)
    write_exec(os.path.join(tmpdir, "e2e", "run.sh"), run_body)
    write_exec(os.path.join(tmpdir, "bin", "opencode"), oc_body)
    write_exec(os.path.join(tmpdir, "bin", "adb"), ADB_FAKE)


def run_script(tmpdir, env_extra=None):
    env = dict(os.environ)
    env["PATH"] = os.path.join(tmpdir, "bin") + os.pathsep + env.get("PATH", "")
    # Le bac a sable doit etre hermetique : aucun OUT/verdict modele/summary
    # herite de l'environnement appelant.
    for key in ("OUT", "VERDICT", "GITHUB_STEP_SUMMARY", "OC_MODEL",
                "OC_MODEL_CHAIN", "OC_MODEL_USED", "EMU_UPTIME_S",
                "OC_REPAIR_BUDGET_MIN", "OC_REPAIR_BUDGET_S",
                "OC_REPAIR_MODEL_MAX_S", "E2E_STEP_BUDGET_MIN",
                "E2E_PRE_SCRIPT_MIN", "E2E_PRE_SCRIPT_ALLOW_S",
                "E2E_FLUSH_MARGIN_MIN", "E2E_SAFETY_MARGIN_MIN",
                "SCENARIO"):
        env.pop(key, None)
    env["ARTEMIS_DISABLE"] = "1"
    # Papa 10:20 : le job Artemis peut exporter SCENARIO=lot ; ces specs
    # unitaires ciblent le harnais multistop (faux run.sh uniquement).
    env["SCENARIO"] = "multistop"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "e2e/ci-opencode-run.sh", "run"],
        cwd=tmpdir, capture_output=True, text=True, timeout=180, env=env)


def out_file(tmpdir, name):
    return os.path.join(tmpdir, "e2e-out", name)


def _count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return len([l for l in fh.read().splitlines() if l.strip()])


def calls(tmpdir):
    return _count_lines(os.path.join(tmpdir, "e2e", "calls.txt"))


def oc_calls(tmpdir):
    return _count_lines(os.path.join(tmpdir, "bin", "oc-calls.txt"))


def harness_deadline(tmpdir):
    """RUN_DEADLINE_MIN recu par le harnais (faux run.sh) — la preuve que le
    budget effectif est REELLEMENT passe au harnais, pas seulement journalise."""
    path = os.path.join(tmpdir, "e2e", "deadline.txt")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        values = [line.strip() for line in fh if line.strip()]
    return values[-1] if values else None


def log_value(stdout, key, suffix=""):
    """Extrait la valeur numerique d'une cle du journal `budget: ...`."""
    import re
    m = re.search(re.escape(key) + r"=(\d+)" + re.escape(suffix), stdout)
    assert m, f"la cle {key} est absente du journal : {stdout[-800:]}"
    return int(m.group(1))


def workflow_text():
    path = os.path.join(ROOT, "..", ".github", "workflows",
                        "artemis-staging-e2e.yml")
    with open(os.path.abspath(path), encoding="utf-8") as fh:
        return fh.read()


# --- cas rouges/verts -------------------------------------------------------

@case
def test_direct_harness_first_and_agent_never_called():
    """Le harnais direct tranche avant l'agent : opencode n'est pas invoque.

    Sur le fichier d'avant inversion ce cas est ROUGE : run_opencode part en
    premier, le faux opencode (rc=124 immediat) est appele une fois par modele de la
    chaine avant que le harnais direct ne tourne.
    """
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT)
        proc = run_script(d)
        assert oc_calls(d) == 0, (
            "l'agent opencode ne doit pas etre appele quand le harnais "
            f"deterministe a rendu un verdict (vu {oc_calls(d)} invocation(s))")
        assert calls(d) == 1, f"harnais direct attendu 1 fois, vu {calls(d)}"
        assert os.path.exists(out_file(d, "results.txt")), "results.txt absent"
        assert os.path.exists(out_file(d, "report.json")), "report.json absent"
        assert proc.returncode == 0, (
            f"cmd_run doit rendre 0 quand le harnais direct rend PASS : "
            f"rc={proc.returncode} stdout={proc.stdout[-800:]} "
            f"stderr={proc.stderr[-800:]}")
        with open(out_file(d, "report.json"), encoding="utf-8") as fh:
            report = json.load(fh)
        assert report["verdict"] == "PASS", report
        assert DIRECT_LOG in proc.stdout, (
            f"la ligne du harnais direct est absente du journal : "
            f"{proc.stdout[-800:]}")
        assert SKIP_AGENT_LOG in proc.stdout, (
            f"le journal doit dire que l'agent n'est pas appele : "
            f"{proc.stdout[-800:]}")


@case
def test_repair_leg_called_once_when_harness_renders_nothing():
    """run.sh muet -> la jambe de reparation opencode tourne une seule fois."""
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_VERDICT_PASS, run_body=RUN_SILENT)
        proc = run_script(d)
        assert calls(d) == 1, f"harnais direct attendu 1 fois, vu {calls(d)}"
        assert oc_calls(d) == 1, (
            f"opencode doit etre appele exactement 1 fois en reparation, "
            f"vu {oc_calls(d)}")
        assert proc.returncode == 0, (
            f"le faux opencode ecrit verdict.txt=PASS, cmd_run doit rendre 0 : "
            f"rc={proc.returncode} stdout={proc.stdout[-800:]} "
            f"stderr={proc.stderr[-800:]}")
        assert DIRECT_LOG in proc.stdout, "la ligne du harnais direct est absente"
        assert REPAIR_LOG in proc.stdout, (
            f"la ligne de la jambe de reparation est absente : "
            f"{proc.stdout[-800:]}")
        with open(out_file(d, "verdict.txt"), encoding="utf-8") as fh:
            assert fh.readline().strip() == "PASS"


@case
def test_fallback_path_keeps_the_verdict_from_report_json():
    """Aucun affaiblissement : un P0 en echec reste FAIL, agent non appele."""
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT, run_body=RUN_FAIL)
        proc = run_script(d)
        assert oc_calls(d) == 0, (
            "le harnais a rendu des resultats FAIL : l'agent ne doit pas etre "
            f"appele (vu {oc_calls(d)} invocation(s))")
        assert proc.returncode == 1, (
            f"un P0 en echec doit rendre FAIL/1 : rc={proc.returncode} "
            f"stdout={proc.stdout[-800:]}")
        with open(out_file(d, "verdict.txt"), encoding="utf-8") as fh:
            content = fh.read()
        first = content.splitlines()[0].strip()
        assert first == "FAIL", f"verdict.txt doit etre FAIL, lu {first!r}"
        assert "P0-7" in content, f"P0-7 absent du verdict : {content!r}"


@case
def test_pass_with_warnings_passes_when_the_substance_is_complete():
    """report.py rend PASS_WITH_WARNINGS (SKIP non-P0) : PASS/0 attendu.

    Mesure du defaut : derive_verdict n'acceptait que la chaine exacte
    "PASS" -> un run parfait (30/30 P0, 0 fail, SKIP sur llm-vision et
    artemis-smoke) sortait en ROUGE avec verdict.txt="FAIL". La decision est
    la SUBSTANCE : fail_count == 0 ET p0_total > 0 ET p0_pass == p0_total
    ET p0_nonpass vide.
    """
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT, run_body=RUN_WARNINGS)
        proc = run_script(d)
        assert proc.returncode == 0, (
            f"PASS_WITH_WARNINGS avec 0 fail et 30/30 P0 doit rendre 0 : "
            f"rc={proc.returncode} stdout={proc.stdout[-800:]} "
            f"stderr={proc.stderr[-800:]}")
        with open(out_file(d, "verdict.txt"), encoding="utf-8") as fh:
            content = fh.read()
        first = content.splitlines()[0].strip()
        assert first == "PASS", f"verdict.txt doit etre PASS, lu {first!r}"
        assert "fails=0" in content, (
            f"verdict.txt doit porter les chiffres, pas un simple mot : "
            f"{content!r}")
        assert "p0=30/30" in content, (
            f"verdict.txt doit porter p0_pass/p0_total : {content!r}")
        assert "nonpass=none" in content, (
            f"verdict.txt doit porter p0_nonpass : {content!r}")


@case
def test_p0_nonpass_forces_fail():
    """Un p0_nonpass non vide rend FAIL/1, meme avec fail_count == 0.

    La clause "p0_nonpass vide" est indépendante de p0_pass == p0_total :
    les deux variantes sont refusees.
    """
    variants = (("compteurs-coherents", RUN_P0_NONPASS),
                ("p0-reellement-echoue", RUN_P0_NONPASS_REAL))
    for label, run_body in variants:
        with tempfile.TemporaryDirectory() as d:
            make_sandbox(d, OC_TIMEOUT, run_body=run_body)
            proc = run_script(d)
            assert proc.returncode == 1, (
                f"[{label}] un p0_nonpass non vide doit rendre 1 : "
                f"rc={proc.returncode} stdout={proc.stdout[-800:]} "
                f"stderr={proc.stderr[-800:]}")
            with open(out_file(d, "verdict.txt"), encoding="utf-8") as fh:
                content = fh.read()
            first = content.splitlines()[0].strip()
            assert first == "FAIL", (
                f"[{label}] verdict.txt doit etre FAIL, lu {first!r}")
            assert "rider-stop2-done" in content, (
                f"[{label}] le P0 non-PASS doit etre nomme : {content!r}")
            assert "fails=" in content and "p0=" in content, (
                f"[{label}] verdict.txt doit porter les chiffres : {content!r}")


@case
def test_deadline_124_derives_a_partial_verdict_without_calling_the_agent():
    """(a) Le harnais sort en 124 avec report.json -> verdict DEDUIT.

    C'est le symptome du run 43956819722 (0 results.txt, 0 report.json) : le
    step est coupe AVANT le verdict. Ici le harnais rend la main en 124 avec
    un rapport partiel : derive_verdict doit en deduire un FAIL (substance
    incomplete) et l'agent opencode ne doit PAS etre appele (le harnais a
    rendu des artefacts).
    """
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT, run_body=RUN_DEADLINE_124)
        proc = run_script(d)
        assert calls(d) == 1, f"harnais direct attendu 1 fois, vu {calls(d)}"
        assert oc_calls(d) == 0, (
            "le harnais a rendu report.json : l'agent ne doit pas etre appele "
            f"(vu {oc_calls(d)} invocation(s))")
        assert proc.returncode == 1, (
            f"un rapport partiel (12/30 P0) doit rendre FAIL/1 : "
            f"rc={proc.returncode} stdout={proc.stdout[-800:]}")
        assert os.path.exists(out_file(d, "report.json")), "report.json absent"
        with open(out_file(d, "verdict.txt"), encoding="utf-8") as fh:
            content = fh.read()
        first = content.splitlines()[0].strip()
        assert first == "FAIL", f"verdict.txt doit etre FAIL, lu {first!r}"
        assert "rider-stop2-done" in content, (
            f"le P0 non-PASS doit etre nomme dans le verdict : {content!r}")
        assert "p0=12/30" in content, (
            f"le verdict doit porter les compteurs du rapport partiel : {content!r}")
        assert "rc=124" in proc.stdout, (
            f"le rc=124 (deadline interne) doit etre journalise : {proc.stdout[-800:]}")


@case
def test_or_true_is_structural_derive_verdict_is_reached():
    """(b) Le `|| true` est STRUCTUREL : sans lui, derive_verdict n'est pas
    atteint ; avec lui, le verdict est rendu.

    run_opencode() reactive `set -e` a chaque tour de boucle : sans la liste
    `||`, le retour non-zero de la fonction tue le script AVANT
    derive_verdict (mesure : rc=124, rien apres la boucle).
    """
    with open(SCRIPT, encoding="utf-8") as fh:
        real = fh.read()
    assert "run_opencode || true" in real, "le `|| true` a disparu du script"
    variant = real.replace("run_opencode || true", "run_opencode")
    assert variant != real, "la variante sans `|| true` n'a pas ete produite"

    # --- ROUGE : sans le `|| true`, le script s'arrete avant derive_verdict.
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT, run_body=RUN_SILENT, script_text=variant)
        proc = run_script(d)
        assert "verdict=" not in proc.stdout, (
            "sans `|| true`, derive_verdict ne doit PAS etre atteint — le "
            f"script s'arrete avant : {proc.stdout[-800:]}")
        assert not os.path.exists(out_file(d, "verdict.txt")), (
            "sans `|| true`, aucun verdict.txt ne doit etre ecrit")

    # --- VERT : avec le `|| true`, derive_verdict est atteint.
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT, run_body=RUN_SILENT)
        proc = run_script(d)
        assert "verdict=" in proc.stdout, (
            f"avec `|| true`, derive_verdict doit etre atteint : {proc.stdout[-800:]}")
        assert os.path.exists(out_file(d, "verdict.txt")), (
            "avec `|| true`, verdict.txt doit etre ecrit")
        with open(out_file(d, "verdict.txt"), encoding="utf-8") as fh:
            first = fh.readline().strip()
        assert first == "FAIL", (
            f"sans rapport ni verdict.txt, le verdict derive est FAIL, lu {first!r}")


@case
def test_repair_leg_stops_under_its_global_budget():
    """(c) La jambe de reparation est BORNEE par son plafond global (DUR).

    Budget reduit (6 s) + chaine de 10 modeles + un modele qui STALLE en
    IGNORANT SIGTERM : sans SIGKILL, le plafond ne serait pas un plafond
    (GNU timeout attend indefiniment). La jambe doit rendre la main en
    <= budget + grace, le budget doit etre journalise, et derive_verdict doit
    TOUJOURS tourner apres la jambe (FAIL/1 attendu : aucun verdict).
    """
    grace = 10
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_STALL, run_body=RUN_SILENT)
        started = time.time()
        proc = run_script(d, {"OC_REPAIR_BUDGET_S": "6"})
        elapsed = time.time() - started
        assert "budget global=" in proc.stdout, (
            f"le budget global de la jambe doit etre journalise : {proc.stdout[-800:]}")
        assert "kill après" in proc.stdout, (
            f"la grace SIGKILL du plafond doit etre journalisee : "
            f"{proc.stdout[-800:]}")
        assert oc_calls(d) == 1, (
            f"la jambe doit s'arreter sous son plafond (1 invocation attendue, "
            f"vu {oc_calls(d)} — la chaine en compte 10)")
        # R2 : le plafond est DUR. Sans SIGKILL, l'enfant qui ignore SIGTERM
        # bloquerait le script AU-DELA du budget (blocage mesure : 90 s pour
        # un budget de 6 s).
        assert elapsed < 6 + grace + 15, (
            f"le plafond doit etre DUR (SIGKILL apres {grace}s de grace) : la "
            f"jambe a dure {elapsed:.1f}s pour un budget de 6s")
        assert elapsed < 60, (
            f"la jambe a dure {elapsed:.1f}s : le plafond global n'a pas ete respecte")
        assert "verdict=" in proc.stdout, (
            "derive_verdict doit tourner APRES la jambe de reparation : "
            f"{proc.stdout[-800:]}")
        assert proc.returncode == 1, (
            f"aucun verdict sans rapport : FAIL/1 attendu, rc={proc.returncode}")
        assert os.path.exists(out_file(d, "verdict.txt")), "verdict.txt absent"


@case
def test_effective_budget_accounts_for_the_time_already_spent():
    """(P2+R1) Le budget EFFECTIF du harnais tient compte du temps deja ecoule.

    - la valeur du workflow (RUN_DEADLINE_MIN=90) reste le PLAFOND ;
    - le temps deja consomme (boot de l'emulateur + prep) le BORNE ;
    - R1 : PRE_SCRIPT_MIN est un PLANCHER, pas un repli. Le temps pre-script
      retenu vaut max(uptime mesure + marge, 15 min) : l'uptime de l'emulateur
      ne voit pas le setup de l'action (~1,4 Go d'image systeme + creation de
      l'AVD), il sous-estime donc le temps reellement consomme ;
    - les DEUX composantes (uptime mesure ET plancher applique) sont
      journalisees ;
    - le budget effectif est REELLEMENT passe au harnais (RUN_DEADLINE_MIN
      recu par e2e/run.sh) et journalise.
    """
    # Cas A : emulateur demarre a l'instant (uptime 0) -> le PLANCHER de
    # 15 min joue malgre une mesure d'uptime nulle.
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT)
        proc = run_script(d, {"EMU_UPTIME_S": "0.00"})
        assert "budget harnais=" in proc.stdout, proc.stdout[-800:]
        eff = log_value(proc.stdout, "budget harnais", "min")
        pre = log_value(proc.stdout, "pré-script", "s")
        step = log_value(proc.stdout, "step", "min")
        flush = log_value(proc.stdout, "flush", "s")
        safety = log_value(proc.stdout, "safety", "s")
        assert pre == 900, (
            f"le plancher pre-script (15 min) doit s'appliquer meme quand "
            f"l'uptime mesure est nul, vu {pre}s")
        assert "uptime=" in proc.stdout, (
            f"l'uptime mesure doit etre journalise : {proc.stdout[-800:]}")
        assert "plancher" in proc.stdout, (
            f"le plancher applique doit etre journalise : {proc.stdout[-800:]}")
        # Budget borne par 100 - 15 (plancher) - flush - slack.
        ceiling = (step * 60 - pre - flush - safety) // 60
        assert ceiling - 1 <= eff <= ceiling, (
            f"le budget effectif doit etre borne par le plancher (~{ceiling}min"
            f"), vu {eff}min")
        assert eff < 90, (
            f"15 min sont deja consommees hors script : le budget ne doit pas "
            f"atteindre le plafond du workflow (90), vu {eff}min")
        assert harness_deadline(d) == str(eff), (
            f"le harnais doit recevoir le budget effectif {eff}, vu "
            f"{harness_deadline(d)}")

    # Cas B : emulateur deja up depuis 3600 s -> la MESURE domine le plancher.
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT)
        proc = run_script(d, {"EMU_UPTIME_S": "3600.00"})
        assert "budget harnais=" in proc.stdout, proc.stdout[-800:]
        eff = log_value(proc.stdout, "budget harnais", "min")
        pre = log_value(proc.stdout, "pré-script", "s")
        assert pre == 3600 + 120, (
            f"au-dela du plancher, le temps pre-script doit suivre l'uptime "
            f"mesure + marge, vu {pre}s")
        assert eff < 90, (
            f"le budget effectif doit etre borne par le temps deja ecoule, "
            f"vu {eff}min")
        assert harness_deadline(d) == str(eff), (
            f"le harnais doit recevoir le budget effectif {eff}, vu "
            f"{harness_deadline(d)}")
        # L'arithmetique du budget tient : budget harnais + temps deja
        # consomme + flush + slack <= budget du step.
        step = log_value(proc.stdout, "step", "min")
        prep = log_value(proc.stdout, "prép", "s")
        flush = log_value(proc.stdout, "flush", "s")
        safety = log_value(proc.stdout, "safety", "s")
        assert eff * 60 + pre + prep + flush + safety <= step * 60, (
            f"le budget deborde du step : {eff}min + {pre}s + {prep}s + "
            f"{flush}s + {safety}s > {step}min")
        # Le temps deja consomme est visible dans le log.
        assert "temps déjà consommé=" in proc.stdout, proc.stdout[-800:]
        assert "budget effectif=" in proc.stdout, proc.stdout[-800:]
        # ... et dans e2e-out/run.log, meme apres la REINITIALISATION de ce
        # fichier par le harnais (le vrai run.sh fait `: > run.log`) : le
        # budget est re-journalise apres le harnais.
        with open(out_file(d, "run.log"), encoding="utf-8") as fh:
            run_log = fh.read()
        assert "budget effectif=" in run_log, (
            f"le budget effectif doit etre visible dans e2e-out/run.log : {run_log[-800:]}")
        assert "temps déjà consommé=" in run_log, run_log[-800:]


@case
def test_script_budget_defaults_stay_in_sync_with_the_workflow():
    """Les defauts du script doivent SUIVRE le workflow (pas l'inverse).

    Le workflow fixe timeout-minutes: 100 (step) et RUN_DEADLINE_MIN: "90".
    Le script ne peut pas modifier le workflow : ses defauts doivent donc
    reproduire ces valeurs, sinon le budget derive ne protegerait rien.
    """
    import re
    with open(SCRIPT, encoding="utf-8") as fh:
        script = fh.read()
    wf = workflow_text()
    # Step renommé Papa 10:20 : « E2E ${{ inputs.scenario }} (opencode + API fallback) ».
    step = int(re.search(
        r"- name: E2E \$\{\{ inputs\.scenario \}\} \([^)]+\)\n\s+timeout-minutes: (\d+)",
        wf,
    ).group(1))
    wf_deadline = int(re.search(r'RUN_DEADLINE_MIN: "(\d+)"', wf).group(1))
    script_step = int(re.search(r'E2E_STEP_BUDGET_MIN:-(\d+)', script).group(1))
    script_cap = int(re.search(r'RUN_DEADLINE_CAP="\$\{RUN_DEADLINE_MIN:-(\d+)\}"', script).group(1))
    assert script_step == step, (
        f"E2E_STEP_BUDGET_MIN ({script_step}) doit suivre le timeout du step "
        f"du workflow ({step})")
    assert script_cap == wf_deadline, (
        f"le plafond RUN_DEADLINE_MIN ({script_cap}) doit suivre la valeur du "
        f"workflow ({wf_deadline})")


@case
def test_fail_count_blocks_pass_even_with_clean_p0_counters():
    """(R4a) fail_count > 0 rend FAIL/1 meme si p0_nonpass est vide.

    Le mutant "suppression de la clause fail_count" doit etre TUYE : sans
    elle, un rapport forger portant verdict=PASS, fail_count=2 et des
    compteurs P0 propres ressortait en PASS/0 (faux vert).
    """
    with tempfile.TemporaryDirectory() as d:
        make_sandbox(d, OC_TIMEOUT, run_body=RUN_FAIL_COUNT)
        proc = run_script(d)
        assert proc.returncode == 1, (
            f"un rapport portant fail_count=2 doit rendre FAIL/1 : "
            f"rc={proc.returncode} stdout={proc.stdout[-800:]}")
        with open(out_file(d, "verdict.txt"), encoding="utf-8") as fh:
            content = fh.read()
        first = content.splitlines()[0].strip()
        assert first == "FAIL", f"verdict.txt doit etre FAIL, lu {first!r}"
        assert "fail_count=2" in content, (
            f"le verdict doit nommer le fail_count : {content!r}")


@case
def test_unreadable_report_never_yields_a_false_pass():
    """(R4b) Un report.json tronque (ou a racine non-objet) rend FAIL/1.

    Le mutant "rapport.json illisible traite comme PASS" doit etre TUYE : un
    rapport corrompu ne doit JAMAIS produire de faux vert.
    """
    for label, run_body in (("tronque", RUN_TRUNCATED),
                            ("racine non-objet", RUN_NON_OBJECT)):
        with tempfile.TemporaryDirectory() as d:
            make_sandbox(d, OC_TIMEOUT, run_body=run_body)
            proc = run_script(d)
            assert proc.returncode == 1, (
                f"[{label}] un rapport illisible doit rendre FAIL/1 : "
                f"rc={proc.returncode} stdout={proc.stdout[-800:]}")
            with open(out_file(d, "verdict.txt"), encoding="utf-8") as fh:
                content = fh.read()
            first = content.splitlines()[0].strip()
            assert first == "FAIL", (
                f"[{label}] verdict.txt doit etre FAIL, lu {first!r}")
            assert "report illisible" in content, (
                f"[{label}] le verdict doit nommer la cause : {content!r}")


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} ci-direct-harness cases passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
