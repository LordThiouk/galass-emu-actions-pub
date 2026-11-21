#!/usr/bin/env python3
"""tap_node.py dump.xml label -> "x y" du centre à taper, ou rien.

Cherche le nœud VISIBLE dont text ou content-desc (espaces normalisés) vaut label, puis
remonte au plus petit ancêtre cliquable (un libellé n'est souvent pas cliquable lui-même :
le tap doit viser le parent qui porte le clic). Retombe sur le nœud si aucun ancêtre.
Sortie additionnelle : "x y clickable|node".
"""
import html
import re
import sys
import xml.etree.ElementTree as ET

norm = lambda s: re.sub(r"\s+", " ", html.unescape(s or "")).strip()
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def centre(node):
    b = BOUNDS.search(node.get("bounds", ""))
    if not b:
        return None
    x1, y1, x2, y2 = map(int, b.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


def find(path, label):
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    want = norm(label)
    parent = {c: p for p in root.iter() for c in p}
    for n in root.iter("node"):
        if n.get("visible-to-user", "true").lower() == "false":
            continue
        if norm(n.get("text")) != want and norm(n.get("content-desc")) != want:
            continue
        if centre(n) is None:
            continue
        cur = n
        while cur is not None:
            if cur.get("clickable") == "true" and centre(cur):
                return centre(cur) + ("clickable",)
            cur = parent.get(cur)
        return centre(n) + ("node",)
    return None


def has_input(path):
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return False
    return any("EditText" in (n.get("class") or "") for n in root.iter("node"))


def empty_input(path):
    """Centre du premier champ de saisie VISIBLE vide (ex. destination), sinon du dernier champ."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    last = None
    for n in root.iter("node"):
        if "EditText" not in (n.get("class") or ""):
            continue
        if n.get("visible-to-user", "true").lower() == "false":
            continue
        c = centre(n)
        if not c:
            continue
        if not norm(n.get("text")):
            return c + ("input",)
        last = c
    # Aucun champ vide (un indice « Adresse de l'arrêt » peut tenir lieu de texte) : le dernier
    # champ visible est celui de la saisie en cours (destination / arrêt).
    return last + ("input-last",) if last else None


def row_control(path, label):
    """Centre du contrôle cliquable (ImageView) situé sur la MÊME ligne que `label`, à sa droite.

    Écran « Paiement » : la ligne « Mode de paiement » ouvre son sélecteur par une icône
    cliquable, pas par le libellé. On prend le plus à droite parmi les ImageView cliquables dont
    le centre vertical tombe dans la hauteur du libellé.
    """
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    want = norm(label)
    row = None
    for n in root.iter("node"):
        if n.get("visible-to-user", "true").lower() == "false":
            continue
        if norm(n.get("text")) == want or norm(n.get("content-desc")) == want:
            b = BOUNDS.search(n.get("bounds", ""))
            if b:
                row = tuple(map(int, b.groups()))
                break
    if row is None:
        return None
    tol = max(60, (row[3] - row[1]))
    cy = (row[1] + row[3]) // 2
    best = None
    for n in root.iter("node"):
        if "ImageView" not in (n.get("class") or "") or n.get("clickable") != "true":
            continue
        if n.get("visible-to-user", "true").lower() == "false":
            continue
        c = centre(n)
        if c is None or abs(c[1] - cy) > tol or c[0] <= row[0]:
            continue
        if best is None or c[0] > best[0]:
            best = c
    return best + ("control",) if best else None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--has-input":
        sys.exit(0 if has_input(sys.argv[2]) else 1)
    if len(sys.argv) == 3 and sys.argv[1] == "--empty-input":
        r = empty_input(sys.argv[2])
        if r:
            print("%d %d %s" % r)
        sys.exit(0)
    if len(sys.argv) == 4 and sys.argv[1] == "--row-control":
        r = row_control(sys.argv[2], sys.argv[3])
        if r:
            print("%d %d %s" % r)
        sys.exit(0)
    r = find(sys.argv[1], sys.argv[2])
    if r:
        print("%d %d %s" % r)
