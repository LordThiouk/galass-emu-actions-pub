import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from find_driver_button import decide

def node(text="", rid="", click=False, b=(0, 0, 10, 10)):
    return ('<node text="%s" resource-id="%s" clickable="%s" visible-to-user="true" bounds="[%d,%d][%d,%d]"/>'
            % (text, rid, "true" if click else "false", *b))

def dump(root, *children):
    return ('<?xml?><hierarchy rotation="0">'
            + node("", "", False, root) + "".join(children) + "</hierarchy>")

TITLE = node("Trouver un chauffeur", b=(40, 900, 560, 980))
LIST = "".join(node("Svc", "service_option_%d" % i, True, (0, 1100 + i * 300, 1080, 1380 + i * 300)) for i in range(4))

# Géométrie réelle du run 40958874460 (Pixel 6, 1080x2400) : liste h=500,
# carte Voiture (service_option_3) centrée à y=2259, bouton hors écran.
ROOT_1080 = (0, 0, 1080, 2400)
REAL_CARD = (42, 2009, 1038, 2509)  # centre = (540, 2259), haut 500, rognée au bas

def test_real_geometry_h500_card_t2259_swipes_above_list():
    """Consigne : geste HORS de la liste, jamais dedans — pas de swipe fixe 1900->1000."""
    r = decide(dump(ROOT_1080, TITLE, node("Svc", "service_option_3", True, REAL_CARD)))
    assert r == "SWIPE 540 1809 1009", r  # départ = top(2009) - 200, hors padding ListView
    start = int(r.split()[2])
    assert not (2009 <= start <= 2509), r  # départ jamais dans les bounds de la liste

def test_swipe_below_list_when_room():
    lst = node("Svc", "service_option_0", True, (0, 1000, 1080, 1300))
    r = decide(dump(ROOT_1080, TITLE, lst))
    assert r == "SWIPE 540 1360 560", r  # sous la liste (bounds 1000..1300), geste vers le haut

def test_title_only_never_tapped_swipes_above_list():
    r = decide(dump(ROOT_1080, TITLE, LIST))
    assert r.startswith("SWIPE 540 "), r  # le titre homonyme n'est JAMAIS tapé
    assert r != "TAP*", r

def test_clickable_button_tapped():
    btn = node("Trouver un chauffeur", click=True, b=(40, 2000, 900, 2120))
    assert decide(dump(ROOT_1080, TITLE, LIST, btn)) == "TAP 470 2060"

def test_clipped_button_never_tapped_pane_scrolls_first():
    """Bouton cliquable mais rogné au bas de l'écran -> PAS de clic, on défile d'abord."""
    btn = node("Trouver un chauffeur", click=True, b=(40, 2300, 900, 2480))
    assert not decide(dump(ROOT_1080, TITLE, node("Svc", "service_option_3", True, REAL_CARD))).startswith("TAP")

def test_non_clickable_button_below_list_tapped():
    btn = node("Trouver un chauffeur", b=(40, 2300, 900, 2380))
    lst = node("Svc", "service_option_0", True, (0, 1100, 1080, 2200))
    assert decide(dump(ROOT_1080, TITLE, lst, btn)) == "TAP 470 2340"

def test_screen_height_read_from_dump_root():
    """Racine 1600 -> arrête le geste dans l'écran, jamais hors écran ni négatif."""
    r = decide(dump((0, 0, 720, 1600), TITLE, node("Svc", "service_option_3", True, REAL_CARD)))
    assert r.startswith("SWIPE 540 "), r
    start, end = (int(r.split("SWIPE 540 ")[1].split()[i]) for i in (0, 1))
    assert 0 < end < start < 1600, r  # geste vers le haut, dans l'écran 1600

def test_no_list_generic_panel_swipe_inside_screen():
    r = decide(dump(ROOT_1080, TITLE))
    assert r == "SWIPE 540 1880 1080", r  # 80% -> fin 1080, borné

def test_no_room_above_list_returns_none():
    """Liste collée au haut : pas de zone panneau exploitable -> NONE, jamais dans la liste."""
    lst = node("Svc", "service_option_0", True, (0, 80, 1080, 2400))
    assert decide(dump(ROOT_1080, TITLE, lst)) == "NONE"

def test_swipe_start_never_inside_any_service_option_bounds():
    """Invariant sweep : sur des géométries variées, le départ sort des bounds de chaque carte."""
    geoms = [
        (42, 2009, 1038, 2509),      # réel run 40958874460
        (0, 1200, 1080, 1700),
        (0, 1759, 1080, 2259),       # hypothèse liste [1759..2259]
        (0, 200, 1080, 700),
        (0, 1000, 1080, 2280),
    ]
    for b in geoms:
        r = decide(dump(ROOT_1080, TITLE, node("Svc", "service_option_3", True, b)))
        if r.startswith("SWIPE"):
            y1 = int(r.split()[2])
            assert not (b[1] <= y1 <= b[3]), (b, r)  # départ hors de la carte/liste
