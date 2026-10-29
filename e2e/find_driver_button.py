#!/usr/bin/env python3
"""Bouton « Trouver un chauffeur » sous la liste des services (run 40958874460).

Entrée : dump uiautomator. Sortie (une ligne) :
  TAP x y            -> noeud cliquable du libellé, ou noeud situé SOUS la liste
  SWIPE x y1 y2      -> geste vers le haut HORS de la liste service_option_* (le panneau défile)
  NONE               -> rien d'exploitable
Le titre homonyme du formulaire (non cliquable, au-dessus de la liste) n'est jamais tapé.
"""
import html, re, sys

LABEL = "Trouver un chauffeur"


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


def decide(xml, screen_h=2400):
    nodes = parse(xml)
    lst = [n["b"] for n in nodes if re.match(r"service_option_\d+$", n["rid"])]
    top = min((b[1] for b in lst), default=None)
    bottom = max((b[3] for b in lst), default=None)
    hits = [n for n in nodes if n["text"] == LABEL]
    good = [n for n in hits if n["clickable"] or (bottom is not None and n["b"][1] >= bottom)]
    if good:
        n = max(good, key=lambda n: (n["clickable"], n["b"][1]))
        x1, y1, x2, y2 = n["b"]
        return "TAP %d %d" % ((x1 + x2) // 2, (y1 + y2) // 2)
    if bottom is None:
        return "SWIPE 540 1800 1000"
    # Geste hors liste : sous la liste s'il reste de la place, sinon au-dessus (titre du panneau).
    if bottom + 80 < screen_h - 120:
        y = bottom + 60
    elif top is not None and top > 300:
        y = top - 40
    else:
        return "NONE"
    return "SWIPE 540 %d %d" % (y, max(150, y - 800))


if __name__ == "__main__":
    print(decide(open(sys.argv[1], encoding="utf-8", errors="replace").read(),
                 int(sys.argv[2]) if len(sys.argv) > 2 else 2400))
