#!/usr/bin/env python3
"""TDD spec for « offre prête » avant le tap (run 41319989273).

Cause de run : CustomNewRideDialog garde le bouton « Accepter la course »
présent mais INACTIF tant que le tarif n'est pas chargé ; le harnais tapait
trop tôt puis la fenêtre expirait (ui_left=0s, repli API).

No emulator, no network. Pins three contracts:
  * run.sh: wait_offer_ready AVANT le premier tap "Accepter la course" ;
  * run.sh: réessai unique du tap si l'écran ne change pas, le wait_ui du
    réessai vit sur offer_left - API_RESERVE_SEC (jamais un tap+attente complet
    deux fois de suite) ;
  * ui.sh: _find_exact_match / wait_offer_ready (fixture XML, stub adb) —
    prêt seulement quand tarif affiché ET bouton actif.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
UI = os.path.join(ROOT, "ui.sh")
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_text():
    return open(RUN, encoding="utf-8").read()


def ui_text():
    return open(UI, encoding="utf-8").read()


# ------------------------------------------------------------- fixtures
def _offer_fixture(enabled, fare):
    fare_node = (
        '<node index="2" text="Prix recommandé : 3000 FCFA, distance environ 8 km" '
        'visible-to-user="true" bounds="[50,900][900,1000]" />\n    '
        if fare
        else ""
    )
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
        '<hierarchy rotation="0">\n'
        '  <node index="0" text="" content-desc="" visible-to-user="true" '
        'bounds="[0,0][1080,2400]">\n    '
        f'<node index="1" text="Accepter la&#10;course" content-desc="" '
        f'clickable="true" enabled="{enabled}" '
        'visible-to-user="true" bounds="[100,1500][980,1650]" />\n    '
        f"{fare_node}"
        '<node index="3" text="REFUSER" content-desc="" '
        'visible-to-user="true" bounds="[50,1700][400,1800]" />\n'
        "  </node>\n</hierarchy>\n"
    )


READY_FIXTURE = _offer_fixture("true", fare=True)
DISABLED_FIXTURE = _offer_fixture("false", fare=False)
ENABLED_NO_FARE_FIXTURE = _offer_fixture("true", fare=False)


def _ui_env():
    """Fixture XML + stub adb, comme test_markers.py (preuve NON RÉELLE opt-in)."""
    tmp = tempfile.mkdtemp(prefix="e2e-offer-ready-")
    xml = os.path.join(tmp, "w.xml")
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir)
    stub = os.path.join(bin_dir, "adb")
    with open(stub, "w") as fh:
        fh.write("#!/usr/bin/env bash\nexit 0\n")
    os.chmod(stub, 0o755)
    env = dict(os.environ, OUT=tmp, UI_XML=xml, UI_XML_SRC=xml, E2E_ALLOW_FIXTURE="1",
               SMOKE_LOG=os.path.join(tmp, "smoke.log"),
               PATH=bin_dir + os.pathsep + os.environ["PATH"])
    env.pop("GITHUB_ACTIONS", None)
    return xml, env


def run_ui(snippet, fixture, **kw):
    xml, env = _ui_env()
    with open(xml, "w", encoding="utf-8") as fh:
        fh.write(fixture)
    env.update(kw)
    return subprocess.run(
        ["bash", "-c", '. "$1"; ' + snippet, "_", UI],
        capture_output=True, text=True, env=env, timeout=60,
    )


# ------------------------------------------------------ contrat run.sh
@case
def test_run_waits_for_offer_ready_before_tap():
    body = run_text()
    i_ready = body.index('wait_offer_ready "Accepter la course"')
    i_tap = body.index('tap_exact "Accepter la course"')
    assert i_ready < i_tap, "le tap doit venir APRÈS wait_offer_ready"
    # Aucun tap inconditionnel : le premier tap_exact est dans la branche prête.
    assert re.search(r'im_cmd ', body) is None
    fn_section = body[i_ready:i_tap]
    assert '"$ui_left"' in fn_section, fn_section
    # La fonction vit dans ui.sh, jamais inline dans run.sh.
    fn = re.search(r"^wait_offer_ready\(\) \{(.*?)^\}", ui_text(), re.M | re.S)
    assert fn, "wait_offer_ready() doit vivre dans ui.sh"
    assert "Prix recommandé" in fn.group(1), fn.group(1)


@case
def test_run_retries_tap_once_when_screen_unchanged():
    body = run_text()
    i_first = body.index('tap_exact "Accepter la course"')
    i_retry = body.index('tap_exact "Accepter la course"', i_first + 1)
    between = body[i_first:i_retry]
    assert "réessai unique" in between, between
    # Le délai d'écran-inchangé est court (settle), et le wait_ui du réessai vit
    # sur ui_left REcalculé (offer_left - API_RESERVE_SEC) : jamais la fenêtre
    # entière consommée deux fois, la réserve du repli API reste intacte.
    assert "OFFER_TAP_SETTLE_SEC" in body, "settle tap absent"
    m = re.search(r'OFFER_TAP_SETTLE_SEC="\$\{OFFER_TAP_SETTLE_SEC:-(\d+)\}"', body)
    assert m and 3 <= int(m.group(1)) <= 15, m
    tail = body[i_retry:i_retry + 400]
    assert re.search(
        r'ui_left=\$\(\(\s*\$\(offer_left\)\s*-\s*API_RESERVE_SEC\s*\)\)', tail), tail
    assert 'wait_ui "$DRIVER_PKG" "$ui_left"' in tail, tail
    tail = body[i_retry:i_retry + 400]
    


@case
def test_tap_block_keeps_api_fallback_wiring():
    """Un bouton jamais prêt VA au repli API (pas de silence)."""
    body = run_text()
    i_ready = body.index('wait_offer_ready "Accepter la course"')
    end = body.index("if [ \"$ACCEPT_OK\" = \"0\" ]", i_ready)
    block = body[i_ready:end]
    assert 'log "offre: tap « Accepter la course » non joué' in block, block


# -------------------------------------------------- contrat helpers ui.sh
@case
def test_find_exact_match_reports_enabled_state():
    p = run_ui('echo "RET=$(_find_exact_match \'Accepter la course\')"', READY_FIXTURE)
    assert p.returncode == 0, (p.returncode, p.stderr)
    assert p.stdout.strip() == "RET=540 1575 true", (p.stdout, p.stderr)

    p = run_ui('echo "RET=$(_find_exact_match \'Accepter la course\')"', DISABLED_FIXTURE)
    assert p.returncode == 0, (p.returncode, p.stderr)
    assert p.stdout.strip() == "RET=540 1575 false", (p.stdout, p.stderr)

    p = run_ui('echo "RET=$(_find_exact_match \'Accepter la course\')"',
               '<hierarchy/>')
    assert p.returncode == 0 and p.stdout.strip() == "RET=", (p.stdout, p.stderr)


@case
def test_wait_offer_ready_passes_when_fare_shown_and_button_enabled():
    p = run_ui("wait_offer_ready 'Accepter la course' 5", READY_FIXTURE)
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    assert "offre prête" in p.stdout or "offre prête" in p.stderr, (p.stdout, p.stderr)


@case
def test_wait_offer_ready_times_out_while_button_disabled():
    """Le run 41319989273 en un fait : bouton présent, inactif, tarif absent."""
    p = run_ui("wait_offer_ready 'Accepter la course' 3", DISABLED_FIXTURE)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)


@case
def test_wait_offer_ready_times_out_without_fare_even_if_button_enabled():
    """Un bouton actif SANS tarif ne prouve rien : _respond avalerait le tap."""
    p = run_ui("wait_offer_ready 'Accepter la course' 3", ENABLED_NO_FARE_FIXTURE)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)


@case
def test_wait_offer_ready_label_other_than_accept():
    """Générique : le helper ne durcit pas « Accepter la course » dans le code."""
    p = run_ui("wait_offer_ready 'Terminer la course' 3", READY_FIXTURE)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)


# ------------------------------------------------------------------- main
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
    print(f"\n{len(CASES) - failed}/{len(CASES)} offer-ready cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
