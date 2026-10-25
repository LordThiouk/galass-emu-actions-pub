#!/usr/bin/env python3
import os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from base_guard import check_base

ALLOW = ["http://192.162.69.113:8080", "http://192.162.69.113:8080/",
         "https://staging.example.net", "https://10.0.0.5:8443"]
DENY = ["", "http://192.162.69.113", "http://192.162.69.113:80", "http://192.162.69.113:8081",
        "https://192.162.69.113.galassapp.com", "http://other.example:8080", "http://example.com",
        "https://galassapp.com", "https://staging.galassapp.com", "https://GalassApp.com/api",
        "http://192.162.69.1130:8080", "http://192.162.69.11:8080", "http://1192.162.69.113:8080",
        "http://192.162.69.113:8080@evil.com", "http://user@192.162.69.113:8080",
        "https://192.162.69.113:8080@evil.com", "http://192.162.69.113:8080.evil.com",
        "http://192.162.69.113:08080", "ftp://192.162.69.113:8080", "192.162.69.113:8080",
        "http://192.162.69.113:8080?x=1", "http://192.162.69.113:8080#f",
        "http://192.162.69.113:8080 ", "http://192.162.69.113:abc", "https://"]
n = ok = 0
for b in ALLOW:
    n += 1; r = check_base(b)
    if r is None: ok += 1
    else: print(f"FAIL allow {b!r}: {r}")
for b in DENY:
    n += 1; r = check_base(b)
    if r is not None: ok += 1
    else: print(f"FAIL deny {b!r}")
cli = os.path.join(os.path.dirname(HERE), "base_guard.py")
for b, want in [("http://192.162.69.113:8080", 0), ("http://192.162.69.113", 1)]:
    n += 1
    if subprocess.run([sys.executable, cli, b], capture_output=True).returncode == want: ok += 1
    else: print(f"FAIL cli {b!r}")
print(f"\n{ok}/{n} base_guard cases passed")
sys.exit(0 if ok == n else 1)
