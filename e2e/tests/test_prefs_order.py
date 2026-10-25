#!/usr/bin/env python3
"""B10 / ISC-1 — write_prefs must create shared_prefs BEFORE piping the XML.

Run 40177022780: the old order (mkdir AFTER the piped `cat >`) failed for both
apps — « can't create shared_prefs/FlutterSharedPreferences.xml » — leaving both
apps without an access token, and they left the foreground for the launcher
(33 UI points FAIL). The fix is an ordering contract on e2e/run.sh.
Also keeps the S18 contract: no /tmp file may ever hold the access token.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")


def func_write_prefs():
    body = open(RUN, encoding="utf-8").read()
    m = re.search(r"^write_prefs\(\) \{.*?^\}", body, re.M | re.S)
    assert m, "write_prefs() introuvable dans run.sh"
    return m.group(0)


def test_mkdir_before_the_piped_cat():
    fn = func_write_prefs()
    mkdir_pos = fn.find("run-as $pkg mkdir -p shared_prefs")
    cat_pos = fn.find("cat > shared_prefs/FlutterSharedPreferences.xml")
    assert mkdir_pos != -1, "run-as mkdir -p shared_prefs introuvable"
    assert cat_pos != -1, "cat > ... interval introuvable"
    assert mkdir_pos < cat_pos, (
        "shared_prefs doit être créé AVANT le cat — "
        "l'ordre ancien crashait les deux apps (run 40177022780)")


def test_chmod_stays_after_the_cat():
    fn = func_write_prefs()
    mkdir_pos = fn.find("run-as $pkg mkdir -p shared_prefs")
    chmod_pos = fn.find("run-as $pkg chmod 660")
    cat_pos = fn.find("cat > shared_prefs/FlutterSharedPreferences.xml")
    assert chmod_pos > cat_pos > mkdir_pos


def test_no_temp_token_file():
    body = open(RUN, encoding="utf-8").read()
    assert "/tmp/prefs-" not in body, "S18 violé: fichier temporaire de token"


CASES = [test_mkdir_before_the_piped_cat, test_chmod_stays_after_the_cat,
         test_no_temp_token_file]

if __name__ == "__main__":
    for fn in CASES:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"PASS ({len(CASES)}/{len(CASES)})")
