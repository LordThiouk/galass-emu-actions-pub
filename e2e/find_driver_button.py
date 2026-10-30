#!/usr/bin/env python3
"""Bouton « Trouver un chauffeur » sous la liste des services (runs 40958874460, 41048460069).

Entrée : dump uiautomator + numéro d'essai (--attempt N, 0 = premier geste).
Sortie (une ligne) :
  TAP x y            -> noeud du libellé visible, tapé HORS de la barre d'onglets
  SWIPE x y1 y2      -> geste vertical dans le PANNEAU (jamais sur une carte)
  NONE               -> rien d'exploitable

Run 41048460069 (dumps diag-service/diag-destination) : après le tap sur
service_option_3 le panneau se replie, la liste s'arrête à y=1820 et le bouton
passe sous l'écran. Les 6 gestes partaient à y=1880, C'EST-À-DIRE SOUS le
panneau, sur la carte : la carte a défilé 6 fois sans que rien ne bouge
(« SWIPE 540 1880 1080 » identique 6x, puis repli API).

D'où l'escalade par ESSAI — chaque tentative essaie une zone différente du
panneau, jamais la carte :
  0-1 : au-dessus de la liste (entre l'en-tête et la première carte) ;
  2-3 : juste sous la liste, colonne de marge à gauche des cartes ;
  4-5 : haut du panneau (poignée / en-tête) quand le reste n'a rien donné.
Le titre homonyme du formulaire (non cliquable, au-dessus de la liste) n'est
jamais tapé ; si le libellé est visible sous la liste on tape son centre, borné
sous la barre d'onglets Accueil/Activité/Menu. La hauteur d'écran vient du
noeud racine du dump.
"""
import html
import re
import sys

LABEL = "Trouver un chauffeur"
NAV_LABELS = ("Accueil", "Activité", "Menu")
# Marge au-dessus de la liste : le padding d'une ListView Flutter est
# hit-testable et capte le geste — 40px ne suffisaient pas (run 40958874460).
ABOVE_MARGIN = 200
# Bornes de la zone tactile utile (bords d'écran / barre système absorbées).
TOP_SAFE = 120
BOTTOM_SAFE = 160
# Colonne de marge : à gauche des cartes (cards réelles x1=84), jamais dessus.
MARGIN_INSET = 40
DIST = 700
MIN_DIST = 300


def parse(xml):
    out = []
    for t in re.findall(r"<node\b[^>]*>", xml):
        a = lambda n: html.unescape((re.search(r'\b' + n + r'="([^"]*)"', t) or [None, ""])[1])
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', t)
        if not b or a("visible-to-user").lower() == "false":
            continue
        x1, y1, x2, y2 = map(int, b.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        out.append({"rid": a("resource-id"), "text": re.sub(r"\s+", " ", (a("text") + " " + a("content-desc"))).strip(),
                    "clickable": a("clickable").lower() == "true", "b": (x1, y1, x2, y2)})
    return out


def screen_height(xml):
    """Hauteur d'écran du noeud racine (premier bounds du dump), sinon 2400."""
    m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if not m:
        return 2400
    h = int(m.group(4))
    return h if 200 <= h <= 10000 else 2400  # racine absurde -> repli conservateur


def swipe_at(x, y, h, dist=DIST):
    """Geste vers le haut depuis (x, y), borné dans l'écran ; None si pas la place."""
    y = min(y, h - BOTTOM_SAFE)
    dist = min(dist, y - TOP_SAFE - 60)
    if dist < MIN_DIST:
        return None
    return "SWIPE %d %d %d" % (x, y, y - dist)


def tap_point(n, nav_top, h):
    """Centre du libellé, borné SOUS la barre d'onglets et dans l'écran."""
    x1, y1, x2, y2 = n["b"]
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    if cy >= nav_top:
        cy = min(max(y1 + 10, nav_top - 20), h - 5)
    return "TAP %d %d" % (cx, max(cy, y1 + 2))


def decide(xml, screen_h_arg=None, attempt=0):
    nodes = parse(xml)
    h = screen_height(xml)
    if screen_h_arg and 200 <= screen_h_arg <= 10000:
        h = screen_h_arg
    cards = [n["b"] for n in nodes if re.match(r"service_option_\d+$", n["rid"])]
    top = min((b[1] for b in cards), default=None)
    bottom = max((b[3] for b in cards), default=None)
    left = min((b[0] for b in cards), default=84)
    nav_top = min((n["b"][1] for n in nodes if n["text"] in NAV_LABELS), default=h - BOTTOM_SAFE)

    # 1) Le libellé est visible : on tape son centre (jamais un noeud rogné).
    #    Cliquable -> tappable où qu'il soit ; non cliquable -> seulement SOUS la
    #    liste (le titre homonyme du formulaire est au-dessus, jamais tapé).
    hits = [n for n in nodes if n["text"] == LABEL and n["b"][1] >= 0 and n["b"][3] <= h]
    below = [n for n in hits if bottom is not None and n["b"][1] >= bottom]
    good = [n for n in hits if n["clickable"]] or below
    if good:
        n = max(good, key=lambda n: (n["clickable"], n["b"][1]))
        if n["b"][1] < nav_top - 20:  # sinon le tap tomberait dans un onglet
            return tap_point(n, nav_top, h)

    # 2) Aucune liste : geste générique sur le panneau.
    if bottom is None:
        return swipe_at(540, int(h * 0.8) - 40, h) or "NONE"

    margin_x = left - MARGIN_INSET if left >= 60 else None
    r = None
    if attempt <= 1:
        # Au-dessus de la liste : la seule zone qu'on SAIT dans le panneau et
        # hors des cartes (run 41048460069 : sous la liste = la carte).
        y = (top - ABOVE_MARGIN) if top is not None else int(h * 0.6)
        y = min(y, h - 80)
        r = swipe_at(540 if attempt == 0 else (margin_x or 540), y, h)
    if r is None and attempt <= 3:
        # Juste sous la liste, colonne de marge : hors carte horizontalement.
        if margin_x is not None:
            r = swipe_at(margin_x, bottom + 60, h)
    if r is None and attempt >= 2:
        # Haut du panneau (poignée / en-tête) : développe une feuille repliée.
        base = (top - ABOVE_MARGIN) if top is not None else int(h * 0.55)
        r = swipe_at(540, base - 400, h, DIST) or swipe_at(540, int(h * 0.45), h)
    return r or "NONE"


def _args(argv):
    xml_path, screen_h, attempt = argv[1], None, 0
    rest = argv[2:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--attempt", "-a") and i + 1 < len(rest):
            attempt = int(rest[i + 1]); i += 2; continue
        if a.isdigit():
            if screen_h is None:
                screen_h = int(a)
            else:
                attempt = int(a)
            i += 1; continue
        i += 1
    return xml_path, screen_h, attempt


if __name__ == "__main__":
    path, sh, att = _args(sys.argv)
    print(decide(open(path, encoding="utf-8", errors="replace").read(), sh, att))
