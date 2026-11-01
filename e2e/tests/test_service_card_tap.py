import os, sys
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from service_card_tap import decide

def node(text="", rid="", click=False, b=(0, 0, 10, 10)):
    return ('<node text="%s" resource-id="%s" clickable="%s" visible-to-user="true" bounds="[%d,%d][%d,%d]"/>'
            % (text, rid, "true" if click else "false", *b))

def dump(root, *children):
    return ('<?xml?><hierarchy rotation="0">'
            + node("", "", False, root) + "".join(children) + "</hierarchy>")

ROOT_1080 = (0, 0, 1080, 2400)
NAV_HOME = node("Accueil", click=True, b=(81, 2182, 378, 2277))
NAV_ACT = node("Activité", click=True, b=(392, 2182, 689, 2277))
NAV_MENU = node("Menu", click=True, b=(703, 2182, 1000, 2277))

def test_card_clear_of_nav_tapped_at_center():
    """Géométrie du run 40965062244 : carte à y 1942-2181, pareil : tap centre."""
    xml = dump(ROOT_1080, NAV_HOME, NAV_ACT, NAV_MENU,
               node("Voiture", "service_option_3", True, (84, 1942, 996, 2181)))
    assert decide(xml, "service_option_3") == "TAP 540 2050", _r(xml)

def _r(xml):
    return decide(xml, "service_option_3")

def test_nav_band_collision_never_taps_a_tab():
    """Run 41112460972 RÉEL : carte 2181-2337, onglets 2182-2277. Le tap
    centre (540,2259) tombait sur Activité (x 392-689) et Changeait d'onglet."""
    xml = dump(ROOT_1080, NAV_HOME, NAV_ACT, NAV_MENU,
               node("Voiture", "service_option_3", True, (84, 2181, 996, 2337)))
    r = decide(xml, "service_option_3")
    assert r.startswith("SCROLL"), r  # intersection < 90px -> on remonte, JAMAIS de TAP
    x, y1, y2 = (int(v) for v in r.split()[1:])
    assert y1 <= 2182 - 24, r        # départ du geste au-dessus des onglets
    assert 84 <= x <= 996, r         # colonne de la carte (pas les onglets)
    assert 0 < y2 < y1, r            # geste vertical vers le haut
    assert not (81 <= x <= 1000 and y1 >= 2182), r

def test_scroll_result_makes_card_tappable_next_try():
    """Après le SCROLL (carte remontée ~600px), le tap tombe HORS des onglets."""
    xml = dump(ROOT_1080, NAV_HOME, NAV_ACT, NAV_MENU,
               node("Voiture", "service_option_3", True, (84, 1581, 996, 1737)))
    r = decide(xml, "service_option_3")
    assert r.startswith("TAP"), r
    x, y = (int(v) for v in r.split()[1:])
    assert y <= 2182 - 24, r
    assert 84 <= x <= 996, r

def test_no_nav_bar_taps_center():
    xml = dump(ROOT_1080, node("Voiture", "service_option_1", True, (84, 700, 996, 980)))
    assert decide(xml, "service_option_1") == "TAP 540 840"

def test_missing_card_returns_none():
    xml = dump(ROOT_1080, NAV_HOME)
    assert decide(xml, "service_option_3") == "NONE"

def test_card_mostly_under_nav_but_90px_free_taps_clamped():
    """Intersection exactement à la limite : tap s'il reste >= 90px nets."""
    xml = dump(ROOT_1080, NAV_HOME, NAV_ACT, NAV_MENU,
               node("Voiture", "service_option_3", True, (84, 2010, 996, 2337)))
    r = decide(xml, "service_option_3")
    assert r.startswith("TAP"), r
    y = int(r.split()[2])
    assert 1990 < y < 2158, r   # centre [2010..2158], jamais dans les onglets

def test_screen_height_read_from_root():
    root = (0, 0, 720, 1600)
    nav = node("Accueil", click=True, b=(81, 1400, 240, 1500))
    xml = dump(root, nav, node("V", "service_option_0", True, (80, 1350, 640, 1600)))
    r = decide(xml, "service_option_0")
    assert not r.startswith("TAP") or int(r.split()[2]) < 1376, r

def test_scroll_start_vertically_inside_card_column_when_possible():
    """Carte presque ENTIEREMENT sous les onglets : remontée obligatoire,
    départ du geste au-dessus des onglets (jamais dans la bande 2182+)."""
    xml = dump(ROOT_1080, NAV_HOME, NAV_ACT, NAV_MENU,
               node("V", "service_option_2", True, (84, 2160, 996, 2337)))
    r = decide(xml, "service_option_2")
    assert r.startswith("SCROLL"), r
    x, y1, y2 = (int(v) for v in r.split()[1:])
    assert y1 <= 2182 - 24, r   # départ au-dessus des onglets
    assert 84 <= x <= 996, r    # colonne de la carte

def main():
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   " + name)
            except Exception:
                failed += 1
                print("FAIL " + name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

