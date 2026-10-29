#!/usr/bin/env python3
"""Bouton « Trouver un chauffeur » sous la liste des services (run 40958874460).

Entrée : dump uiautomator. Sortie (une ligne) :
  TAP x y            -> noeud cliquable du libellé, ENTIÈREMENT à l'écran
  SWIPE x y1 y2      -> geste vers le haut HORS des bounds service_option_*
                        (le panneau défile, jamais la liste : h=500, carte à
                        y=2259 au run 40958874460 — le swipe fixe 1900->1000
                        faisait défiler la liste au lieu du panneau)
  NONE               -> rien d'exploitable
Le titre homonyme du formulaire (non cliquable, au-dessus de la liste) n'est jamais tapé.
La hauteur d'écran vient du noeud racine du dump (l'argv ne supplante que si crédible).
"""
import html, re, sys

LABEL = "Trouver un chauffeur"
# Marge au-dessus de la liste : le padding d'une ListView Flutter est
# hit-testable et capte le geste — 40px ne suffisaient pas (run 40958874460).
ABOVE_MARGIN = 200
# Bornes de la zone tactile utile (bords d'écran / barre système absorbées).
TOP_SAFE = 120
BOTTOM_SAFE = 160


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


def pan(y):
    """Geste vers le haut depuis y : -800px, raccourci près du haut d'écran."""
    dist = min(800, y - TOP_SAFE - 60)
    if dist < 300:
        return None  # pas la place d'un vrai drag -> on ne mime pas un geste
    return "SWIPE 540 %d %d" % (y, y - dist)


def decide(xml, screen_h_arg=None):
    nodes = parse(xml)
    h = screen_height(xml)
    if screen_h_arg and 200 <= screen_h_arg <= 10000:
        h = screen_h_arg
    lst = [n["b"] for n in nodes if re.match(r"service_option_\d+$", n["rid"])]
    top = min((b[1] for b in lst), default=None)
    bottom = max((b[3] for b in lst), default=None)
    hits = [n for n in nodes if n["text"] == LABEL]
    # TAP seulement si le noeud est ENTIÈREMENT visible : un bouton rogné au bas
    # de l'écran se centre hors de sa zone cliquable -> clic raté.
    live = [n for n in hits if n["b"][1] >= 0 and n["b"][3] <= h]
    good = [n for n in live if n["clickable"] or (bottom is not None and n["b"][1] >= bottom)]
    if good:
        n = max(good, key=lambda n: (n["clickable"], n["b"][1]))
        x1, y1, x2, y2 = n["b"]
        return "TAP %d %d" % ((x1 + x2) // 2, (y1 + y2) // 2)
    if bottom is None:
        # Aucune liste visible : geste générique sur le panneau.
        return pan(int(h * 0.8) - 40) or "NONE"
    # Défaut : sous la liste (zone panneau, jamais dans les bounds de la liste).
    if bottom + 60 < h - BOTTOM_SAFE:
        return pan(bottom + 60) or "NONE"
    # Liste qui touche/dépasse le bas d'écran : au-dessus de la liste, margé
    # pour ne pas retomber dans le padding hit-testable de la ListView.
    y = top - ABOVE_MARGIN
    y = min(y, h - 80)  # racine incohérente -> geste reste dans l'écran
    if y < top:
        return pan(y) or "NONE"
    return "NONE"


if __name__ == "__main__":
    print(decide(open(sys.argv[1], encoding="utf-8", errors="replace").read(),
                 int(sys.argv[2]) if len(sys.argv) > 2 else None))
