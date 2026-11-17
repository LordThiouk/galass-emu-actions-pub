#!/usr/bin/env python3
"""emu_sampler.py OUT LOGFILE [PERIODE_S] — échantillonne l'hôte et l'AVD toutes les N s.

Écrit OUT/metrics.jsonl (une ligne JSON par échantillon, sans secret : noms de processus
seulement). Sert à expliquer un plantage d'émulateur (CPU/RAM saturés ? gms en ANR ?).
"""
import json
import os
import re
import subprocess
import sys
import time

GMS_RE = re.compile(r"ANR in (com\.google\.android\.gms\.persistent|com\.google\.android\.configupdater)")


def cpu_times():
    with open("/proc/stat", encoding="utf-8") as fh:
        f = fh.readline().split()[1:]
    vals = list(map(int, f))
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def meminfo():
    d = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            k, _, v = line.partition(":")
            d[k] = int(v.split()[0]) // 1024  # Mo
    return d


def run(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def host_top5():
    out = run(["ps", "-eo", "pcpu,comm", "--sort=-pcpu"], 5).splitlines()[1:6]
    return [" ".join(l.split(None, 1)) for l in out]


def guest_top5():
    out = run(["adb", "shell", "top", "-b", "-n", "1", "-m", "5"], 8).splitlines()
    rows = [re.sub(r"\s+", " ", l).strip()[:110] for l in out if re.match(r"\s*\d+\s", l)]
    return rows[:5]


def main():
    out_dir, logfile = sys.argv[1], sys.argv[2]
    period = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    path = os.path.join(out_dir, "metrics.jsonl")
    last_idle, last_total = cpu_times()
    offset = 0
    ncpu = os.cpu_count() or 1
    while True:
        time.sleep(period)
        idle, total = cpu_times()
        d_total = max(total - last_total, 1)
        idle_pct = round(100.0 * (idle - last_idle) / d_total, 1)
        last_idle, last_total = idle, total
        mem = meminfo()
        anr = set()
        try:
            with open(logfile, encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()
            anr = {m.group(1).rsplit(".", 1)[-1] for m in GMS_RE.finditer(chunk)}
        except OSError:
            pass
        row = {
            "t": int(time.time()),
            "ncpu": ncpu,
            "load1": float(open("/proc/loadavg", encoding="utf-8").read().split()[0]),
            "cpu_idle_pct": idle_pct,
            "mem_avail_mb": mem.get("MemAvailable"),
            "swap_used_mb": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
            "host_top5": host_top5(),
            "guest_top5": guest_top5(),
            "anr_gms": sorted(anr),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
