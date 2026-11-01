#!/usr/bin/env python3
"""Tap SAFE d'une carte service, hors de la barre d'onglets flottante.

Run 41112460972 : la liste d'écran bas rendait service_option_3 centré à
(540, 2259) — DANS la bande Accueil/Activité/Menu (y 2182-2277). Le tap
changeait d'onglet « Activité » et abandonnait la course composée (écran
historique, bouton « Trouver un chauffeur » jamais atteint, repli API).

Entrée : dump uiautomator + resource-id exact de la carte.
Sortie (une ligne) :
  TAP x y          -> point DANS les bounds de la carte ET au-dessus de la
                      barre d'onglets (centre de l'intersection, jamais
                      clamped dans un onglet)
  SCROLL x y1 y2   -> liste remontée, geste dans la colonne de la carte, qui
                      part au-dessus de la barre d'onglets (jamais dans les
                      onglets, jamais hors carte horizontalement)
  NONE             -> carte absente / aucune zone exploitable
"""
import html
import re
import sys

NAV_LABELS = ("Accueil", "Activité", "Menu")
TOP_SAFE = 100          # barre système absorbée
NAV_CLEAR = 24          # marge sous onglets : le tap flotte au-dessus
TAP_MIN_H = 90          # intersection plus basse -> inutile de tenter un tap
SCROLL_DIST = 600
MIN_SCROLL = 200


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
        out.append({"rid": a("resource-id"),
                    "text": re.sub(r"\s+", " ", (a("text") + " " + a("content-desc"))).strip(),
                    "b": (x1, y1, x2, y2)})
    return out


def screen_metrics(xml):
    m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    w, h = (1080, 2400) if not m else (int(m.group(3)), int(m.group(4)))
    if not (200 <= w <= 10000):
        w = 1080
    if not (200 <= h <= 10000):
        h = 2400
    return w, h


def decide(xml, rid):
    nodes = parse(xml)
    w, h = screen_metrics(xml)
    card = next((n["b"] for n in nodes if n["rid"] == rid), None)
    if card is None:
        return "NONE"
    x1, y1, x2, y2 = card
    nav_ys = [n["b"][1] for n in nodes if n["text"] in NAV_LABELS and n["b"][1] >= 0]
    nav_top = min(nav_ys) if nav_ys else None
    top = max(y1, TOP_SAFE)
    bottom = min(y2, h - 5)
    if nav_top is not None:
        bottom = min(bottom, nav_top - NAV_CLEAR)
    if bottom - top >= TAP_MIN_H:
        cx = min(max((x1 + x2) // 2, 60), w - 60)
        return "TAP %d %d" % (cx, (top + bottom) // 2)
    if nav_top is not None and y2 > nav_top - NAV_CLEAR:
        # Liste en bas d'écran : la carte dépasse sous les onglets. On remonte
        # depuis la colonne de la carte, départ au-dessus des onglets,
        # arrivée jamais au-dessus de la zone sûre.
        sy = min(max(y2, 60), nav_top - NAV_CLEAR)
        if sy - (TOP_SAFE + 20) >= MIN_SCROLL:
            ey = max(sy - SCROLL_DIST, TOP_SAFE + 20)
            cx = min(max((x1 + x2) // 2, 60), w - 60)
            return "SCROLL %d %d %d" % (cx, sy, ey)
    return "NONE"


if __name__ == "__main__":
    print(decide(open(sys.argv[1], encoding="utf-8", errors="replace").read(), sys.argv[2]))
