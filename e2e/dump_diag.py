#!/usr/bin/env python3
"""dump_diag.py dump.xml -> diagnostic en clair d'un écran de recherche (sans secret).

Sortie (une ligne) : champs de saisie (texte réel), lignes cliquables sous le champ
(= résultats), nœuds visibles (premiers textes). Sert à savoir si la saisie arrive dans le
champ et si la liste de résultats se remplit.
"""
import html
import re
import sys
import xml.etree.ElementTree as ET

norm = lambda s: re.sub(r"\s+", " ", html.unescape(s or "")).strip()
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def box(n):
    b = BOUNDS.search(n.get("bounds", ""))
    return tuple(map(int, b.groups())) if b else None


def describe(path, max_texts=12):
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {"readable": False, "fields": [], "rows": 0, "texts": []}
    nodes = [n for n in root.iter("node") if n.get("visible-to-user", "true").lower() != "false"]
    fields = [n for n in nodes if "EditText" in (n.get("class") or "")]
    bottom = max((box(f)[3] for f in fields if box(f)), default=None)
    rows = 0
    if bottom is not None:
        for n in nodes:
            b = box(n)
            if b and b[1] >= bottom and (n.get("clickable") == "true") and norm(n.get("text") or n.get("content-desc")):
                rows += 1
    texts = []
    for n in nodes:
        t = norm(n.get("text")) or norm(n.get("content-desc"))
        if t and t not in texts:
            texts.append(t[:40])
    return {"readable": True,
            "fields": [norm(f.get("text")) for f in fields],
            "rows": rows, "texts": texts[:max_texts]}


if __name__ == "__main__":
    d = describe(sys.argv[1])
    if not d["readable"]:
        print("dump illisible")
    else:
        print("champs=%s lignes_résultat=%d nœuds=%s" % (
            [f[:40] for f in d["fields"]], d["rows"], d["texts"]))
