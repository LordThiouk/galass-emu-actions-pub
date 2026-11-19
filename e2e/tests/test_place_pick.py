#!/usr/bin/env python3
"""Composition UI : de vrais lieux de Dakar (Google Places ne connaît pas « E2E Dest »).

Run 44526136027 : 5 requêtes autocomplete en HTTP 200 mais aucune suggestion pour « E2E Dest »
(faux nom) -> ride-composed en repli API. On tape maintenant « Point E », « Medina », « Fann »
et on tape la première suggestion dont le texte COMMENCE par le lieu (description Google
« Point E, Dakar, Senegal »), sans accent ni casse, jamais le champ de saisie lui-même.
"""
import os
import re
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui.sh")
RUN = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()


def node(text, bounds, cls="android.widget.TextView"):
    return f'<node class="{cls}" text="{text}" content-desc="" visible-to-user="true" bounds="{bounds}"/>'


def tap(nodes, prefix, timeout=4):
    d = tempfile.mkdtemp()
    xml = os.path.join(d, "w.xml")
    open(xml, "w", encoding="utf-8").write("<hierarchy>" + "".join(nodes) + "</hierarchy>")
    script = (
        f'. "{UI}"\nUI_XML_PATH="{xml}"\n'
        'ui_xml() { UI_DUMP_VALID=1; return 0; }\nxml_pkg() { echo sn.galass.galassrider; }\n'
        'sleep() { SECONDS=$((SECONDS+2)); }\n_ulog() { :; }\n'
        'adb() { echo "adb $*" >> "$TAPLOG"; }\n'
        f'tap_prefix sn.galass.galassrider "{prefix}" {timeout}; echo "rc=$?"'
    )
    env = dict(os.environ, TAPLOG=os.path.join(d, "taps.txt"))
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
    taps = open(env["TAPLOG"]).read() if os.path.exists(env["TAPLOG"]) else ""
    return out.strip(), taps


def test_taps_first_suggestion_starting_with_place():
    out, taps = tap([node("Point E", "[0,100][1000,200]", "android.widget.EditText"),
                     node("Point E, Dakar, Senegal", "[0,300][1000,400]"),
                     node("Point E 2, Dakar", "[0,420][1000,520]")], "Point E,")
    assert "rc=0" in out and "input tap 500 350" in taps, (out, taps)


def test_accent_and_case_insensitive():
    out, taps = tap([node("Médina, Dakar, Senegal", "[0,300][1000,400]")], "medina,")
    assert "rc=0" in out and "input tap 500 350" in taps, (out, taps)


def test_never_taps_the_search_field():
    out, taps = tap([node("Point E,", "[0,100][1000,200]", "android.widget.EditText")], "Point E,")
    assert "rc=1" in out and not taps, (out, taps)


def test_miss_when_no_suggestion():
    out, taps = tap([node("Où allez-vous ?", "[0,100][1000,200]")], "Fann,")
    assert "rc=1" in out and not taps, (out, taps)


def test_compose_uses_real_dakar_places():
    body = re.search(r"^compose_ride_ui\(\) \{.*?^\}", RUN, re.S | re.M).group(0)
    assert "E2E Dest" not in body and "E2E Stop" not in body, body
    assert 'ui_pick_place "Point E"' in body and "Médina" in body and "Fann" in body, body


def test_destination_confirmed_before_adding_stop():
    # Run 44602357716 : après « Point E » l'écran porte encore « Confirmer le lieu » (picker) ;
    # « Ajouter un arrêt » n'existe qu'après cette confirmation.
    body = re.search(r"^compose_ride_ui\(\) \{.*?^\}", RUN, re.S | re.M).group(0)
    cap15 = body.index("15-rider-destination-selected")
    confirm = body.index("Confirmer le lieu", cap15)
    add_stop = body.index("Ajouter un arrêt", cap15)
    assert cap15 < confirm < add_stop, body


def test_pickup_capture_taken_after_tap_on_confirmation_dialog():
    # ISC-F19-6 : la capture 36 prise AVANT le tap était identique à 35 (duplicate-state).
    # Le bouton « Prise en charge du client » existe : on capture la fenêtre de confirmation qu'il ouvre.
    tap = RUN.index('try_tap "$DRIVER_PKG" "Prise en charge du client"')
    cap = RUN.index("cap driver-pickup-arrived 36-driver-pickup-arrived")
    confirm = RUN.index('dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler"')
    assert tap < cap < confirm, (tap, cap, confirm)


def test_each_stop_confirmed_with_real_label():
    # Run 44609821983 : après « Médina » le picker garde « Confirmer le lieu » ; « Confirmer » seul
    # (tap_exact) ne le touche pas -> « Ajouter un arrêt » absent pour le second arrêt.
    body = re.search(r"^compose_ride_ui\(\) \{.*?^\}", RUN, re.S | re.M).group(0)
    loop = body[body.index("for stop in"):]
    assert 'tap_when "$RIDER_PKG" "Confirmer le lieu"' in loop, loop


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
    print(f"{n}/{n} cases passed")
