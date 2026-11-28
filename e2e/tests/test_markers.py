#!/usr/bin/env python3
"""TDD spec for the screen markers in e2e/run.sh and the UI helpers in e2e/ui.sh.

Run with `python3 e2e/tests/test_markers.py`. No emulator, no network: every
device call is either a fixture (UI_XML_SRC) or a stub `adb` on PATH.
"""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
UI = os.path.join(ROOT, "ui.sh")
LOGCHECK = os.path.join(ROOT, "logcheck.sh")
SET_MODEL = os.path.join(ROOT, "set_model.py")
WORKFLOW = os.path.join(ROOT, "..", ".github", "workflows", "artemis-staging-e2e.yml")
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_text():
    return open(RUN, encoding="utf-8").read()


def ui_text():
    return open(UI, encoding="utf-8").read()


def markers():
    return dict(re.findall(r"^MARK_([A-Za-z0-9_]+)='([^']*)'", run_text(), re.M))


def p0_points():
    m = re.search(r'P0_POINTS="([^"]+)"', run_text(), re.S)
    assert m, "P0_POINTS introuvable"
    return set(m.group(1).replace("\\\n", " ").split())


def spec_groups(spec):
    """`+A|B&-C` -> [('+', ['A','B']), ('-', ['C'])] comme le `groups()` de e2e/ui.sh.

    `|` = OU dans le groupe courant ; `&` = ET (nouveau groupe, type hérité).
    """
    out, kind, new_group = [], None, True
    for raw in re.split(r"([|&])", spec):
        if raw == "|":
            new_group = False
            continue
        if raw == "&":
            new_group = True
            continue
        raw = raw.strip()
        if not raw:
            continue
        if raw[0] in "+-":
            kind, raw = raw[0], raw[1:]
        if not kind or not raw:
            continue
        if new_group or not out or out[-1][0] != kind:
            out.append((kind, [raw]))
        else:
            out[-1][1].append(raw)
        new_group = False
    return out


def spec_chunks(spec):
    """Vue plate de `spec_groups` : [(kind, valeur)] — pour les gardes de specs."""
    return [(kind, pat) for kind, pats in spec_groups(spec) for pat in pats]


FIXTURE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Où allez-vous ?" content-desc="" visible-to-user="true" bounds="[100,200][900,300]" />
    <node index="2" text="Accepter la&#10;course" content-desc="" visible-to-user="true" bounds="[100,800][600,1000]" />
    <node index="3" text="Accepter" content-desc="" visible-to-user="true" bounds="[700,800][900,900]" />
    <node index="4" text="Chargement..." content-desc="" visible-to-user="false" bounds="[100,1500][600,1600]" />
    <node index="5" text="Arrêt 1" content-desc="" visible-to-user="true" bounds="[0,0][0,0]" />
  </node>
</hierarchy>
"""


def spec_ok(spec, fixture=FIXTURE):
    """Run the real bash _spec_ok against a fixture XML."""
    tmp = tempfile.mkdtemp(prefix="e2e-spec-")
    xml = os.path.join(tmp, "w.xml")
    with open(xml, "w", encoding="utf-8") as fh:
        fh.write(fixture)
    env = dict(os.environ, OUT=tmp, UI_XML=xml)
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; _spec_ok "$2"', "_", UI, spec],
        capture_output=True, text=True, env=env)
    return proc.returncode


# ------------------------------------------------------------------ cases
@case
def test_every_cap_point_has_a_marker():
    text = run_text()
    points = [p for p in re.findall(r"^\s*cap\s+([a-z0-9][a-z0-9-]+)\s+", text, re.M)]
    assert points, "aucun appel cap trouvé"
    known = markers()
    missing = [p for p in points if p.replace("-", "_") not in known]
    assert not missing, f"markers manquants: {missing}"


@case
def test_every_marker_has_a_positive_spec():
    bad = {k: v for k, v in markers().items() if not any(c[0] == "+" for c in spec_chunks(v))}
    assert not bad, f"specs sans positif: {bad}"


GENERIC_POS = re.compile(
    r"^(destination|arr[eê]t( \d+)?|note[rz]?|cash|avis|gains|r[eé]sum[eé]"
    r"|terminer la course|prise en charge|arr[eê]t en cours)$", re.I)


@case
def test_no_generic_only_marker():
    """B2: no P0 marker may rest on a generic label alone — `Destination` in particular
    must never be the sole positive of a P0 marker."""
    known = markers()
    p0 = {p.replace("-", "_") for p in p0_points()}
    bad = {}
    for name, spec in known.items():
        if name not in p0:
            continue
        positives = [c[1].strip() for c in spec_chunks(spec) if c[0] == "+"]
        if not positives or all(GENERIC_POS.match(p) for p in positives):
            bad[name] = spec
    assert not bad, f"marqueurs génériques: {bad}"


@case
def test_b1_no_false_fail_on_running_start():
    spec = markers().get("rider_running_start", "")
    negatives = [c[1].lower() for c in spec_chunks(spec) if c[0] == "-"]
    assert not any("terminez votre course" in n for n in negatives), spec
    assert any(c[0] == "+" for c in spec_chunks(spec)), spec


@case
def test_b3_no_stop_requirement_on_final_itinerary():
    spec = markers().get("rider_final_itinerary", "")
    assert "arrêt" not in spec.lower() and "arret" not in spec.lower(), spec


@case
def test_b2_markers_exist():
    m = markers()
    for name in ("driver_offer_with_stops", "driver_accepted_details", "driver_stop1_done",
                 "driver_stop2_done", "driver_accepted_details"):
        assert name in m, name
    assert "accepter\\s+la\\s+course" in m["driver_offer_with_stops"].lower()


@case
def test_hypothesis_markers_are_the_nine_driver_screens():
    text = run_text()
    m = re.search(r"MARK_HYPOTHESIS=\"([^\"]*)\"", text, re.S)
    assert m, "MARK_HYPOTHESIS introuvable"
    hyp = {t for t in m.group(1).split() if re.fullmatch(r"[a-z0-9_]+", t)}
    expected = {"driver_offer_with_stops", "driver_accepted_details", "driver_pickup_arrived",
                "driver_running_stop1", "driver_stop1_done", "driver_stop2_done",
                "driver_cash_request", "driver_cash_confirmed", "driver_summary"}
    assert hyp == expected, hyp ^ expected


@case
def test_p0_includes_new_points():
    p0 = p0_points()
    for point in ("rider-destination-selected", "rider-cash-methods"):
        assert point in p0, point


@case
def test_five_api_fallbacks_are_adjacent_to_a_fail_mark():
    lines = run_text().splitlines()
    pairs = [("api accept", "driver-accepted-details", "api fallback: accept"),
             ("api start", "rider-running-start", "api fallback: start"),
             ("api stop_one", "driver-stop${n}-done", "api fallback: stop"),
             ("api end", "rider-destination-arrived", "api fallback: end"),
             ("api cash_driver", "driver-cash-confirmed", "api fallback: cash")]
    for call, point, mark_txt in pairs:
        idx = [i for i, line in enumerate(lines) if re.search(rf"\b{re.escape(call)}\b", line)]
        assert idx, f"{call} introuvable"
        ok = False
        for i in idx:
            window = "\n".join(lines[max(0, i - 6): i + 7])
            if ("FAIL" in window and "api fallback:" in window
                    and mark_txt in window and point in window):
                ok = True
        assert ok, f"{call}: aucun mark FAIL adjacent ({point} / {mark_txt})"


@case
def test_driver_stop_retry_precedes_the_api_fallback():
    """Run 44927908295 : aucun complete-stop reçu des taps UI. La voie UI (driver_stop_confirm_ui,
    preuve SERVEUR, 3 essais sans re-tap si l'arrêt est déjà vu) précède le repli API, qui reste
    le dernier ressort (mark FAIL adjacent + api stop_one) et ne compte jamais comme succès."""
    t = run_text()
    start = t.index('next_marker="MARK_driver_stop${n}_done"')
    end = t.index('mark "driver-stop${n}-done" FAIL', start)
    window = t[start:end]
    assert 'driver_stop_confirm_ui "$DRIVER_PKG" "$n"' in window, window
    assert '[ "$stop_rc" = "0" ] && ui_stop_ok=1' in window, window
    assert window.index('driver_stop_confirm_ui') < window.index('if [ "$ui_stop_ok" = "1" ]; then'), window
    ui = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.sh")).read()
    fn = ui[ui.index("driver_stop_confirm_ui() {"):ui.index("rider_leave_rating() {")]
    assert "for attempt in 1 2 3" in fn and "stop_one" not in fn, fn


@case
def test_spec_ok_honours_or_and_whitespace():
    assert spec_ok(r"+Accepter\s+la\s+course|-Accueil") == 0
    assert spec_ok("+Nope|+Où allez-vous") == 0
    assert spec_ok("+Nope") == 1
    assert spec_ok("-Où allez-vous") == 2


@case
def test_spec_ok_honours_and_operator():
    """`&` = ET entre groupes : deux signaux distincts du même écran sont tous deux exigés.
    `|` reste un OU : `+Nope|+Où allez-vous` passe. Aucune assertion n'est relâchée."""
    both = _xml("1/2 arrêt terminé", "En route vers E2E Stop 2")
    assert spec_ok("+1/2 arrêt terminé&+En route vers E2E Stop 2", both) == 0
    # compteur sans bannière -> ET non satisfait
    assert spec_ok("+1/2 arrêt terminé&+En route vers E2E Stop 2",
                   _xml("1/2 arrêt terminé")) == 1
    # bannière sans compteur -> ET non satisfait
    assert spec_ok("+1/2 arrêt terminé&+En route vers E2E Stop 2",
                   _xml("En route vers E2E Stop 2")) == 1
    # le OU reste un OU à l'intérieur d'un groupe
    assert spec_ok(r"+Nope|+Où allez-vous&+Accepter\s+la\s+course") == 0
    # groupes de type différent : ET conservé (positif et négatif)
    assert spec_ok("+Où allez-vous&-Chargement") == 0
    assert spec_ok(r"+Où allez-vous&-Accepter\s+la\s+course") == 2
    # un groupe positif non satisfait fait échouer le ET, même si le reste passe
    assert spec_ok(r"+Nope|-Chargement&+Où allez-vous") == 1
    # le type hérité traverse `&`
    assert spec_chunks("+A&+B") == [("+", "A"), ("+", "B")]
    assert spec_groups("+A|B&+C") == [("+", ["A", "B"]), ("+", ["C"])]


@case
def test_spec_ok_ignores_invisible_and_zero_area_nodes_for_negatives():
    # hidden Chargement node and zero-area Arrêt 1 node must not trip a negative spec
    assert spec_ok("-Chargement") == 0
    assert spec_ok("-Arrêt 1") == 0


@case
def test_spec_ok_requires_visible_for_positives():
    """B1: a positive must be visible-to-user too — an invisible node is not a screen."""
    assert spec_ok("+Chargement") == 1, "un noeud invisible ne peut pas satisfaire un positif"
    # area stays optional for positives: a visible zero-area node still counts
    assert spec_ok("+Arrêt 1") == 0


# ------------------------------------------------- B2 marker pre-state fixtures
SUMMARY_FIXTURE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Arrêt 1" content-desc="" visible-to-user="true" bounds="[50,600][900,700]" />
    <node index="2" text="Arrêt 2" content-desc="" visible-to-user="true" bounds="[50,800][900,900]" />
    <node index="3" text="Destination" content-desc="" visible-to-user="true" bounds="[50,1000][900,1100]" />
    <node index="4" text="Ajouter un arrêt" content-desc="" visible-to-user="true" bounds="[50,1200][900,1300]" />
    <node index="5" text="Confirmer la course" content-desc="" visible-to-user="true" bounds="[50,1500][900,1600]" />
  </node>
</hierarchy>
"""

RUNNING_FIXTURE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Terminer la course" content-desc="" visible-to-user="true" bounds="[50,2000][900,2100]" />
    <node index="2" text="Arrêt 1" content-desc="" visible-to-user="true" bounds="[50,600][900,700]" />
    <node index="3" text="Destination" content-desc="" visible-to-user="true" bounds="[50,800][900,900]" />
  </node>
</hierarchy>
"""

ACCEPTED_FIXTURE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Point de prise en charge" content-desc="" visible-to-user="true" bounds="[50,600][900,700]" />
    <node index="2" text="Arrêt 1" content-desc="" visible-to-user="true" bounds="[50,800][900,900]" />
  </node>
</hierarchy>
"""


@case
def test_p0_markers_do_not_match_prestate_screens():
    """B2: every rewritten marker must reject the screen that pre-exists its transition."""
    m = markers()
    pre = {
        "rider_running_start": SUMMARY_FIXTURE,
        "rider_stop1_active": SUMMARY_FIXTURE,
        "rider_stop2_done": SUMMARY_FIXTURE,
        "driver_running_stop1": RUNNING_FIXTURE,
        "driver_stop1_done": RUNNING_FIXTURE,
        "driver_stop2_done": RUNNING_FIXTURE,
        "driver_pickup_arrived": ACCEPTED_FIXTURE,
    }
    bad = {}
    for name, fixture in pre.items():
        spec = m.get(name, "")
        assert spec, f"marqueur absent: {name}"
        if spec_ok(spec, fixture) == 0:
            bad[name] = spec
    assert not bad, f"marqueurs satisfaits par l'écran pré-existant: {bad}"


# ------------------------------------------------ L1 markers re-grounded on UI
# Every fixture below carries the labels the Rider/Driver widgets actually render:
# Galass_Rider my_strings.dart:47/64/65/68/232/766 + ride_details_bottom_sheet_widget
# (stopsProgressText / currentLegBanner) and Galass_Driver my_strings.dart:74/82-85/935.

def _xml(*texts):
    """uiautomator dump with one visible, non-empty node per label."""
    rows = "".join(
        '    <node index="%d" text="%s" content-desc="" visible-to-user="true" '
        'bounds="[50,%d][900,%d]" />\n' % (i, t, 200 + i * 80, 260 + i * 80)
        for i, t in enumerate(texts))
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
            '<hierarchy rotation="0">\n'
            '  <node index="0" text="" content-desc="" visible-to-user="true" '
            'bounds="[0,0][1080,2400]">\n' + rows + "  </node>\n</hierarchy>\n")


# Rider: stop picker opened by « Ajouter un arrêt ».
RIDER_STOP_PICKER = _xml("Adresse de l'arrêt", "Confirmer le lieu", "Recherchez votre position")
# Rider: PENDING summary with the search toggle still open — pre-state of every
# post-start marker. The FULL timeline is rendered at PENDING, so the stop labels
# (« E2E Stop 1 Médina », « Arrêt 2 ») are already on screen here: neither can ever be
# a positive of a post-start marker.
RIDER_PENDING = _xml("Où allez-vous ?", "Arrêt 1", "E2E Stop 1 Médina", "Arrêt 2",
                     "Ajouter un arrêt", "Trouver un chauffeur", "Paiement en espèces")
# Papa 27/11 21:22 — F4-27 frise complète en RUNNING (plus de bandeaux En route / x/N).
RIDER_RUNNING_START = _xml("Lieu de prise en charge", "Arrêt 1", "Arrêt 2", "Destination",
                           "E2E Driver", "Terminer la course")
RIDER_STOP1_ACTIVE = RIDER_RUNNING_START
RIDER_LEG2 = RIDER_RUNNING_START
RIDER_STOP1_DONE = RIDER_RUNNING_START
RIDER_STOP2_DONE = _xml("Lieu de prise en charge", "Arrêt 1", "Arrêt 2", "Destination",
                        "Point E", "E2E Driver")
RIDER_FINAL_COUNTER = _xml("Arrêt 1", "Arrêt 2", "Course terminée")
RIDER_PLACES_STUCK = _xml("Confirmer le lieu", "Destination", "Fann, Senegal")  # 460606
# Rider at destination: what stop2_done must NOT accept as proof of a stop confirmation.
RIDER_END = _xml("Vous êtes arrivé", "Payer maintenant", "Facture à payer")

# Driver: offer toast above the dashboard, navbar « Accueil » still visible.
DRIVER_OFFER = _xml("Accueil", "Accepter la course", "REFUSER",
                    "Prix recommandé : 2 000 FCFA, distance environ 3.2 km",
                    "Consignes du client:")
DRIVER_STOP1_DONE = _xml("Arrêt 1", "Arrêt 2", "Terminer la course", "Medina, Dakar, Senegal", "Fann, Dakar, Senegal")
DRIVER_STOP2_DONE = _xml("Arrêt 1", "Arrêt 2", "Terminer la course", "Fann, Dakar, Senegal")


def compose_body():
    m = re.search(r"^compose_ride_ui\(\) \{.*?^\}", run_text(), re.M | re.S)
    assert m, "compose_ride_ui introuvable"
    return m.group(0)


@case
def test_l1_rider_stop_picker_matches_real_picker_labels():
    spec = markers()["rider_stop_picker"]
    assert spec_ok(spec, RIDER_STOP_PICKER) == 0, spec
    # The old positives (« Arrêt 1 », « Rechercher un lieu ») exist nowhere in the picker.
    assert spec_ok(spec, _xml("Arrêt 1", "Rechercher un lieu")) != 0, spec
    assert spec_ok(spec, RIDER_PENDING) != 0, spec


@case
def test_l1_rider_running_start_matches_f427_frise():
    spec = markers()["rider_running_start"]
    assert spec_ok(spec, RIDER_RUNNING_START) == 0, spec
    assert spec_ok(spec, RIDER_PENDING) != 0, spec
    assert spec_ok(spec, RIDER_PLACES_STUCK) != 0, spec
    assert "En route vers" not in spec, spec
    assert "+E2E Driver" in spec and "+Arrêt 1" in spec, spec


@case
def test_l1_rider_stop1_active_matches_f427_two_stops():
    spec = markers()["rider_stop1_active"]
    assert spec_ok(spec, RIDER_STOP1_ACTIVE) == 0, spec
    assert spec_ok(spec, RIDER_PENDING) != 0, spec
    assert spec_ok(spec, RIDER_PLACES_STUCK) != 0, spec
    assert "En route vers" not in spec, spec
    assert "+E2E Driver" in spec and "+Arrêt 1" in spec and "+Arrêt 2" in spec, spec


@case
def test_l1_rider_stop1_active_gated_by_api_state():
    """Point 22 is closed by the API state, not by the screen: stops_done=0 is asserted
    between the capture and the driver confirmation loop."""
    text = run_text()
    start = text.index("cap rider-stop1-active")
    end = text.index("next_marker=", start)
    window = text[start:end]
    assert 'grep -q "stops_done=0" "$OUT/last-ride-state.txt"' in window, window
    assert 'mark rider-stop1-active FAIL "état API incohérent : stops_done' in window, window
    assert '${!next_marker}' not in window, "la garde doit précéder la boucle de confirmation"


@case
def test_rider_stop1_done_matches_f427_arret_n():
    """Point 23 — Papa 27/11 21:22 : marqueurs = frise Arrêt N + chauffeur (bandeaux retirés)."""
    spec = markers().get("rider_stop1_done")
    assert spec == "+E2E Driver&+Arrêt 1&+Arrêt 2|-Ajouter un arrêt|-Confirmer le lieu|-En route vers", spec
    assert spec_ok(spec, RIDER_STOP1_DONE) == 0, spec
    assert spec_ok(spec, RIDER_PLACES_STUCK) != 0, spec
    assert spec_ok(spec, _xml("En route vers E2E Stop 2 Fann")) != 0, spec
    t = run_text()
    assert "cap rider-stop1-done 23-rider-stop1-done" in t
    assert "rider_open_active_ride" in t, "filet compose-API requis (460606)"


@case
def test_l1_rider_stop2_done_matches_f427_frise_not_end_screen():
    spec = markers()["rider_stop2_done"]
    assert spec == "+E2E Driver&+Arrêt 1&+Arrêt 2&+Destination|-Ajouter un arrêt|-Confirmer le lieu|-En route vers", spec
    assert spec_ok(spec, RIDER_STOP2_DONE) == 0, spec
    assert spec_ok(spec, RIDER_END) != 0, spec
    assert spec_ok(spec, RIDER_PENDING) != 0, spec
    assert spec_ok(spec, RIDER_PLACES_STUCK) != 0, spec


@case
def test_l1_rider_stop2_done_rejects_old_bandeaux_alone():
    """Anciens bandeaux x/N + En route : jamais PASS sous F4-27."""
    spec = markers()["rider_stop2_done"]
    assert spec_ok(spec, _xml("2/2 arrêts terminés", "En route vers E2E Dest Point E")) != 0, spec
    assert spec_ok(spec, _xml("En route vers Point E, Dakar, Senegal")) != 0, spec


@case
def test_rider_rating_marker_is_avis_screen():
    spec = markers()["rider_rating"]
    avis = _xml("Avis", "Course terminée", "Arrêt 1", "Arrêt 2")
    assert spec_ok(spec, avis) == 0, spec
    assert spec_ok(spec, RIDER_END) != 0, spec
    # « Course terminée » seul sur l'écran de paiement ne vaut pas preuve de l'écran Avis.
    assert spec_ok(spec, _xml("Course terminée", "Payer maintenant")) != 0, spec
    assert "Notez" not in spec, spec


# Écran de composition réel (run 44441344957, capture 30 rider-rating) : le marqueur 29
# l'acceptait, donc la capture 29 était un faux PASS.
RIDER_COMPOSITION = _xml("Lieu de prise en charge", "PG8J+MWG, 611 Av. El Hadj Mansour Sy.",
                         "Destination", "E2E Dest", "Confirmer le lieu")


@case
def test_rider_final_itinerary_rejects_composition_screen():
    """Run 44441344957 : les captures 26/27/29/30 tombaient sur l'écran de composition
    (« Lieu de prise en charge », « Destination », « Confirmer le lieu ») ; le marqueur 29
    acceptait cet écran, donc la capture 29 était un faux PASS. L'écran de clôture réel
    porte « Course terminée » + « Avis » + les arrêts, jamais « Confirmer le lieu »."""
    spec = markers()["rider_final_itinerary"]
    assert spec_ok(spec, RIDER_COMPOSITION) != 0, spec
    final = _xml("Course terminée", "Avis", "Arrêt 1", "Arrêt 2")
    assert spec_ok(spec, final) == 0, spec


@case
def test_rider_home_after_marker_is_the_real_home_screen():
    # Écran réel (run 45004400552, capture 31) : « Partons pour une course » + « Où allez-vous ? ».
    # L'ancien marqueur supposait l'écran de composition (« Confirmer le lieu »…) et ne l'a jamais reconnu.
    spec = markers()["rider_home_after"]
    home = _xml("E2E Rider MHV3+674, Rue CO 38, Dakar, Senegal", "Partons pour une course", "Où allez-vous ?")
    assert spec_ok(spec, home) == 0, spec
    assert spec_ok(spec, _xml("Avis", "Course terminée")) != 0, spec
    assert spec_ok(spec, _xml("Confirmer le lieu", "Lieu de prise en charge", "Destination")) != 0, spec


@case
def test_rider_pays_now_before_waiting_for_methods():
    t = run_text()
    i = t.index('"Payer maintenant"')
    assert i < t.index("cap rider-cash-methods 26-rider-cash-methods"), "tap avant la capture 26"
    assert i > t.index("cap rider-destination-arrived 25-rider-destination-arrived")


@case
def test_driver_pickup_dialog_confirmed_before_waiting_marker():
    t = run_text()
    a = t.index('"Prise en charge du client"')
    d = t.index('dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler"')
    w = t.index('wait_ui "$DRIVER_PKG" 25 "$MARK_driver_running_stop1"')
    assert a < d < w


@case
def test_pickup_diag_captures_backend_status_right_after_the_tap():
    """Diag 37/38/39 : le statut backend est lu JUSTE après le tap « Confirmer », et
    re-signalé en dernière ligne du run (sinon il sort du tail borné des logs).
    status=2 → start UI jamais reçu ; status=3 → reçu, bug de rafraîchissement Driver."""
    t = run_text()
    d = t.index('dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler"')
    r = t.index("ride_state", d)
    c = t.index("PICKUP_START_STATUS=$(sed -n", r)
    m = t.index('mark pickup-backend-status WARN', c)
    assert d < r < c < m, "l'ordre tap → state → capture → mark doit tenir"
    assert "status=3" in t and "status=2" in t, "les deux hypothèses doivent être nommées"
    # re-signalé après le rapport : la dernière ligne du log reste lisible dans un tail
    rep = t.index('"$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"')
    diag = t.index("diag final pickup:", rep)
    assert 'start_status=${PICKUP_START_STATUS:-inconnu}' in t[diag:diag + 400]
    # le diag ne touche aucun point P0 ni aucun verdict
    p0 = re.search(r'P0_POINTS="([^"]+)"', t, re.S).group(1)
    assert "pickup-backend-status" not in p0


@case
def test_driver_stop_button_is_confirmer_l_arret_not_terminer_l_arret():
    """Galass_Driver my_strings.confirmStop = « Confirmer l'arrêt » ; aucun « Terminer l'arrêt »."""
    t = run_text()
    ui = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.sh")).read()
    assert '"Confirmer l\'arrêt")' in ui[ui.index("driver_stop_confirm_ui() {"):], "tap du bouton réel (parent cliquable)"
    assert 'driver_stop_confirm_ui "$DRIVER_PKG" "$n"' in t
    assert "Terminer l'arrêt" not in t.replace("Terminer l'arrêt » n'existe", "").replace("« Terminer l'arrêt »", ""), \
        "plus de tap sur un bouton « Terminer l'arrêt » inexistant"
    assert "wait_ui \"$DRIVER_PKG\" 45 '+Confirmer l.arrêt'" in t, "attente du bouton avant le tap"


DRIVER_ACTIVE = _xml("Arrêt 1", "E2E Stop 1 Médina", "Arrêt 2", "E2E Stop 2 Fann",
                     "0/2 arrêt terminé", "Prise en charge du client", "Annuler la course")
DRIVER_RUNNING = _xml("Arrêt 1", "E2E Stop 1 Médina", "0/2 arrêt terminé",
                      "Confirmer l'arrêt", "Terminer la course")
DRIVER_HOME = _xml("En ligne", "Accueil", "Activité", "Courses")


@case
def test_driver_running_stop1_requires_the_stop_button_not_the_active_screen():
    spec = markers()["driver_running_stop1"]
    assert spec_ok(spec, DRIVER_RUNNING) == 0, spec
    # L'écran ACTIVE réel (run 44192999654) montre déjà « Arrêt 1 » et le compteur.
    assert spec_ok(spec, DRIVER_ACTIVE) != 0, spec
    assert "Terminer l.arrêt" not in spec, spec


@case
def test_driver_inride_marker_rejects_active_screen():
    spec = markers()["driver_inride_stops"]
    assert spec_ok(spec, DRIVER_RUNNING) == 0, spec
    assert spec_ok(spec, DRIVER_ACTIVE) != 0, "« Arrêt 1 » seul ne prouve pas la course démarrée"


@case
def test_driver_stop_done_markers_use_arret_n_not_counter():
    """Papa 27/11 : plus de x/N — frise Arrêt N + Terminer (F5-14)."""
    m = markers()
    assert spec_ok(m["driver_stop1_done"], DRIVER_STOP1_DONE) == 0
    assert spec_ok(m["driver_stop2_done"], DRIVER_STOP2_DONE) == 0
    # Anciens compteurs seuls : FAIL (négatifs -1/2|-2/2 dans le spec)
    assert spec_ok(m["driver_stop1_done"], _xml("1/2 arrêt terminé")) != 0
    assert spec_ok(m["driver_stop2_done"], _xml("2/2 arrêts terminés")) != 0
    assert "+1/2" not in m["driver_stop1_done"] and "+2/2" not in m["driver_stop2_done"]
    assert "Arrêt 1" in m["driver_stop1_done"] and "Arrêt 2" in m["driver_stop2_done"]
    # Papa 28/11 : Destination n'est plus exigée (textes visibles Arrêt N / Terminer).
    assert "Destination" not in m["driver_stop2_done"]
    assert "galass.driver" not in m["driver_stop1_done"]
    assert "galass.driver" not in m["driver_stop2_done"]


@case
def test_driver_summary_is_the_home_screen_the_app_really_shows():
    spec = markers()["driver_summary"]
    assert spec_ok(spec, DRIVER_HOME) == 0, spec
    assert spec_ok(spec, DRIVER_RUNNING) != 0, spec
    assert spec_ok(spec, _xml("En ligne", "Reçois le paiement")) != 0, spec
    assert "Gains" not in spec and "Résumé" not in spec, "l'app n'a pas d'écran de résumé"


@case
def test_l1_driver_stop_done_markers_require_arret_n_frise():
    m = markers()
    assert spec_ok(m["driver_stop1_done"], DRIVER_STOP1_DONE) == 0, m["driver_stop1_done"]
    assert spec_ok(m["driver_stop2_done"], DRIVER_STOP2_DONE) == 0, m["driver_stop2_done"]
    # « Arrêt 2 » seul / écran fin : faux PASS
    assert spec_ok(m["driver_stop1_done"], _xml("Arrêt 2", "Terminer la course")) != 0, \
        m["driver_stop1_done"]
    assert spec_ok(m["driver_stop2_done"], _xml("Arrivée", "Vous êtes arrivé", "Terminer la course")) != 0, \
        m["driver_stop2_done"]
    for name in ("driver_stop1_done", "driver_stop2_done"):
        assert spec_ok(m[name], DRIVER_OFFER) != 0, (name, m[name])


@case
def test_l1_driver_offer_accepts_home_navbar_under_the_toast():
    spec = markers()["driver_offer_with_stops"]
    assert spec_ok(spec, DRIVER_OFFER) == 0, spec
    # The four labels are ALTERNATIVES: an offer without the instruction block is still
    # an offer, and « Accueil » is expected under the toast (dashboard stays visible).
    assert spec_ok(spec, _xml("Accueil", "Accepter la course")) == 0, spec
    assert spec_ok(spec, _xml("Accueil")) != 0, spec


@case
def test_l1_composition_is_unconditional_after_api_reset():
    text = run_text()
    reset = text.index("api reset")
    pick = text.index("api pick >>", reset)
    window = text[reset:pick]
    assert "compose_ride_ui" in window, window
    assert "has_open" not in window, "la composition reste conditionnée à api has_open"
    assert "artemis_run rider-" not in text, "les étapes Artemis rider-* sont encore sur le chemin"
    for name in ("artemis-rider-destination", "artemis-rider-stop1", "artemis-rider-stop2"):
        assert re.search(rf'mark {name} SKIP "[^"]+"', text), name
    assert "ui_compose" in text, "ui_compose absent"


@case
def test_l1_compose_emits_the_picker_and_summary_captures():
    body = compose_body()
    for name in ("15-rider-destination-selected", "16-rider-stop-picker",
                 "17-rider-multistop-summary"):
        assert name in body, (name, body)
    # 16 is taken right after the tap that opens the picker, before any typing.
    assert body.index('"Ajouter un arrêt"') < body.index("16-rider-stop-picker"), body
    # 17 is taken once both stops are set, before the request is sent.
    assert body.index("17-rider-multistop-summary") < body.index('"Trouver un chauffeur"'), body


@case
def test_l1_ui_compose_failure_cannot_pass_ride_composed():
    text = run_text()
    block = re.search(r'if \[ "\$\{ui_compose:-0\}" != "1" \].*?\nfi', text, re.S)
    assert block, "garde ui_compose introuvable avant ride-composed"
    assert 'mark ride-composed FAIL "api fallback: compose"' in block.group(0), block.group(0)


@case
def test_l1_driver_home_online_is_a_hard_assertion():
    text = run_text()
    idx = text.index("33-driver-home-online")
    window = text[idx:idx + 400]
    assert "mark driver-home-online FAIL" in window, window
    assert "mark driver-home-online WARN" not in window, window


@case
def test_l1_back_press_before_driver_summary_capture():
    text = run_text()
    i42 = text.index("42-driver-cash-confirmed")
    i43 = text.index("43-driver-summary")
    assert i42 < i43, (i42, i43)
    window = text[i42:i43]
    assert "adb shell input keyevent 4" in window, window


@case
def test_tap_exact_collapses_whitespace_and_taps_node_center():
    tmp = tempfile.mkdtemp(prefix="e2e-tap-")
    xml = os.path.join(tmp, "w.xml")
    with open(xml, "w", encoding="utf-8") as fh:
        fh.write(FIXTURE)
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir)
    adb_log = os.path.join(tmp, "adb.log")
    stub = os.path.join(bin_dir, "adb")
    with open(stub, "w") as fh:
        fh.write("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$ADB_LOG\"\n")
    os.chmod(stub, 0o755)
    env = dict(os.environ, OUT=tmp, UI_XML=xml, UI_XML_SRC=xml, E2E_ALLOW_FIXTURE="1",
               SMOKE_LOG=os.path.join(tmp, "smoke.log"),
               PATH=bin_dir + os.pathsep + os.environ["PATH"], ADB_LOG=adb_log)
    env.pop("GITHUB_ACTIONS", None)
    proc = subprocess.run(["bash", "-c", '. "$1"; tap_exact "$2"', "_", UI, "Accepter la course"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    taps = [l for l in open(adb_log).read().splitlines() if "input tap" in l]
    assert taps, "aucun tap émis"
    assert taps[-1].strip() == "shell input tap 350 900", taps


@case
def test_ui_action_uses_whitespace_tolerant_regex():
    body = ui_text()
    fn = re.search(r"^ui_action\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert r"\\s+" in fn or "\\s+" in fn, fn
    assert "tap_exact" in fn, fn


@case
def test_wait_ui_never_relaunches():
    body = ui_text()
    fn = re.search(r"^wait_ui\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert "ensure_fg" not in fn, fn
    assert "return 3" in fn, fn


@case
def test_ensure_fg_never_relaunches_on_unknown():
    body = ui_text()
    fn = re.search(r"^ensure_fg\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert "UNKNOWN" in fn, fn
    assert "fg_pkg" in fn, fn


@case
def test_ui_xml_freshness_contract():
    body = ui_text()
    fn = re.search(r"^ui_xml\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert "rm -f /sdcard/w.xml" in fn, fn
    assert "uiautomator dump --compressed /sdcard/w.xml" in fn, fn
    assert "dumped to" in fn, fn
    assert "<hierarchy" in fn, fn
    assert "400" in fn, fn
    assert "for try in 1 2 3" in fn, fn
    assert "rm -f /tmp/w.xml" in fn, fn


@case
def test_cap_uses_mark_table_via_local_n():
    body = run_text()
    fn = re.search(r"^cap\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert "local -n" in fn, fn
    assert "MARK_" in fn, fn


@case
def test_report_py_replaces_inline_report_block():
    body = run_text()
    assert "e2e/report.py" in body, "report.py n'est pas appelé par run.sh"
    assert "PASS_WITH_WARNINGS" not in body, "l'ancien bloc report inline est encore là"


@case
def test_notif_probe_is_token_attributed():
    body = run_text()
    assert "e2e/notif.py" in body, "notif.py n'est pas appelé par run.sh"
    assert "unattributed" in body, "le cas delta sans token n'est pas géré"



# ------------------------------------------------------- behavioural harness
def stub_env(fixture=FIXTURE, adb_body="", extra=None):
    """tmp dir + fixture XML + a stub `adb` that only logs its argv."""
    tmp = tempfile.mkdtemp(prefix="e2e-ui-")
    xml = os.path.join(tmp, "w.xml")
    with open(xml, "w", encoding="utf-8") as fh:
        fh.write(fixture)
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir)
    adb_log = os.path.join(tmp, "adb.log")
    stub = os.path.join(bin_dir, "adb")
    with open(stub, "w") as fh:
        fh.write('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$ADB_LOG"\n' + adb_body)
    os.chmod(stub, 0o755)
    env = dict(os.environ, OUT=tmp, UI_XML=xml, UI_XML_SRC=xml, E2E_ALLOW_FIXTURE="1",
               SMOKE_LOG=os.path.join(tmp, "smoke.log"),
               PATH=bin_dir + os.pathsep + os.environ["PATH"], ADB_LOG=adb_log)
    env.pop("GITHUB_ACTIONS", None)
    env.update(extra or {})
    return tmp, adb_log, env


def run_ui(script, env):
    return subprocess.run(["bash", "-c", script, "_", UI],
                          capture_output=True, text=True, env=env)


CONFIRM_FIXTURE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="1" text="Confirmer l'arrêt" content-desc="" visible-to-user="true" bounds="[50,1800][900,1900]" />
  </node>
</hierarchy>
"""


@case
def test_ui_action_returns_failure_when_confirm_dialog_absent():
    """B3: a tap whose confirmation dialog never opened is not a successful action."""
    tmp, _log, env = stub_env(CONFIRM_FIXTURE, extra={"UI_CONFIRM_TIMEOUT": "1"})
    proc = run_ui('. "$1"; ui_action sn.galass.galassdriver "Confirmer l\'arrêt"', env)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    log = open(os.path.join(tmp, "run.log"), encoding="utf-8").read()
    assert "pas de dialogue" in log, log
    # and the exact trigger label must not satisfy the dialog probe
    assert "+^Confirmer$" in ui_text(), "le dialogue doit être sondé exactement"


@case
def test_snap_refuses_empty_screencap():
    """B4: a MATCH capture with no PNG (or <8 bytes) is not a PASS."""
    tmp, _log, env = stub_env()
    proc = run_ui('. "$1"; snap cap-empty sn.galass.galassrider 5 "+Où allez-vous" "" '
                  '"rider-home-loaded" "grounded"', env)
    assert proc.returncode == 5, (proc.returncode, proc.stdout, proc.stderr)
    side = json.load(open(os.path.join(tmp, "screens", "cap-empty.json")))
    assert side["status"] == "CAPTURE_MISS", side
    assert side["png_sha256"] == "", side


@case
def test_snap_files_capture_under_explicit_point():
    """S4: the point is passed to snap, never inherited from a previous capture."""
    tmp, _log, env = stub_env()
    run_ui('. "$1"; snap cap-one sn.galass.galassrider 5 "+Où allez-vous" "" '
           '"rider-home-loaded" "grounded"', env)
    run_ui('. "$1"; snap cap-two sn.galass.galassrider 5 "+Où allez-vous" "" '
           '"rider-stop1-active" "grounded"', env)
    one = json.load(open(os.path.join(tmp, "screens", "cap-one.json")))
    two = json.load(open(os.path.join(tmp, "screens", "cap-two.json")))
    assert one["point"] == "rider-home-loaded", one
    assert two["point"] == "rider-stop1-active", two


@case
def test_fixture_requires_opt_in():
    """S12: UI_XML_SRC is a local convenience, never a CI backdoor."""
    _tmp, _log, env = stub_env()
    env.pop("E2E_ALLOW_FIXTURE", None)
    proc = run_ui('. "$1"; ui_xml', env)
    assert proc.returncode == 4, (proc.returncode, proc.stdout, proc.stderr)
    env["E2E_ALLOW_FIXTURE"] = "1"
    env["GITHUB_ACTIONS"] = "true"
    proc = run_ui('. "$1"; ui_xml', env)
    assert proc.returncode == 4, (proc.returncode, proc.stdout, proc.stderr)


# ------------------------------------------------------------- B6 model id
@case
def test_vision_model_injection_rejected():
    tmp = tempfile.mkdtemp(prefix="e2e-model-")
    cfg = os.path.join(tmp, "artemis.jsonc")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write('{\n  "model": "old-model",\n  "other": 1\n}\n')
    bad = 'x"; rm -rf / #'
    proc = subprocess.run([sys.executable, SET_MODEL, cfg, bad],
                          capture_output=True, text=True)
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "old-model" in open(cfg, encoding="utf-8").read()
    proc = subprocess.run([sys.executable, SET_MODEL, cfg, "gemini-2.5-flash"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    text = open(cfg, encoding="utf-8").read()
    assert '"model": "gemini-2.5-flash"' in text, text
    assert '"other": 1' in text, text
    body = run_text()
    assert "e2e/set_model.py" in body, "run.sh n'utilise pas le réécrivain argv"
    assert re.search(r"grep -F .*VISION_MODEL|grep -c -F", body), body
    assert not re.search(r"sed -i[^\n]*\$VISION_MODEL", body), body


# --------------------------------------------------------------- B9 logcat
@case
def test_log_scan_requires_evidence():
    tmp = tempfile.mkdtemp(prefix="e2e-log-")
    empty = os.path.join(tmp, "empty.log")
    open(empty, "w").close()
    proc = subprocess.run(["bash", "-c", '. "$1"; log_scan "$2" "staging.example" 200',
                           "_", LOGCHECK, empty], capture_output=True, text=True)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "no-evidence" in proc.stdout, proc.stdout

    full = os.path.join(tmp, "full.log")
    with open(full, "w", encoding="utf-8") as fh:
        fh.write("D/Ok( 1): " + "x" * 500 + "\n")
    proc = subprocess.run(["bash", "-c", '. "$1"; log_scan "$2" "staging.example" 200',
                           "_", LOGCHECK, full], capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)

    prod = os.path.join(tmp, "prod.log")
    with open(prod, "w", encoding="utf-8") as fh:
        fh.write("D/Ok( 1): GET https://galassapp.com/api/ride " + "x" * 300 + "\n")
    proc = subprocess.run(["bash", "-c", '. "$1"; log_scan "$2" "staging.example" 200',
                           "_", LOGCHECK, prod], capture_output=True, text=True)
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert "prod_hits=1" in proc.stdout, proc.stdout

    missing = os.path.join(tmp, "nope.log")
    proc = subprocess.run(["bash", "-c", '. "$1"; log_scan "$2" "staging.example" 200',
                           "_", LOGCHECK, missing], capture_output=True, text=True)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "missing-file" in proc.stdout, proc.stdout


@case
def test_logcat_is_streamed_and_grepped_safely():
    body = run_text()
    assert "logcat/stream.log" in body, "logcat non streamé depuis le boot"
    assert "grep -F" in body or "grep -a -F" in body, body
    assert "log_scan" in body, "le scan logcat n'utilise pas logcheck.sh"
    assert "rc" in body and "2" in body, body


# ------------------------------------------------------- S6/S7 early paths
@case
def test_out_dir_is_cleaned_at_start():
    body = run_text()
    assert re.search(r'rm -rf "\$OUT"/screens', body), body
    assert re.search(r'rm -rf [^\n]*state\.json', body), body


@case
def test_early_exit_flushes_results():
    body = run_text()
    assert "flush_results" in body, body
    fn = re.search(r"^flush_results\(\) \{(.*?)^\}", body, re.M | re.S)
    assert fn and '"${RESULTS[@]}"' in fn.group(1), fn
    boot = re.search(r"emulator-boot FAIL(.*?)\nfi", body, re.S)
    assert boot and "flush_results" in boot.group(1), boot


# ------------------------------------------------------------ S8/S9/S10/S11
@case
def test_snap_refetches_dump_after_screencap():
    fn = re.search(r"^snap\(\) \{(.*?)^\}", ui_text(), re.M | re.S).group(1)
    assert "first_hash" in fn, fn
    assert fn.count("ui_xml") >= 1, fn
    assert "ACTION_MISS" in fn, fn
    assert "screencap" in fn, fn


@case
def test_b7_probe_runs_after_accept():
    body = run_text()
    assert body.index("bug-b7-push") > body.index('tap_exact "Accepter la course"'), "sonde B7 avant acceptation"


@case
def test_b4_decline_count_excludes_retry_window():
    body = run_text()
    assert "declines_pre" in body, body
    window = re.search(r"bug-b4-decline-409.{0,400}", body, re.S)
    assert window and "non concluant" in window.group(0), window


@case
def test_home_after_proves_no_relaunch():
    body = run_text()
    assert "rider-final-itinerary|PASS" in body, body
    assert "rider_leave_rating" in body, body
    seg = body[body.index("cap rider-rating 30-rider-rating"):body.index("rider-home-after FAIL \"accueil Rider jamais observé")]
    assert "keyevent 4" not in seg, "Back aveugle avant home-after (quitte l'app sur l'accueil)"


# ---------------------------------------------------- S2/S14/S15/S18/S21
@case
def test_absence_claims_require_evidence():
    body = run_text()
    for point in ("bug-b4-decline-409", "bug-b5-estimated-time", "maps-auth"):
        idx = body.index(point)
        window = body[max(0, idx - 700): idx + 500]
        assert "no evidence" in window, (point, window)
        assert "[ -s " in window, (point, window)


@case
def test_ride_identifier_is_uid_not_token():
    body = run_text()
    assert "token=" not in body, "identifiant de course encore exposé en token="
    assert "uid=$RIDE_UID" in body or "uid=${RIDE_UID}" in body, body


@case
def test_b1_and_b3_statuses_emitted():
    body = run_text()
    assert "bug-b1-" in body, "B1 sans statut"
    assert "bug-b3-" in body, "B3 sans statut"


@case
def test_write_prefs_pipes_xml_without_temp_file():
    body = run_text()
    assert "/tmp/prefs-" not in body, body
    assert "FlutterSharedPreferences.xml" in body, body


@case
def test_log_is_file_only_and_mark_echoes_point():
    body = run_text()
    log_fn = re.search(r"^log\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert "tee" not in log_fn, log_fn
    assert ">&2" not in log_fn, log_fn
    mark_fn = re.search(r"^mark\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert "printf '%s|%s\\n'" in mark_fn, mark_fn


# ---------------------------------------------------------- S17/S20/S22/S23
@case
def test_gitignore_covers_evidence_artifacts():
    body = open(os.path.join(ROOT, "..", ".gitignore"), encoding="utf-8").read()
    for pat in ("ev.tgz", "*.tgz", "*.gpg", "evidence/"):
        assert pat in body, pat


@case
def test_api_flow_uses_subprocess_for_stop_hook():
    body = open(os.path.join(ROOT, "api_flow.py"), encoding="utf-8").read()
    assert "os.system(" not in body, body
    assert "subprocess.run([hook" in body, body


@case
def test_workflow_hardens_checkouts_and_runs_tests():
    body = open(WORKFLOW, encoding="utf-8").read()
    assert body.count("persist-credentials: false") >= 2, body
    assert "bash -n e2e/*.sh" in body, body
    assert "for t in e2e/tests/test_*.py" in body, body


@case
def test_every_harness_test_runs_in_ci():
    body = open(WORKFLOW, encoding="utf-8").read()
    tests = sorted(os.path.basename(p) for p in
                   glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    assert tests, "aucune spec dans e2e/tests"
    loop = "for t in e2e/tests/test_*.py" in body
    for name in tests:
        assert loop or name in body, f"{name} n'est exécuté par aucun step du workflow"


@case
def test_deadlines_are_ordered_watchdog_step_job():
    wf = open(WORKFLOW, encoding="utf-8").read()
    m = re.search(r"- name: E2E multistop \([^)]+\)\n\s+timeout-minutes: (\d+)", wf)
    assert m, "le step E2E doit porter son propre timeout-minutes"
    step = int(m.group(1))
    job = int(re.search(r"^  e2e:\n(?:.*\n)*?    timeout-minutes: (\d+)", wf, re.M).group(1))
    env_deadline = int(re.search(r'RUN_DEADLINE_MIN: "(\d+)"', wf).group(1))
    default = int(re.search(r'RUN_DEADLINE_MIN="\$\{RUN_DEADLINE_MIN:-(\d+)\}', run_text()).group(1))
    assert env_deadline == default, (env_deadline, default)
    assert env_deadline < step < job, (env_deadline, step, job)


@case
def test_find_is_narrowed():
    body = run_text()
    assert 'find "$HOME/.artemis" vendor/artemis' in body, body
    assert 'find "$HOME" vendor/artemis' not in body, body


@case
def test_adb_calls_are_timeout_wrapped():
    body = ui_text()
    fn = re.search(r"^adb\(\) \{(.*?)^\}", body, re.M | re.S)
    assert fn, "aucun wrapper adb"
    assert "timeout" in fn.group(1) and "ADB_TMO" in fn.group(1), fn.group(1)
    assert "command adb logcat" in run_text(), "le stream logcat doit échapper au timeout"


@case
def test_api_flow_rejects_non_https_and_wrong_base():
    body = open(os.path.join(ROOT, "api_flow.py"), encoding="utf-8").read()
    assert "check_base(BASE)" in body, body
    sys.path.insert(0, ROOT)
    from base_guard import check_base
    assert check_base("http://example.com") is not None
    assert check_base("http://192.162.69.113") is not None
    assert check_base("https://staging.galassapp.com") is not None
    assert check_base("http://192.162.69.113:8080") is None
    assert "GALASS_STAGING_URL" in body, body
    assert "sys.exit(" in body, body


@case
def test_b8_api_composed_ride_cannot_pass():
    """B8: when the harness composed the ride, no UI claim may close on it."""
    body = run_text()
    assert 'mark ride-composed FAIL "api fallback: compose"' in body, body
    assert 'RIDE_SOURCE' in body, body
    assert 'ride_source=$RIDE_SOURCE' in body, body
    flow = open(os.path.join(ROOT, "api_flow.py"), encoding="utf-8").read()
    assert 'st["ride_source"] = "ui"' in flow, flow
    assert 'st["ride_source"] = "api"' in flow, flow


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
    print(f"\n{len(CASES) - failed}/{len(CASES)} marker cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
