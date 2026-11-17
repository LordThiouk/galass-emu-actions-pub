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


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--has-input":
        sys.exit(0 if has_input(sys.argv[2]) else 1)
    r = find(sys.argv[1], sys.argv[2])
    if r:
        print("%d %d %s" % r)
