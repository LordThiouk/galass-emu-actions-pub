#!/usr/bin/env python3
"""Sonde « Confirmer l'arrêt » : ferme (annule, sans rien supprimer) les courses ouvertes du rider
avant de créer la course de test. Staging uniquement (base_guard). Jamais de jeton dans la sortie."""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base_guard import check_base  # noqa: E402

BASE = os.environ.get("BASE", "").rstrip("/")
reason = check_base(BASE)
if reason or "galassapp.com" in BASE:
    sys.exit("refusing BASE: %s" % (reason or "prod"))
RT, DEV = os.environ["RIDER_TOKEN"], os.environ["DEV_TOKEN"]


def call(method, path, body=None):
    req = urllib.request.Request(
        "%s/api/%s" % (BASE, path), method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + RT, "Accept": "application/json",
                 "Content-Type": "application/json", "dev-token": DEV})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            code, txt = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, txt = e.code, e.read().decode()
    try:
        return code, json.loads(txt)
    except ValueError:
        return code, {}


code, j = call("GET", "ride/list")
rides = (((j.get("data") or {}).get("rides") or {}).get("data")) or []
opened = [r for r in rides if str(r.get("status")) in ("0", "2", "3")]
print("open_rides", len(opened), flush=True)
for r in opened:
    c, jj = call("POST", "ride/cancel/%d" % int(r["id"]), {"cancel_reason": "E2E probe cleanup"})
    print("closed ride=%s status=%s -> http=%s remark=%s" % (r["id"], r.get("status"), c, jj.get("remark")), flush=True)
