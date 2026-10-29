import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from find_driver_button import decide

def node(text="", rid="", click=False, b=(0, 0, 10, 10)):
    return ('<node text="%s" resource-id="%s" clickable="%s" visible-to-user="true" bounds="[%d,%d][%d,%d]"/>'
            % (text, rid, "true" if click else "false", *b))

TITLE = node("Trouver un chauffeur", b=(40, 900, 560, 980))
LIST = "".join(node("Svc", "service_option_%d" % i, True, (0, 1100 + i * 300, 1080, 1380 + i * 300)) for i in range(4))

def test_title_only_never_tapped_swipes_above_list_when_list_reaches_bottom():
    r = decide("<h>" + TITLE + LIST + "</h>")
    assert r.startswith("SWIPE 540 1060 "), r  # au-dessus de la liste (top 1100 - 40), pas dedans

def test_swipe_below_list_when_room():
    lst = node("Svc", "service_option_0", True, (0, 1000, 1080, 1300))
    r = decide("<h>" + TITLE + lst + "</h>")
    assert r == "SWIPE 540 1360 560", r

def test_clickable_button_tapped():
    btn = node("Trouver un chauffeur", click=True, b=(40, 2000, 900, 2120))
    assert decide("<h>" + TITLE + LIST + btn + "</h>") == "TAP 470 2060"

def test_non_clickable_button_below_list_tapped():
    btn = node("Trouver un chauffeur", b=(40, 2300, 900, 2380))
    lst = node("Svc", "service_option_0", True, (0, 1100, 1080, 2200))
    assert decide("<h>" + TITLE + lst + btn + "</h>") == "TAP 470 2340"
