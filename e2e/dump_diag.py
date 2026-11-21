#!/usr/bin/env python3
"""dump_diag.py dump.xml [--texts] -> diagnostic en clair d'un écran (sans secret).

Défaut (recherche de lieu) : texte COMPLET de chaque champ de saisie, lequel a le focus,
lignes cliquables sous le champ (= résultats), premiers nœuds visibles. `--texts` : seulement
les textes visibles (jusqu'à 25), pour expliquer un TIMEOUT de capture.
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


def visible_texts(nodes, limit, width=60):
    texts = []
    for n in nodes:
        t = norm(n.get("text")) or norm(n.get("content-desc"))
        if t and t[:width] not in texts:
            texts.append(t[:width])
    return texts[:limit]


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
            if b and b[1] >= bottom and n.get("clickable") == "true" and norm(n.get("text") or n.get("content-desc")):
                rows += 1
    return {"readable": True,
            "fields": [{"text": norm(f.get("text")), "focused": f.get("focused") == "true"} for f in fields],
            "rows": rows, "texts": visible_texts(nodes, max_texts)}


def fold(s):
    import unicodedata
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def field_has(path, query):
    """True si un champ de saisie contient la requête tapée (comparaison sans accents ni casse)."""
    d = describe(path)
    return any(fold(query) in fold(f["text"]) for f in d["fields"])


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--contains":
        sys.exit(0 if field_has(sys.argv[2], sys.argv[3]) else 1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    d = describe(args[0], 25 if "--texts" in sys.argv else 12)
    if not d["readable"]:
        print("dump illisible")
    elif "--texts" in sys.argv:
        print("textes visibles=%s" % d["texts"])
    else:
        print("champs=%s lignes_résultat=%d nœuds=%s" % (
            [("FOCUS:" if f["focused"] else "") + f["text"] for f in d["fields"]],
            d["rows"], d["texts"]))
