#!/usr/bin/env python3
"""B3 stop-order gate for the Galass staging harness.

Usage : python3 e2e/b3_order.py <out-dir>      (the dir holding state.json)
Output: exactly one line `<VERDICT>|<detail>`, VERDICT in {PASS, WARN, FAIL}.

Claim measured ("Bridge-B3"): the order of `stops[]` composed in the Rider app is
the order of the `ride_stops` read back by `GET ride/details`.

A FAIL (stop order disagrees) flips the run verdict: report.py fails the whole run
on any FAIL row, P0 or not, so `bug-b3-stops-order` does NOT belong in P0_POINTS.
A WARN (ride composed by the API, or unreadable evidence) measures nothing about
the app and leaves the verdict non-conclusive on this claim, never PASS.
"""
import json, sys

out = sys.argv[1]
try:
    st = json.load(open(f"{out}/state.json"))
except Exception as e:
    print("WARN|no evidence: state.json illisible (%s)" % str(e)[:100])
    raise SystemExit(0)

src = str(st.get("ride_source") or "unknown")
picked = (st.get("ride_picked") or {}).get("stops") or []
created = (st.get("ride_create") or {}).get("stops") or []
final = st.get("stops") or []


def labels(seq):
    rows = [s for s in seq if isinstance(s, dict)]
    if len(rows) != len(seq):
        return []
    if any(r.get("sequence") is not None for r in rows):
        def key(s):
            try:
                return int(s.get("sequence"))
            except (TypeError, ValueError):
                return 10 ** 6
        rows = sorted(rows, key=key)
    return [str(r.get("location_label") or "").strip() for r in rows]


def judge(labs):
    # True = l'arrêt 1 (Médina) précède l'arrêt 2 (Fann) ; None = illisible.
    if len(labs) != 2:
        return None
    low = [x.lower() for x in labs]
    p1 = [i for i, x in enumerate(low) if "m\u00e9dina" in x or "medina" in x]
    p2 = [i for i, x in enumerate(low) if "fann" in x]
    if len(p1) != 1 or len(p2) != 1:
        return None
    return p1[0] < p2[0]


p_labs, c_labs, f_labs = labels(picked), labels(created), labels(final)
shown = "composé=%s create=%s detail=%s" % (p_labs or "?", c_labs or "?", f_labs or "?")

if src != "ui":
    print("WARN|non concluant: course composée par l'API (ride_source=%s), "
          "l'ordre envoyé par l'app n'est pas mesuré" % src)
    raise SystemExit(0)

seen = {}
for name, labs in (("compose", p_labs), ("create", c_labs), ("detail", f_labs)):
    j = judge(labs)
    if j is not None:
        seen[name] = j
if not seen:
    print("WARN|no evidence: ordre illisible (%s)" % shown)
    raise SystemExit(0)
if False in seen.values():
    bad = next(k for k, v in seen.items() if v is False)
    print("FAIL|B3: ordre des arrêts différent (%s) %s" % (bad, shown))
    raise SystemExit(0)
print("PASS|B3: ordre identique %s" % shown)
