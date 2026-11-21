#!/usr/bin/env python3
"""Diagnostic read-only : pourquoi aucun chauffeur n'est assigné quand status==0.

Mesure, pas une porte. Lit :
  * $OUT/assign-pre.txt        — sonde assign_pre (bal, online AVANT l'accept)
  * $OUT/last-ride-state.txt   — status= de la course
  * $OUT/api.jsonl             — réponses driver/ride/offer/respond (att, remark)

Émet UNE ligne `VERDICT<TAB>detail` (detail ≤ 140 caractères). Aucune écriture,
aucun état, aucun verdict changé. Le seuil de solde négatif n'est pas lisible
en public : la cause bal-guard vient du remark `balance_limit` émis par le
backend, jamais d'une comparaison sur bal.
"""
import json
import os
import re
import sys

# remark backend -> cause nommée. Le remark est autoritaire, jamais le solde.
CAUSES = {
    "balance_limit": "bal-guard",
    "driver_offline": "offline",
    "ride_unavailable": "ride-gone",
    "offer_expired": "offer-expired",
    "not_authorized": "no-offer",
}
RESPOND_PATH = "driver/ride/offer/respond"


def read_assign_pre(path):
    """(bal, online) de la dernière ligne assign_pre ; None si illisible."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
    except OSError:
        return None, None
    if not lines:
        return None, None
    m = re.search(r"assign_pre\s+bal=(-?\d+(?:\.\d+)?|\?)\s+online=([01?])", lines[-1])
    if not m:
        return None, None
    bal = None if m.group(1) == "?" else m.group(1)
    online = None if m.group(2) == "?" else m.group(2)
    return bal, online


def read_status(path):
    """status entier de last-ride-state.txt ; None si illisible."""
    try:
        with open(path, encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        return None
    m = re.search(r"(?:^|\s)status=(-?\d+)(?:\s|$)", txt)
    return int(m.group(1)) if m else None


def read_attempts(path):
    """(att, dernier remark) sur les réponses driver/ride/offer/respond."""
    att, remark = 0, None
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return att, remark
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("path") != RESPOND_PATH:
                continue
            att += 1
            resp = rec.get("resp")
            if isinstance(resp, dict) and resp.get("remark"):
                remark = str(resp["remark"])
    return att, remark


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OUT", "e2e-out")
    bal, online = read_assign_pre(os.path.join(out, "assign-pre.txt"))
    status = read_status(os.path.join(out, "last-ride-state.txt"))
    att, remark = read_attempts(os.path.join(out, "api.jsonl"))
    ctx = (f"att={att} bal={bal if bal is not None else '?'} "
           f"online={online if online is not None else '?'}")
    if status is None:
        verdict, detail = "WARN", f"state-unreadable ({ctx})"
    elif status != 0:
        verdict, detail = "PASS", f"assigned status={status} ({ctx})"
    elif remark in CAUSES:
        verdict, detail = "FAIL", f"{CAUSES[remark]} (remark={remark} {ctx})"
    elif att == 0:
        verdict, detail = "FAIL", f"no-attempt (remark=aucun {ctx})"
    elif remark:
        short = re.sub(r"[^a-z0-9]+", "-", remark.lower()).strip("-")[:24]
        verdict, detail = "FAIL", f"remark-{short or 'vide'} (remark={remark} {ctx})"
    else:
        verdict, detail = "FAIL", f"no-remark (remark=aucun {ctx})"
    detail = detail.replace("\t", " ").replace("\n", " ")[:140]
    print(f"{verdict}\t{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
