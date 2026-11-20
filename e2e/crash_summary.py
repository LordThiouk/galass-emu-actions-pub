#!/usr/bin/env python3
"""crash_summary.py OUT [EPOCH] [FENETRE_S] — résumé en clair, sans secret, de la fenêtre
précédant un plantage d'émulateur, et UNE ligne de conclusion :
  CPU/RAM saturés (piste 2) | gms en cause (piste 1) | ni l'un ni l'autre.
"""
import json
import os
import sys
import time

CPU_IDLE_SAT = 10.0   # % d'inactivité CPU hôte en dessous duquel on parle de saturation
LOAD_FACTOR = 1.5     # load1 >= 1.5 x nombre de vCPU
MEM_SAT_MB = 800      # RAM hôte disponible sous ce seuil
SWAP_SAT_MB = 200     # swap utilisé au-dessus de ce seuil


def load(out_dir):
    rows = []
    try:
        with open(os.path.join(out_dir, "metrics.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def summarize(rows, epoch, window=60):
    win = [r for r in rows if epoch - window <= r.get("t", 0) <= epoch]
    if not win:
        return ["[crash-summary] aucun échantillon dans la fenêtre de %ds" % window,
                "[crash-summary] CONCLUSION: mesure indisponible (ni l'un ni l'autre démontré)"]
    idle_min = min(r["cpu_idle_pct"] for r in win)
    load_max = max(r["load1"] for r in win)
    ncpu = win[-1].get("ncpu") or 1
    mem_min = min(r["mem_avail_mb"] for r in win if r.get("mem_avail_mb") is not None)
    swap_max = max(r.get("swap_used_mb") or 0 for r in win)
    gms = sorted({a for r in win for a in r.get("anr_gms", [])})
    last = win[-1]
    sat_cpu = idle_min < CPU_IDLE_SAT or load_max >= LOAD_FACTOR * ncpu
    sat_ram = mem_min < MEM_SAT_MB or swap_max > SWAP_SAT_MB
    lines = [
        "[crash-summary] fenêtre %ds avant le plantage (%d échantillons)" % (window, len(win)),
        "[crash-summary] CPU hôte: inactif min %.1f%%, load1 max %.2f sur %d vCPU" % (idle_min, load_max, ncpu),
        "[crash-summary] RAM hôte: MemAvailable min %d Mo, swap utilisé max %d Mo" % (mem_min, swap_max),
        "[crash-summary] top5 hôte (pcpu processus): " + " | ".join(last.get("host_top5", [])),
        "[crash-summary] top5 AVD: " + " | ".join(last.get("guest_top5", [])),
        "[crash-summary] ANR gms.persistent/configupdater dans la fenêtre: "
        + (", ".join(gms) if gms else "non"),
    ]
    if sat_cpu or sat_ram:
        what = "CPU" if sat_cpu and not sat_ram else ("RAM" if sat_ram and not sat_cpu else "CPU et RAM")
        extra = " (gms aussi en ANR)" if gms else ""
        lines.append("[crash-summary] CONCLUSION: %s saturés -> piste 2 (alléger l'émulateur/GPU)%s" % (what, extra))
    elif gms:
        lines.append("[crash-summary] CONCLUSION: gms en cause (%s) -> piste 1 (sans gms)" % ", ".join(gms))
    else:
        lines.append("[crash-summary] CONCLUSION: ni l'un ni l'autre (CPU/RAM non saturés, pas d'ANR gms)")
    return lines


def main():
    out_dir = sys.argv[1]
    rows = load(out_dir)
    epoch = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else (rows[-1]["t"] if rows else int(time.time()))
    window = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    print("\n".join(summarize(rows, epoch, window)))


if __name__ == "__main__":
    main()
