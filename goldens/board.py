#!/usr/bin/env python3
"""Planche avant/après d'un échec golden-diff — stdlib only.

Flutter écrit trois PNG par golden qui échoue, à côté du fichier de test :

    failures/<golden>_masterImage.png  → la référence (issue de la branche de base)
    failures/<golden>_testImage.png    → la capture de la branche évaluée
    failures/<golden>_maskedDiff.png   → les pixels différents

Ce script les assemble en une planche HTML autonome (images en base64, aucune
dépendance, aucun appel réseau) plus un résumé Markdown. Il ne compare rien
lui-même : le verdict vient de `flutter test`, la planche ne fait que rendre
l'écart lisible.

Usage:
    board.py --failures app/test/goldens/rider/failures \
             --goldens app/test/goldens/rider \
             --out board/ \
             --app Galass_Rider --ref feat/ma-branche --base main

Sortie: board/index.html, board/summary.md
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
from pathlib import Path

DIFF_RE = re.compile(r'Golden "(?P<name>[^"]+)": Pixel test failed, '
                     r'(?P<pct>[\d.]+)%, (?P<px>\d+)px diff detected')
MISSING_RE = re.compile(r'Golden "(?P<name>[^"]+)": Flutter Golden file does '
                        r'not exist')


def parse_failures(flutter_log: str) -> list[dict]:
    found: dict[str, dict] = {}
    for match in DIFF_RE.finditer(flutter_log):
        found[match.group('name')] = {
            'name': match.group('name'),
            'kind': 'diff',
            'percent': float(match.group('pct')),
            'pixels': int(match.group('px')),
        }
    for match in MISSING_RE.finditer(flutter_log):
        found.setdefault(match.group('name'), {
            'name': match.group('name'),
            'kind': 'missing_baseline',
            'percent': None,
            'pixels': None,
        })
    return sorted(found.values(), key=lambda row: (-(row['percent'] or 0),
                                                   row['name']))


def embed(path: Path) -> str | None:
    if not path.is_file():
        return None
    return base64.b64encode(path.read_bytes()).decode('ascii')


def image_cell(data: str | None, label: str) -> str:
    if data is None:
        return (f'<figure><figcaption>{html.escape(label)}</figcaption>'
                '<p class="absent">absente</p></figure>')
    return (f'<figure><figcaption>{html.escape(label)}</figcaption>'
            f'<img alt="{html.escape(label)}" src="data:image/png;base64,{data}">'
            '</figure>')


CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; padding: 24px; background: #0f1115; color: #e7e9ee; }
h1 { font-size: 20px; margin: 0 0 4px; }
p.meta { color: #9aa3b2; margin: 0 0 24px; }
.row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
       gap: 16px; padding: 16px; margin-bottom: 24px;
       background: #161922; border: 1px solid #242a36; border-radius: 10px; }
.row h2 { grid-column: 1 / -1; font-size: 15px; margin: 0; }
.row h2 span { color: #9aa3b2; font-weight: 400; }
figure { margin: 0; }
figcaption { color: #9aa3b2; font-size: 12px; margin-bottom: 6px; }
img { width: 100%; height: auto; display: block; border-radius: 6px;
      border: 1px solid #242a36; background: #fff; }
p.absent { color: #ff6b6b; font-family: ui-monospace, monospace; }
"""


def build_board(rows: list[dict], failures_dir: Path, goldens_dir: Path,
                meta: dict) -> tuple[str, str]:
    cards = []
    for row in rows:
        stem = row['name'][:-len('.png')] if row['name'].endswith('.png') else row['name']
        master = embed(failures_dir / f'{stem}_masterImage.png')
        test = embed(failures_dir / f'{stem}_testImage.png')
        diff = embed(failures_dir / f'{stem}_maskedDiff.png')
        if row['kind'] == 'diff':
            detail = f"écart {row['percent']}% — {row['pixels']} px"
        else:
            detail = 'aucune référence sur la branche de base'
        cards.append(
            '<section class="row">'
            f'<h2>{html.escape(row["name"])} <span>· {html.escape(detail)}</span></h2>'
            f'{image_cell(master, "référence (" + meta["base"] + ")")}'
            f'{image_cell(test, "branche évaluée (" + meta["ref"] + ")")}'
            f'{image_cell(diff, "pixels différents")}'
            '</section>'
        )
    doc = (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        f'<title>golden-diff — {html.escape(meta["app"])}</title>'
        f'<style>{CSS}</style></head><body>'
        f'<h1>golden-diff — {html.escape(meta["app"])}</h1>'
        f'<p class="meta">branche évaluée <code>{html.escape(meta["ref"])}</code>'
        f' · référence <code>{html.escape(meta["base"])}</code>'
        f' · {len(rows)} écart(s) · verdict '
        f'<strong>{html.escape(meta["verdict"])}</strong></p>'
        + ''.join(cards) +
        '</body></html>'
    )

    md = ['| golden | écart | pixels |', '|---|---|---|']
    for row in rows:
        pct = '—' if row['percent'] is None else f"{row['percent']}%"
        px = '—' if row['pixels'] is None else str(row['pixels'])
        md.append(f"| `{row['name']}` | {pct} | {px} |")
    md.append('')
    md.append(f"Verdict : **{meta['verdict']}** — planche détaillée : `index.html`.")
    return doc, '\n'.join(md)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--failures', required=True)
    parser.add_argument('--goldens', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--app', default='Galass_Rider')
    parser.add_argument('--ref', default='inconnue')
    parser.add_argument('--base', default='main')
    parser.add_argument('--log', default='',
                        help='fichier log flutter test (défaut: stdin si vide)')
    parser.add_argument('--verdict-file', default='',
                        help='fichier contenant le verdict (défaut: déduit des écarts)')
    args = parser.parse_args()

    failures_dir = Path(args.failures)
    goldens_dir = Path(args.goldens)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    log = Path(args.log).read_text(encoding='utf-8', errors='replace') \
        if args.log else sys.stdin.read()
    rows = parse_failures(log)

    if args.verdict_file and Path(args.verdict_file).is_file():
        verdict = Path(args.verdict_file).read_text(encoding='utf-8').strip()
    else:
        verdict = 'ROUGE' if rows else 'VERT'

    meta = {'app': args.app, 'ref': args.ref, 'base': args.base,
            'verdict': verdict}
    doc, md = build_board(rows, failures_dir, goldens_dir, meta)
    (out_dir / 'index.html').write_text(doc, encoding='utf-8')
    (out_dir / 'summary.md').write_text(md + '\n', encoding='utf-8')
    (out_dir / 'gaps.json').write_text(json.dumps(rows, indent=2) + '\n',
                                       encoding='utf-8')
    print(f'{len(rows)} écart(s) — verdict {verdict} — {out_dir}/index.html')
    for row in rows:
        pct = '—' if row['percent'] is None else f"{row['percent']}%"
        print(f"  {row['name']}: {pct}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
