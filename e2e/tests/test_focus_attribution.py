#!/usr/bin/env python3
"""TDD spec for reliable foreground attribution, accept credit, and the Artemis smoke log.

No emulator, no network, python3 only. Pins:
  * `fg_pkg` reads the DEFAULT display (mDisplayId=0), never the launcher of a
    secondary display, and falls through window -> activities -> top;
  * `wait_ui` treats a marker as authoritative: a lying foreground is logged, not
    fatal, and rc=3 only when the foreground stayed foreign and no marker matched;
  * `launch` starts the resolved launcher component with `am start -W -n`;
  * `accepted_via(code, kind, assigned, tap_proven)` credits api, ui or unknown,
    and an unknown credit never renews and always carries an explicit cause;
  * run.sh never publishes accept-via=PASS on an unknown credit;
  * Artemis always runs standalone on the pinned device serial, and its log is
    published masked (API key / token / uid) to run.log and the smoke artifact.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
UI = os.path.join(ROOT, "ui.sh")
SANITIZE = os.path.join(ROOT, "sanitize.py")
SMOKE_SH = os.path.join(ROOT, "smoke_log.sh")
sys.path.insert(0, ROOT)

RIDER_PKG = "sn.galass.galassrider"
DRIVER_PKG = "sn.galass.galassdriver"
LAUNCHER_PKG = "com.android.launcher3"

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def text(path):
    return open(path, encoding="utf-8").read()


# ---------------------------------------------------------------- fixtures
# Android 13/14 `dumpsys window displays`: display 2 (launcher) is printed FIRST,
# which is exactly how the old `grep -m1` bug declared the Driver app absent.
WINDOW_MULTI_DISPLAY = f"""\
WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=2
    init=1080x2340 420dpi cur=1080x2340 app=1080x2138 rng=1080x2138-1080x2138
    mCurrentFocus=Window{{21d4f7a u0 {LAUNCHER_PKG}/{LAUNCHER_PKG}.Launcher}}
    mFocusedApp=ActivityRecord{{7c0e1b2 u0 {LAUNCHER_PKG}/.Launcher t7}}
  Display: mDisplayId=0
    init=1080x2340 420dpi cur=1080x2340 app=1080x2138 rng=1080x2138-1080x2138
    mCurrentFocus=Window{{a1b2c3d u0 {DRIVER_PKG}/{DRIVER_PKG}.MainActivity}}
    mFocusedApp=ActivityRecord{{d4e5f6a u0 {DRIVER_PKG}/.MainActivity t42}}
"""

WINDOW_NO_FOCUS = """\
WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=0
    init=1080x2340 420dpi cur=1080x2340 app=1080x2138
    mCurrentFocus=null
    mFocusedApp=null
"""

ACTIVITIES_FALLBACK = f"""\
ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  * Task{{4a7f21c #42 type=standard A={DRIVER_PKG}}}
    mResumedActivity: ActivityRecord{{9c3b7d1 u0 {DRIVER_PKG}/.MainActivity t42}}
    topResumedActivity=ActivityRecord{{9c3b7d1 u0 {DRIVER_PKG}/.MainActivity t42}}
    ResumedActivity: ActivityRecord{{9c3b7d1 u0 {DRIVER_PKG}/.MainActivity t42}}
"""

# A dump that names displays 2 and 1 but never 0: there is NO default display, so
# the only honest answer is "" (fall through to the next source), never the first
# block — that fallback put the secondary-display launcher back in the winning seat.
WINDOW_NO_DEFAULT_DISPLAY = f"""\
WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=2
    init=1080x2340 420dpi cur=1080x2340 app=1080x2138 rng=1080x2138-1080x2138
    mCurrentFocus=Window{{21d4f7a u0 {LAUNCHER_PKG}/{LAUNCHER_PKG}.Launcher}}
    mFocusedApp=ActivityRecord{{7c0e1b2 u0 {LAUNCHER_PKG}/.Launcher t7}}
  Display: mDisplayId=1
    init=1080x2340 420dpi cur=1080x2340 app=1080x2138 rng=1080x2138-1080x2138
    mCurrentFocus=Window{{b7e1c02 u0 {LAUNCHER_PKG}/{LAUNCHER_PKG}.Launcher}}
    mFocusedApp=ActivityRecord{{0f1a2b3 u0 {LAUNCHER_PKG}/.Launcher t9}}
"""

ACTIVITIES_NO_DEFAULT_DISPLAY = f"""\
ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #2 (activities from top to bottom):
  * Task{{4a7f21c #42 type=standard A={LAUNCHER_PKG}}}
    mResumedActivity: ActivityRecord{{9c3b7d1 u0 {LAUNCHER_PKG}/.Launcher t42}}
Display #1 (activities from top to bottom):
  * Task{{5b8e32d #43 type=standard A={LAUNCHER_PKG}}}
    mResumedActivity: ActivityRecord{{a1c4e2f u0 {LAUNCHER_PKG}/.Launcher t43}}
"""

TOP_NO_DEFAULT_DISPLAY = f"""\
ACTIVITY MANAGER TOP (dumpsys activity top)
Display #2 (activities from top to bottom):
  TASK {LAUNCHER_PKG} uid 10123
    ACTIVITY {LAUNCHER_PKG}/.Launcher 5c3d1a2 pid=1234
        mResumedActivity: ActivityRecord{{5c3d1a2 u0 {LAUNCHER_PKG}/.Launcher t42}}
"""

UI_ACCEPT_XML = """\
<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="sn.galass.galassdriver" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[0,0][1080,2340]">
    <node index="0" text="Accepter la course" resource-id="" class="android.widget.Button" package="sn.galass.galassdriver" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[100,900][600,1000]" />
    <node index="1" text="E2E Stop 1 Medina — destination du parcours de recette staging" resource-id="" class="android.widget.TextView" package="sn.galass.galassdriver" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[40,120][900,180]" />
  </node>
</hierarchy>
"""

UI_NO_MARKER_XML = """\
<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="sn.galass.galassdriver" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[0,0][1080,2340]">
    <node index="0" text="Accueil chauffeur — aucune offre en attente pour le moment" resource-id="" class="android.widget.TextView" package="sn.galass.galassdriver" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[40,120][900,180]" />
    <node index="1" text="En ligne" resource-id="" class="android.widget.Switch" package="sn.galass.galassdriver" content-desc="" checkable="true" checked="true" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[40,200][400,260]" />
  </node>
</hierarchy>
"""

STUB_ADB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$ADB_LOG"
case "$*" in
  *"dumpsys window displays"*) [ -n "${WINDOW_DUMP:-}" ] && cat "$WINDOW_DUMP" ;;
  *"dumpsys activity activities"*) [ -n "${ACTIVITIES_DUMP:-}" ] && cat "$ACTIVITIES_DUMP" ;;
  *"dumpsys activity top"*) [ -n "${TOP_DUMP:-}" ] && cat "$TOP_DUMP" ;;
  *"resolve-activity"*) [ -n "${RESOLVE_OUT:-}" ] && cat "$RESOLVE_OUT" ;;
  *"uiautomator dump"*) printf 'UI hierarchy dumped to: /sdcard/w.xml\\n' ;;
  *"cat /sdcard/w.xml"*) [ -n "${UI_XML_FIXTURE:-}" ] && cat "$UI_XML_FIXTURE" ;;
esac
exit 0
"""


def write(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def make_env(tmp, **extra):
    """tmp OUT + a stub `adb` first on PATH. GITHUB_ACTIONS removed (fixtures)."""
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    stub = os.path.join(bin_dir, "adb")
    write(stub, STUB_ADB)
    os.chmod(stub, 0o755)
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    env.update({
        "OUT": tmp,
        "SMOKE_LOG": os.path.join(tmp, "smoke.log"),
        "PATH": bin_dir + os.pathsep + os.environ["PATH"],
        "ADB_LOG": os.path.join(tmp, "adb.log"),
        "PY": sys.executable,
    })
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def bash(script, env, cwd=None, argv=()):
    return subprocess.run(["bash", "-c", script, "_", *argv],
                          capture_output=True, text=True, env=env, cwd=cwd)


def extract_fn(path, name):
    """Return the whole `name() { ... }` definition (one-line or multi-line).

    Brace counting, not a `^}$` regex: `launch()` is written on one line, and the
    old regex silently skipped it. Every `{` in these functions is balanced
    (`${...}`, `${#RESULTS[@]}`), so counting is safe here.
    """
    body = text(path)
    start = re.search(r"(?m)^%s\(\) \{" % re.escape(name), body)
    assert start, f"{name}() introuvable dans {path}"
    depth = 0
    for i in range(start.end() - 1, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                return body[start.start():i + 1]
    raise AssertionError(f"{name}() non fermée dans {path}")


def adb_lines(env):
    path = env["ADB_LOG"]
    if not os.path.exists(path):
        return []
    return [line.strip() for line in open(path, encoding="utf-8").read().splitlines() if line.strip()]


# ------------------------------------------------- 1. default display only
@case
def test_fg_pkg_ignores_the_secondary_display_launcher():
    tmp = tempfile.mkdtemp(prefix="e2e-fg-multi-")
    win = write(os.path.join(tmp, "window.txt"), WINDOW_MULTI_DISPLAY)
    env = make_env(tmp, WINDOW_DUMP=win)
    proc = bash('. "$1"; fg_pkg', env, argv=(UI,))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == DRIVER_PKG, proc.stdout
    assert LAUNCHER_PKG not in proc.stdout, "le launcher du display 2 a gagné"
    log = adb_lines(env)
    assert any("dumpsys window displays" in line for line in log), log


@case
def test_fg_pkg_falls_back_to_activity_activities():
    tmp = tempfile.mkdtemp(prefix="e2e-fg-fallback-")
    win = write(os.path.join(tmp, "window.txt"), WINDOW_NO_FOCUS)
    acts = write(os.path.join(tmp, "activities.txt"), ACTIVITIES_FALLBACK)
    env = make_env(tmp, WINDOW_DUMP=win, ACTIVITIES_DUMP=acts)
    proc = bash('. "$1"; fg_pkg', env, argv=(UI,))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == DRIVER_PKG, proc.stdout
    log = adb_lines(env)
    first_window = next(i for i, line in enumerate(log) if "dumpsys window displays" in line)
    first_activity = next(i for i, line in enumerate(log) if "dumpsys activity activities" in line)
    assert first_window < first_activity, log


# ------------------------------------------- 2. wait_ui marker is authoritative
@case
def test_wait_ui_marker_wins_over_a_lying_foreground():
    tmp = tempfile.mkdtemp(prefix="e2e-waitui-lie-")
    win = write(os.path.join(tmp, "window.txt"), WINDOW_MULTI_DISPLAY.replace(
        DRIVER_PKG, LAUNCHER_PKG))
    xml = write(os.path.join(tmp, "ui.xml"), UI_ACCEPT_XML)
    env = make_env(tmp, WINDOW_DUMP=win, UI_XML_SRC=xml,
                   UI_XML=os.path.join(tmp, "w.xml"), E2E_ALLOW_FIXTURE="1")
    proc = bash('. "$1"; wait_ui "$2" 5 "$3"', env, argv=(UI, DRIVER_PKG, r"+Accepter\s+la\s+course"))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


@case
def test_wait_ui_returns_3_when_foreground_stays_foreign_and_marker_never_matches():
    tmp = tempfile.mkdtemp(prefix="e2e-waitui-foreign-")
    win = write(os.path.join(tmp, "window.txt"), WINDOW_MULTI_DISPLAY.replace(
        DRIVER_PKG, LAUNCHER_PKG))
    xml = write(os.path.join(tmp, "ui.xml"), UI_NO_MARKER_XML)
    env = make_env(tmp, WINDOW_DUMP=win, UI_XML_FIXTURE=xml)
    proc = bash('. "$1"; wait_ui "$2" 3 "$3"', env, argv=(UI, DRIVER_PKG, "+JamaisPresent"))
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------- 3. launch
@case
def test_launch_uses_the_resolved_launcher_component():
    tmp = tempfile.mkdtemp(prefix="e2e-launch-")
    resolve = write(os.path.join(tmp, "resolve.txt"),
                    "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n"
                    f"{DRIVER_PKG}/{DRIVER_PKG}.MainActivity\n")
    env = make_env(tmp, RESOLVE_OUT=resolve)
    script = '. "$1"\neval "$2"\nlaunch "$3"'
    proc = bash(script, env, argv=(UI, extract_fn(RUN, "launch"), DRIVER_PKG))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    log = adb_lines(env)
    assert any("cmd package resolve-activity --brief" in line
               and "-c android.intent.category.LAUNCHER" in line
               and line.endswith(DRIVER_PKG) for line in log), log
    assert any(line == f"shell am start -W -n {DRIVER_PKG}/{DRIVER_PKG}.MainActivity"
               for line in log), log

    # Resolution failure: fall back to monkey, never an `am start` with no component.
    bad = write(os.path.join(tmp, "resolve-bad.txt"), "No activity found\n")
    env2 = make_env(tempfile.mkdtemp(prefix="e2e-launch-bad-"), RESOLVE_OUT=bad)
    proc = bash(script, env2, argv=(UI, extract_fn(RUN, "launch"), DRIVER_PKG))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    log2 = adb_lines(env2)
    assert any(f"shell monkey -p {DRIVER_PKG} -c android.intent.category.LAUNCHER 1" in line
               for line in log2), log2
    assert not any("shell am start" in line for line in log2), log2


# ------------------------------------------- 4. accept credit is attributable
def _responder(code, remark, calls):
    def respond():
        calls["respond"] += 1
        return code, ({"remark": remark} if remark else {"error": "boom"})
    return respond


@case
def test_accepted_via_matrix():
    import offer_flow as f

    cases = [
        ((200, f.ACCEPTED, False, False), "api"),
        ((204, f.ACCEPTED, False, True), "api"),
        ((200, f.ACCEPTED, True, True), "api"),
        ((403, f.NOT_AUTHORIZED, True, True), "ui"),
        ((403, f.NOT_AUTHORIZED, True, False), "unknown"),
        ((403, f.EXPIRED, True, False), "unknown"),
        ((403, f.NOT_AUTHORIZED, False, True), None),
        ((403, f.NOT_AUTHORIZED, False, False), None),
        ((200, f.EXPIRED, False, False), None),
        ((500, f.ACCEPTED, False, False), None),
    ]
    for args, want in cases:
        got = f.accepted_via(*args)
        assert got == want, (args, got, want)


@case
def test_unknown_credit_has_an_explicit_cause_and_never_renews():
    import offer_flow as f

    calls = {"respond": 0, "renew": 0, "sleep": 0}
    out = f.run_accept(_responder(403, f.NOT_AUTHORIZED, calls),
                       lambda: True,
                       lambda: calls.__setitem__("renew", calls["renew"] + 1) or True,
                       lambda _s: calls.__setitem__("sleep", calls["sleep"] + 1),
                       retries=5, delay=1, tap_proven=False)
    assert out["ok"] is True and out["via"] == "unknown", out
    assert out["cause"] == "respond_403_assigned", out
    assert calls == {"respond": 1, "renew": 0, "sleep": 0}, calls

    # Same ride, but the harness proved the tap: ui, not unknown.
    out = f.run_accept(_responder(403, f.NOT_AUTHORIZED, calls),
                       lambda: True,
                       lambda: True,
                       lambda _s: None,
                       retries=5, delay=1, tap_proven=lambda: True)
    assert out == {"ok": True, "via": "ui", "cause": None,
                   "renewed": False, "attempts": 1}, out

    # A 2xx accepted answer stays api even when a tap was also proven.
    out = f.run_accept(_responder(200, f.ACCEPTED, calls),
                       lambda: True, lambda: True, lambda _s: None,
                       retries=5, delay=1, tap_proven=True)
    assert out["ok"] is True and out["via"] == "api" and out["cause"] is None, out


@case
def test_accept_via_unknown_is_never_published_as_pass():
    body = text(RUN)
    assert body.count("mark accept-via PASS") == 1, "un seul PASS accept-via, gardé par api|ui"
    assert re.search(r"api\|ui\)\s*\n\s*mark accept-via PASS", body), \
        "PASS accept-via doit vivre dans la branche api|ui"
    assert re.search(r"unknown\)\s*\n\s*mark accept-via WARN", body), \
        "via=unknown doit être WARN, jamais PASS"
    # api_accept_ok ne retombe plus sur "api" par défaut.
    fn = re.search(r"^api_accept_ok\(\) \{(.*?)^\}", body, re.M | re.S).group(1)
    assert 'get("via") or "unknown"' in fn, fn
    assert 'get("via") or "api"' not in fn, fn

    # Report behaviour: a forged PASS on unknown cannot survive.
    import report

    out = tempfile.mkdtemp(prefix="e2e-accept-guard-")
    write(os.path.join(out, "results-raw.txt"),
          "accept-via|PASS|accept_via=unknown cause=respond_403_assigned\n"
          "no-prod-calls|PASS|0 hit\n")
    res = report.build(out, p0={"no-prod-calls"})
    row = next(p for p in res["points"] if p["point"] == "accept-via")
    assert row["result"] == "WARN", row
    assert "accept_via=unknown" in row["detail"] or "non prouvé" in row["detail"], row
    assert res["verdict"] != "FAIL", res["verdict"]

    # api and ui remain PASS.
    for via in ("api", "ui"):
        out2 = tempfile.mkdtemp(prefix=f"e2e-accept-{via}-")
        write(os.path.join(out2, "results-raw.txt"),
              f"accept-via|PASS|accept_via={via} cause=none\n")
        res2 = report.build(out2, p0=set())
        row2 = next(p for p in res2["points"] if p["point"] == "accept-via")
        assert row2["result"] == "PASS", row2


@case
def test_ui_credit_requires_a_proven_tap():
    """A marker alone proves the screen, never the tap: ui only when tap_proven=1."""
    fn = extract_fn(RUN, "ui_accept_via")
    for proven, want in (("1", "ui"), ("0", "unknown")):
        env = make_env(tempfile.mkdtemp(prefix="e2e-ui-credit-"),
                       UI_TAP_PROVEN=proven)
        proc = bash('eval "$1"\nui_accept_via', env, argv=(fn,))
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert proc.stdout.strip() == want, (proven, proc.stdout, want)

    body = text(RUN)
    assert 'set_accept_via "$(ui_accept_via)"' in body, \
        "la branche succès doit dériver le crédit du tap prouvé"
    assert not re.search(r"set_accept_via\s+ui\b", body), \
        "aucun crédit ui inconditionnel ne doit subsister"


# ------------------------------------------------ 5. Artemis smoke hardening
@case
def test_artemis_run_is_standalone_and_pinned_to_the_device_serial():
    body = text(RUN)
    fn = extract_fn(RUN, "artemis_run")
    assert "--standalone" in fn, fn
    assert '"--device-serial" "$ARTEMIS_SERIAL"' in fn or \
        "--device-serial \"$ARTEMIS_SERIAL\"" in fn, fn
    assert "ARTEMIS_SERIAL=" in body and "ANDROID_SERIAL" in body, body
    assert "adb get-serialno" in body, "série résolue via adb get-serialno"
    assert 'case "$ARTEMIS_SERIAL" in' in body, "validation de la série absente"

    tmp = tempfile.mkdtemp(prefix="e2e-artemis-run-")
    art = os.path.join(tmp, "vendor", "artemis", ".venv", "bin")
    os.makedirs(art)
    args_file = os.path.join(tmp, "args.txt")
    fake = os.path.join(art, "artemis")
    write(fake, "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$ARGS_FILE\"\nexit 0\n")
    os.chmod(fake, 0o755)
    os.makedirs(os.path.join(tmp, "artemis"))
    env = make_env(tmp, ARTEMIS_OK="1", ARTEMIS_SERIAL="emulator-5554",
                   ARGS_FILE=args_file)
    # The function text travels as argv, never through an expanding double quote:
    # `eval "$2"` (not `eval "…$1…"`) keeps every `$name` in the body intact.
    script = ('log() { :; }\nadb() { :; }\neval "$1"\neval "$2"\n'
              'artemis_run smoke "open settings" 5\n')
    proc = bash(script, env, cwd=tmp,
                argv=(fn, extract_fn(RUN, "artemis_serial")))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    args = open(args_file, encoding="utf-8").read().strip()
    assert args.startswith("run open settings --profile flash --test-name smoke"), args
    assert "--standalone" in args, args
    assert "--device-serial emulator-5554" in args, args


@case
def test_publish_artemis_log_masks_and_reaches_both_logs():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-log-")
    os.makedirs(os.path.join(tmp, "artemis"))
    key = "Zx9Qw7ErTy1234567890abcd"
    write(os.path.join(tmp, "artemis", "smoke.log"),
          "INFO llm ready key " + key + "\n"
          "Authorization: Bearer abcdef1234567890\n"
          "offre ride uid 8f3c1d9a4b2e7c55\n"
          "step done\n")
    write(os.path.join(tmp, "run.log"), "")
    env = make_env(tmp, OPENAI_API_KEY=key)
    script = ('. "$1"\neval "$2"\npublish_artemis_log smoke\n')
    proc = bash(script, env, argv=(SMOKE_SH, extract_fn(RUN, "publish_artemis_log")))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    for path in (os.path.join(tmp, "run.log"), env["SMOKE_LOG"]):
        body = open(path, encoding="utf-8").read()
        assert "artemis-log: " in body, (path, body)
        assert "step done" in body, (path, body)
        for leaked in (key, "abcdef1234567890", "8f3c1d9a4b2e7c55"):
            assert leaked not in body, (path, leaked, body)

    # 200-line bound, prefixed, in both destinations.
    write(os.path.join(tmp, "artemis", "big.log"),
          "".join(f"line {i:03d}\n" for i in range(300)))
    write(os.path.join(tmp, "run.log"), "")
    write(env["SMOKE_LOG"], "")
    proc = bash(script.replace("publish_artemis_log smoke",
                               "publish_artemis_log big"), env,
                argv=(SMOKE_SH, extract_fn(RUN, "publish_artemis_log")))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    run_lines = [l for l in open(os.path.join(tmp, "run.log"), encoding="utf-8").read().splitlines()
                 if l.startswith("artemis-log: ")]
    smoke_lines = [l for l in open(env["SMOKE_LOG"], encoding="utf-8").read().splitlines()
                   if l.startswith("artemis-log: ")]
    assert len(run_lines) == 200, len(run_lines)
    assert len(smoke_lines) == 200, len(smoke_lines)
    assert run_lines[0] == "artemis-log: line 100", run_lines[0]


@case
def test_redact_env_masks_the_literal_value():
    secret = "Zx9Qw7ErTy1234567890abcd"
    env = dict(os.environ, E2E_TEST_SECRET=secret)
    proc = subprocess.run(
        [sys.executable, SANITIZE, "--redact-env", "E2E_TEST_SECRET"],
        input=f"provider said {secret} and continued\n", capture_output=True,
        text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert secret not in proc.stdout, proc.stdout
    assert "[REDACTED]" in proc.stdout, proc.stdout
    assert "provider said" in proc.stdout and "continued" in proc.stdout

    # Unset/empty variable: nothing is redacted by that flag.
    env2 = dict(os.environ)
    env2.pop("E2E_TEST_SECRET", None)
    proc2 = subprocess.run(
        [sys.executable, SANITIZE, "--redact-env", "E2E_TEST_SECRET"],
        input="keep this line\n", capture_output=True,
        text=True, env=env2)
    assert proc2.returncode == 0 and proc2.stdout.strip() == "keep this line", proc2.stdout


# ------------------------------- 6. no default display means no answer at all
@case
def test_fg_pkg_never_uses_a_display_that_is_not_zero():
    import fg

    # Named displays, none with id 0 -> every parser refuses to guess.
    assert fg.parse_window(WINDOW_NO_DEFAULT_DISPLAY) == "", \
        "le launcher du display 2 ne doit jamais gagner faute de mDisplayId=0"
    assert fg.parse_activities(ACTIVITIES_NO_DEFAULT_DISPLAY) == "", \
        "le display 2 ne doit jamais tenir lieu de display par défaut"
    assert fg.parse_top(TOP_NO_DEFAULT_DISPLAY) == "", \
        "le display 2 ne doit jamais tenir lieu de display par défaut"
    # A dump with no `Display` header stays one anonymous block, still parsable.
    assert fg.parse_window(
        "  mCurrentFocus=Window{1 u0 " + DRIVER_PKG + "/.MainActivity}") == DRIVER_PKG, \
        "un dump sans en-tête Display reste un bloc anonyme lisible"

    # Behaviour: window has no default display -> activities answers, not the launcher.
    tmp = tempfile.mkdtemp(prefix="e2e-fg-nodefault-")
    win = write(os.path.join(tmp, "window.txt"), WINDOW_NO_DEFAULT_DISPLAY)
    acts = write(os.path.join(tmp, "activities.txt"), ACTIVITIES_FALLBACK)
    env = make_env(tmp, WINDOW_DUMP=win, ACTIVITIES_DUMP=acts)
    proc = bash('. "$1"; fg_pkg', env, argv=(UI,))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == DRIVER_PKG, proc.stdout
    assert LAUNCHER_PKG not in proc.stdout, "le launcher d'un display non-défaut a gagné"
    log = adb_lines(env)
    assert any("dumpsys window displays" in line for line in log), log
    assert any("dumpsys activity activities" in line for line in log), log

    # Nothing to fall back to: UNKNOWN, never the launcher of display 2 or 1.
    lonely = tempfile.mkdtemp(prefix="e2e-fg-nodefault-solo-")
    solo_win = write(os.path.join(lonely, "window.txt"), WINDOW_NO_DEFAULT_DISPLAY)
    env2 = make_env(lonely, WINDOW_DUMP=solo_win)
    proc2 = bash('. "$1"; fg_pkg', env2, argv=(UI,))
    assert proc2.returncode == 0, (proc2.returncode, proc2.stderr)
    assert proc2.stdout.strip() == "UNKNOWN", proc2.stdout


# --------------------------- 7. a successful Artemis smoke is published too
@case
def test_artemis_run_publishes_the_log_on_success():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-success-")
    art = os.path.join(tmp, "vendor", "artemis", ".venv", "bin")
    os.makedirs(art)
    os.makedirs(os.path.join(tmp, "artemis"))
    fake = os.path.join(art, "artemis")
    write(fake, "#!/usr/bin/env bash\n"
                "printf 'INFO smoke line one\\n'\n"
                "printf 'INFO smoke line two\\n'\n"
                "printf 'INFO smoke line three\\n'\n"
                "exit 0\n")
    os.chmod(fake, 0o755)
    # run.sh resolves the sanitizer relative to the repo root; cwd here is `tmp`
    # (the fake vendor tree), so the e2e/ helper is linked in.
    os.symlink(ROOT, os.path.join(tmp, "e2e"))
    write(os.path.join(tmp, "run.log"), "")
    env = make_env(tmp, ARTEMIS_OK="1", ARTEMIS_SERIAL="emulator-5554")
    script = ('log() { :; }\nadb() { :; }\n'
              '. "$1"\neval "$2"\neval "$3"\neval "$4"\n'
              'artemis_run smoke "open settings" 10\n')
    proc = bash(script, env, cwd=tmp,
                argv=(SMOKE_SH, extract_fn(RUN, "artemis_serial"),
                      extract_fn(RUN, "publish_artemis_log"),
                      extract_fn(RUN, "artemis_run")))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    wanted = ["artemis-log: INFO smoke line one",
              "artemis-log: INFO smoke line two",
              "artemis-log: INFO smoke line three"]
    for path in (os.path.join(tmp, "run.log"), env["SMOKE_LOG"]):
        lines = [l for l in open(path, encoding="utf-8").read().splitlines()
                 if l.startswith("artemis-log: ")]
        assert lines == wanted, (path, lines)


# ------------------- 8. one python process per run, byte-identical destinations
@case
def test_publish_artemis_log_uses_a_single_python_process():
    tmp = tempfile.mkdtemp(prefix="e2e-artemis-procs-")
    os.makedirs(os.path.join(tmp, "artemis"))
    write(os.path.join(tmp, "run.log"), "")
    write(os.path.join(tmp, "artemis", "smoke.log"),
          "".join(f"line {i:03d}\n" for i in range(200)))
    calls = os.path.join(tmp, "py-calls.txt")
    wrapper = os.path.join(tmp, "pywrap")
    write(wrapper, "#!/usr/bin/env bash\n"
                   'printf "call\\n" >> "$PY_CALLS"\n'
                   f'exec "{sys.executable}" "$@"\n')
    os.chmod(wrapper, 0o755)
    env = make_env(tmp, PY=wrapper, PY_CALLS=calls)
    proc = bash('. "$1"\neval "$2"\npublish_artemis_log smoke\n', env,
                argv=(SMOKE_SH, extract_fn(RUN, "publish_artemis_log")))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    invocations = (open(calls, encoding="utf-8").read().splitlines()
                   if os.path.exists(calls) else [])
    assert len(invocations) == 1, f"{len(invocations)} processus python pour 200 lignes"
    run_lines = open(os.path.join(tmp, "run.log"), encoding="utf-8").read().splitlines()
    smoke_lines = open(env["SMOKE_LOG"], encoding="utf-8").read().splitlines()
    assert run_lines == smoke_lines, "run.log et le smoke artifact doivent être identiques"
    assert len(run_lines) == 200 and run_lines[0] == "artemis-log: line 000", run_lines[:1]



# ------------------------------------------------------------------ runner
def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test harness
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} focus/attribution cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
