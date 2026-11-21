#!/usr/bin/env python3
"""Ordre des arrêts TEL QU'AFFICHÉ, lu sur l'écran de composition (Bridge-B3).

`ride/list` ne renvoie pas les stops et une course composée par l'UI n'a pas de
`ride_create` : le gate B3 ne mesurait alors que la relecture de `GET ride/details`,
jamais l'ordre que l'app tient elle-même. Ce script lit le dump uiautomator de
l'écran 17 (« 17-rider-multistop-summary », les deux arrêts posés avant la demande)
et rend l'ordre affiché, indépendant du backend.

Usage : python3 e2e/compose_order.py <dump.xml> [--state <state.json>]
Sortie: exactement une ligne `OK|...` ou `WARN|...` ; sort toujours en 0.
"""
import html
import json
import re
import sys
import xml.etree.ElementTree as ET

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
# (clé de compte rendu, aiguilles en minuscules) — accents tolérés comme à l'écran.
WANTED = (("médina", ("médina", "medina")), ("fann", ("fann",)))
NO_Y = 10 ** 9  # nœud sans bounds : candidat, mais jamais préféré à un nœud positionné.


def norm(value):
    return " ".join(html.unescape(value or "").split())


def top_y(node):
    m = BOUNDS.search(node.get("bounds") or "")
    return int(m.group(2)) if m else NO_Y


def load_nodes(path):
    root = ET.parse(path).getroot()
    found = []
    for node in root.iter("node"):
        if (node.get("visible-to-user") or "").strip().lower() == "false":
            continue
        text = norm(node.get("text")) or norm(node.get("content-desc"))
        if text:
            found.append((text, top_y(node)))
    return found


def write_state(state_path, ordered):
    with open(state_path) as fh:
        st = json.load(fh)
    st["compose_ui"] = [{"location_label": t} for t in ordered]
    with open(state_path, "w") as fh:
        json.dump(st, fh)


def main(argv):
    dump, state_path = None, None
    args = list(argv[1:])
    while args:
        arg = args.pop(0)
        if arg == "--state" and args:
            state_path = args.pop(0)
        elif dump is None:
            dump = arg
        # argument inconnu : ignoré, le harnais ne doit pas mourir sur un flag de trop.
    if not dump:
        return "WARN|usage: compose_order.py <dump.xml> [--state <state.json>]"

    try:
        nodes = load_nodes(dump)
    except Exception as e:  # ET.ParseError, OSError, encoding inconnu : une seule ligne, jamais de trace.
        return "WARN|dump illisible (%s)" % str(e)[:120]

    hits, missing = [], []
    for key, needles in WANTED:
        best = None
        for text, y in nodes:
            low = text.lower()
            if any(n in low for n in needles) and (best is None or y < best[1]):
                best = (text, y)
        if best is None:
            missing.append(key)
        else:
            hits.append(best)

    hits.sort(key=lambda h: h[1])
    ordered = [h[0] for h in hits]
    if missing:
        return "WARN|arrêt(s) absent(s) de l'écran de composition: %s (trouvés=%s)" % (
            ", ".join(missing), " > ".join(ordered) or "aucun")

    if state_path:
        try:
            write_state(state_path, ordered)
        except Exception as e:
            return "WARN|state illisible en écriture (%s)" % str(e)[:120]

    return "OK|%s" % " > ".join(ordered)


if __name__ == "__main__":
    try:
        line = main(sys.argv)
    except Exception as e:  # garde-fou : contrat « une ligne, exit 0 » avant tout.
        line = "WARN|interne (%s)" % str(e)[:120]
    print(line)
