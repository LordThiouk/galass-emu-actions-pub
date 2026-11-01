#!/usr/bin/env python3
"""Sonde SNAKBAR après un tap (runs 41112460972 + 41263271208).

Le formulaire ride (RideCreateForm) rejette un tap faute de sélection par un
SnackBar bref (~4 s, puis il disparaît) : « Veuillez sélectionner un service »,
« Veuillez sélectionner votre lieu de prise en charge et votre destination »,
« ... votre course en cours ... », « Le tarif recommandé n'est pas disponible ».
L'ancien dump « après commande » (rider_after_request_diag, seen[:25] avec
truncature) arrivait 3-5 s après le tap : le texte était déjà parti ou coupé par
la liste — d'où des runs sans cause identifiée.

Entrée : deux XML uiautomator (AVANT le tap, APRÈS le tap) ; "-" = avant absent.
Sortie : les textes (text/content-desc) NOUVEAUX après le tap, triés, max 8,
un par ligne — la réaction (ou le refus) de l'app en preuve directe. Aucun
secret n'est recopié : pas d'email, pas de jeton, pas de téléphone, pas de
GREEK de mesures (coordonnées, montants), pas de timestamps.
"""
import html
import re
import sys

# Bruit non discriminant : seuls les NOUVEAUX textes mènent au SnackBar.
NOISE = re.compile(r"\d{6,}|221\d|@|\[REDACTED\]|[A-Za-z0-9+/=_]{24,}"
                   r"|\b\d{1,3}[,.]\d+\b|\b\d{1,2}[:h]\d{2}\b|\b\d+ ?min\b")


def node_texts(xml):
    out = set()
    for t in re.findall(r"<node\b[^>]*>", xml):
        def g(n):
            m = re.search(r'\b' + n + r'="([^"]*)"', t)
            return html.unescape(m.group(1)) if m else ""
        v = re.sub(r"\s+", " ", (g("text") + " " + g("content-desc"))).strip()
        if len(v) > 2 and not NOISE.search(v):
            out.add(v)
    return out


def new_texts(before_path, after_path, limit=8):
    def read(p):
        try:
            return open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            return ""
    before = node_texts(read(before_path)) if before_path != "-" else set()
    after = node_texts(read(after_path))
    return sorted(after - before)[:limit]


def main(argv):
    if len(argv) != 3:
        print("usage: tap_reaction.py <before.xml|-> <after.xml>", file=sys.stderr)
        return 2
    for line in new_texts(argv[1], argv[2]):
        print(" " + line[:88])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
