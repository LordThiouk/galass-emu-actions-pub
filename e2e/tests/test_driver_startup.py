#!/usr/bin/env python3
"""TDD spec for the Driver startup hardening (run 40375590679).

No emulator, no network, python3 only. Pins:
  * ISC-1 `grant_app_permissions` (run.sh): runtime permissions, background appops
    and the battery-optimisation exemption are granted BEFORE the first Driver
    launch, and the deviceidle whitelist is READ BACK (`user,pkg,uid`);
  * ISC-2 `e2e/fg.py --overlay` / `--component`: a known system overlay exits 0,
    any normal app (launcher3 included) exits 1, and the focused component of the
    default display is named for the journal;
  * ISC-3 `close_system_overlay`: BACK, then `am force-stop` if it resists, at most
    3 attempts, rc=0 only when no overlay is left — and no input at all on a
    normal app;
  * ISC-4 `launch_fg`: `am start -W -n <resolved component>`, rc=0 ONLY when
    `fg_pkg` is the package, at most 2 launches;
  * ISC-5 `wait_app_ui`: loops `wait_ui` in slices, closes overlays, relaunches an
    app that lost the foreground, rc=0 on the marker and 3 otherwise; run.sh
    chains grant -> launch_fg -> wait_app_ui before `snap 33-driver-home-online`.

The battery dialog that stole the foreground belongs to com.android.settings: the
harness closes it, it never learns to call it a pass.
"""
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
UI = os.path.join(ROOT, "ui.sh")
FG = os.path.join(ROOT, "fg.py")
sys.path.insert(0, ROOT)

DRIVER_PKG = "sn.galass.galassdriver"
RIDER_PKG = "sn.galass.galassrider"
LAUNCHER_PKG = "com.android.launcher3"
SETTINGS_PKG = "com.android.settings"
PERMISSION_PKG = "com.android.permissioncontroller"
GOOGLE_PERMISSION_PKG = "com.google.android.permissioncontroller"

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def text(path):
    return open(path, encoding="utf-8").read()


def write(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


# ---------------------------------------------------------------- fixtures
# `dumpsys window displays` for display 0, the only display a caller may trust.
WINDOW_SETTINGS = f"""\
WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=0
    init=1080x2340 420dpi cur=1080x2340 app=1080x2138
    mCurrentFocus=Window{{a1b2c3d u0 {SETTINGS_PKG}/.Settings$HighPowerApplicationsActivity}}
    mFocusedApp=ActivityRecord{{d4e5f6a u0 {SETTINGS_PKG}/.Settings t42}}
"""

WINDOW_DRIVER = f"""\
WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=0
    init=1080x2340 420dpi cur=1080x2340 app=1080x2138
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

# `dumpsys activity activities` still names the focused component when the window
# dump is empty: the fallback chain must use it instead of giving up.
ACTIVITIES_SETTINGS = f"""\
ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
  Display #0 (activities from top to bottom):
    * Task{{4f1c2b0 #42 type=standard A=10234:{SETTINGS_PKG} U=0 visible=true}}
      mResumedActivity: ActivityRecord{{d4e5f6a u0 {SETTINGS_PKG}/.Settings$HighPowerApplicationsActivity t42}}
"""

UI_ONLINE_XML = f"""\
<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="{DRIVER_PKG}" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[0,0][1080,2340]">
    <node index="0" text="En ligne" resource-id="" class="android.widget.Switch" package="{DRIVER_PKG}" content-desc="" checkable="true" checked="true" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[40,200][400,260]" />
    <node index="1" text="Aucune offre en attente" resource-id="" class="android.widget.TextView" package="{DRIVER_PKG}" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[40,300][900,360]" />
  </node>
</hierarchy>
"""

UI_NO_MARKER_XML = f"""\
<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="{DRIVER_PKG}" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[0,0][1080,2340]">
    <node index="0" text="Chargement" resource-id="" class="android.widget.TextView" package="{DRIVER_PKG}" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" visible-to-user="true" bounds="[40,120][900,180]" />
  </node>
</hierarchy>
"""

# One stub `adb` for the whole suite. `FG_STATE` is the package currently focused;
# `FG_SEQ` is the queue of focus changes a key press / a force-stop may trigger, so
# "the BACK key closes the dialog" and "the dialog resists" are both expressible.
STUB_ADB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$ADB_LOG"

_pop_fg() {
  [ -n "${FG_SEQ:-}" ] || return 0
  [ -s "$FG_SEQ" ] || return 0
  head -1 "$FG_SEQ" > "$FG_STATE"
  tail -n +2 "$FG_SEQ" > "$FG_SEQ.tmp" && mv "$FG_SEQ.tmp" "$FG_SEQ"
}

_emit_fg() {
  local pkg=""
  [ -n "${FG_STATE:-}" ] && [ -s "$FG_STATE" ] && pkg=$(head -1 "$FG_STATE")
  [ -z "$pkg" ] && return 0
  printf '  Display: mDisplayId=0\\n'
  printf '    mCurrentFocus=Window{1 u0 %s/%s.MainActivity}\\n' "$pkg" "$pkg"
  printf '    mFocusedApp=ActivityRecord{1 u0 %s/.MainActivity t42}\\n' "$pkg"
}

case "$*" in
  *"dumpsys window displays"*)
    if [ -n "${WINDOW_OUT:-}" ]; then cat "$WINDOW_OUT"; else _emit_fg; fi ;;
  *"dumpsys activity activities"*) [ -n "${ACTIVITIES_OUT:-}" ] && cat "$ACTIVITIES_OUT" ;;
  *"dumpsys activity top"*) : ;;
  *"cmd package resolve-activity"*) [ -n "${RESOLVE_OUT:-}" ] && cat "$RESOLVE_OUT" ;;
  *"cmd deviceidle whitelist +"*) : ;;
  *"cmd deviceidle whitelist"*) [ -n "${DEVICEIDLE_OUT:-}" ] && cat "$DEVICEIDLE_OUT" ;;
  *"input keyevent 4"*) [ "${POP_ON:-any}" != "stop" ] && _pop_fg ;;
  *"am force-stop"*) [ "${POP_ON:-any}" != "back" ] && _pop_fg ;;
  *"am start"*) [ -n "${AM_START_FG:-}" ] && printf '%s\\n' "$AM_START_FG" > "$FG_STATE" ;;
  *"uiautomator dump"*) printf 'UI hierarchy dumped to: /sdcard/w.xml\\n' ;;
  *"cat /sdcard/w.xml"*) [ -n "${UI_XML_FIXTURE:-}" ] && cat "$UI_XML_FIXTURE" ;;
esac
exit 0
"""


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
        "LOG_FILE": os.path.join(tmp, "log.txt"),
        # Tests must not pay for real settle/poll sleeps — always bounded by tmo.
        "OVERLAY_SETTLE_SEC": "0",
        "FG_POLL_SEC": "1",
        "WAIT_APP_UI_RELAUNCH_SEC": "2",
    })
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def focus_env(tmp, current, queue=(), **extra):
    """Focus the stub `adb` reports, plus the queue of focus changes after input."""
    state = write(os.path.join(tmp, "fg.state"), current + "\n")
    seq = write(os.path.join(tmp, "fg.seq"), "".join(p + "\n" for p in queue))
    return make_env(tmp, FG_STATE=state, FG_SEQ=seq, **extra)


def resolve_fixture(tmp, pkg=DRIVER_PKG):
    return write(os.path.join(tmp, "resolve.txt"),
                 "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n"
                 f"{pkg}/{pkg}.MainActivity\n")


def bash(script, env, cwd=None, argv=()):
    return subprocess.run(["bash", "-c", script, "_", *argv],
                          capture_output=True, text=True, env=env, cwd=cwd)


def extract_fn(path, name):
    """Return the whole `name() { ... }` definition (brace counting, not `^}$`)."""
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
    return [line.strip() for line in text(path).splitlines() if line.strip()]


def log_lines(env):
    """Lines written by run.sh's `log` stub (LOG_FILE)."""
    path = env["LOG_FILE"]
    if not os.path.exists(path):
        return []
    return [line.strip() for line in text(path).splitlines() if line.strip()]


def journal_lines(env):
    """Lines written by ui.sh's `_ulog` (always $OUT/run.log)."""
    path = os.path.join(env["OUT"], "run.log")
    if not os.path.exists(path):
        return []
    return [line.strip() for line in text(path).splitlines() if line.strip()]


def ui_run(env, snippet, argv=()):
    """Source ui.sh, then run `snippet` (ui.sh owns the adb wrapper + journals)."""
    return bash('. "$1"\n' + snippet, env, argv=(UI,) + tuple(argv))


def fn_run(env, snippet, fns, *args):
    """Source ui.sh, eval the extracted helpers, then run `snippet` ($PKG = args[0])."""
    script = '. "$1"\n'
    for i in range(2, len(fns) + 2):
        script += 'eval "${%d}"\n' % i
    script += 'PKG="${%d:-}"\n' % (len(fns) + 2)
    script += snippet
    return bash(script, env, argv=(UI,) + tuple(fns) + tuple(args))


# --------------------------------------------------- ISC-2 : fg.py classification
@case
def test_fg_overlay_classifies_known_system_overlays_only():
    cases = [
        (SETTINGS_PKG, 0),
        (PERMISSION_PKG, 0),
        (GOOGLE_PERMISSION_PKG, 0),
        (LAUNCHER_PKG, 1),
        (DRIVER_PKG, 1),
        ("", 1),
    ]
    for pkg, want in cases:
        proc = subprocess.run([sys.executable, FG, "--overlay", pkg],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == want, (pkg, proc.returncode, want, proc.stderr)


@case
def test_fg_overlay_flag_never_needs_a_dump_on_stdin():
    """`--overlay` answers from the argument alone: no stdin, no hang."""
    proc = subprocess.run([sys.executable, FG, "--overlay", SETTINGS_PKG],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)


@case
def test_fg_component_names_the_focused_activity_of_display_zero():
    cases = [
        (WINDOW_SETTINGS, f"{SETTINGS_PKG}/.Settings$HighPowerApplicationsActivity"),
        (WINDOW_DRIVER, f"{DRIVER_PKG}/{DRIVER_PKG}.MainActivity"),
    ]
    for dump, want in cases:
        proc = subprocess.run([sys.executable, FG, "--component", "--source", "window"],
                              input=dump, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert proc.stdout.strip() == want, (proc.stdout, want)

    proc = subprocess.run([sys.executable, FG, "--component", "--source", "window"],
                          input=WINDOW_NO_FOCUS, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, (proc.returncode, proc.stdout)
    assert proc.stdout.strip() == "", proc.stdout


@case
def test_fg_parse_path_and_options_are_untouched():
    """The pre-existing contract (stdin dump -> package, --source) still holds."""
    for dump, source in ((WINDOW_DRIVER, "window"), (WINDOW_SETTINGS, "auto")):
        proc = subprocess.run([sys.executable, FG, "--source", source],
                              input=dump, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        want = DRIVER_PKG if source == "window" else SETTINGS_PKG
        assert proc.stdout.strip() == want, (source, proc.stdout, want)


@case
def test_fg_component_falls_back_to_the_activities_dump_when_window_is_empty():
    # fg.py itself reads an activities dump when told so…
    proc = subprocess.run([sys.executable, FG, "--component", "--source", "activities"],
                          input=ACTIVITIES_SETTINGS, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == \
        f"{SETTINGS_PKG}/.Settings$HighPowerApplicationsActivity", proc.stdout
    # …and a window dump with no focus yields nothing, never a guess.
    proc = subprocess.run([sys.executable, FG, "--component", "--source", "window"],
                          input=WINDOW_NO_FOCUS, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 1, (proc.returncode, proc.stdout)
    assert proc.stdout.strip() == "", proc.stdout

    # ui.sh chains window -> activities: no component in window must not end the search.
    tmp = tempfile.mkdtemp(prefix="e2e-fgcomponent-fallback-")
    win = write(os.path.join(tmp, "window.txt"), WINDOW_NO_FOCUS)
    acts = write(os.path.join(tmp, "activities.txt"), ACTIVITIES_SETTINGS)
    env = focus_env(tmp, DRIVER_PKG, WINDOW_OUT=win, ACTIVITIES_OUT=acts)
    proc = fn_run(env, 'fg_component', [extract_fn(UI, "fg_component")])
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == \
        f"{SETTINGS_PKG}/.Settings$HighPowerApplicationsActivity", proc.stdout
    log = adb_lines(env)
    dumps = [line for line in log if "dumpsys window displays" in line
             or "dumpsys activity activities" in line]
    assert dumps == ["shell dumpsys window displays", "shell dumpsys activity activities"], \
        dumps


# ------------------------------ ISC-3 : close_system_overlay (overlay closing)
@case
def test_close_system_overlay_presses_back_and_journals_the_component():
    tmp = tempfile.mkdtemp(prefix="e2e-overlay-back-")
    env = focus_env(tmp, SETTINGS_PKG, queue=(DRIVER_PKG,))
    proc = fn_run(env, 'close_system_overlay',
                  [extract_fn(UI, "close_system_overlay"), extract_fn(UI, "is_system_overlay"),
                   extract_fn(UI, "fg_component")])
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    log = adb_lines(env)
    assert any("shell input keyevent 4" == line for line in log), log
    assert not any("am force-stop" in line for line in log), log
    journal = "\n".join(journal_lines(env))
    assert SETTINGS_PKG in journal, journal
    assert "close_system_overlay" in journal, journal


@case
def test_close_system_overlay_force_stops_an_overlay_that_resists_back():
    tmp = tempfile.mkdtemp(prefix="e2e-overlay-resist-")
    env = focus_env(tmp, SETTINGS_PKG, queue=(DRIVER_PKG,), POP_ON="stop")
    proc = fn_run(env, 'close_system_overlay',
                  [extract_fn(UI, "close_system_overlay"), extract_fn(UI, "is_system_overlay"),
                   extract_fn(UI, "fg_component")])
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    log = adb_lines(env)
    assert any("shell input keyevent 4" == line for line in log), log
    assert any(line == f"shell am force-stop {SETTINGS_PKG}" for line in log), log


@case
def test_close_system_overlay_gives_up_after_three_attempts():
    tmp = tempfile.mkdtemp(prefix="e2e-overlay-stuck-")
    env = focus_env(tmp, SETTINGS_PKG)  # nothing pops: the overlay stays
    proc = fn_run(env, 'close_system_overlay',
                  [extract_fn(UI, "close_system_overlay"), extract_fn(UI, "is_system_overlay"),
                   extract_fn(UI, "fg_component")])
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    log = adb_lines(env)
    backs = [line for line in log if line == "shell input keyevent 4"]
    stops = [line for line in log if line.startswith("shell am force-stop")]
    assert 1 <= len(backs) <= 3, backs
    assert 1 <= len(stops) <= 3, stops


@case
def test_close_system_overlay_is_a_no_op_on_a_normal_app():
    for pkg in (DRIVER_PKG, RIDER_PKG, LAUNCHER_PKG):
        tmp = tempfile.mkdtemp(prefix="e2e-overlay-noop-")
        env = focus_env(tmp, pkg)
        proc = fn_run(env, 'close_system_overlay', CLOSE_FNS)
        assert proc.returncode == 0, (pkg, proc.returncode, proc.stderr)
        log = adb_lines(env)
        assert not any("input keyevent" in line for line in log), (pkg, log)
        assert not any("am force-stop" in line for line in log), (pkg, log)


# ---------------------------------------- ISC-4 : launch_fg proves the foreground
@case
def test_launch_fg_returns_zero_only_when_the_package_became_foreground():
    tmp = tempfile.mkdtemp(prefix="e2e-launchfg-ok-")
    env = focus_env(tmp, LAUNCHER_PKG, RESOLVE_OUT=resolve_fixture(tmp),
                    AM_START_FG=DRIVER_PKG)
    proc = fn_run(env, 'launch_fg "$PKG" 10', LAUNCH_FNS, DRIVER_PKG)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    log = adb_lines(env)
    starts = [line for line in log if "am start" in line]
    assert starts == [f"shell am start -W -n {DRIVER_PKG}/{DRIVER_PKG}.MainActivity"], log
    assert any("cmd package resolve-activity --brief" in line for line in log), log


@case
def test_launch_fg_never_returns_zero_on_a_foreign_foreground_and_launches_at_most_twice():
    tmp = tempfile.mkdtemp(prefix="e2e-launchfg-fail-")
    env = focus_env(tmp, LAUNCHER_PKG, RESOLVE_OUT=resolve_fixture(tmp))
    t0 = time.time()
    proc = fn_run(env, 'launch_fg "$PKG" 4', LAUNCH_FNS, DRIVER_PKG)
    elapsed = time.time() - t0
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    log = adb_lines(env)
    starts = [line for line in log if "am start" in line]
    assert 1 <= len(starts) <= 2, starts
    assert elapsed < 30, f"launch_fg a dépassé sa borne ({elapsed:.1f}s)"
    assert any("launch_fg" in line and DRIVER_PKG in line for line in journal_lines(env)), \
        journal_lines(env)


@case
def test_launch_fg_closes_a_system_overlay_and_then_proves_the_foreground():
    tmp = tempfile.mkdtemp(prefix="e2e-launchfg-overlay-")
    env = focus_env(tmp, SETTINGS_PKG, queue=(DRIVER_PKG,),
                    RESOLVE_OUT=resolve_fixture(tmp))
    proc = fn_run(env, 'launch_fg "$PKG" 10', LAUNCH_FNS, DRIVER_PKG)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    log = adb_lines(env)
    assert any(line == "shell input keyevent 4" for line in log), log
    assert len([line for line in log if "am start" in line]) == 1, log


# ------------------------------- ISC-5 : wait_app_ui (marker, slices, relaunch)
@case
def test_wait_app_ui_returns_zero_on_the_online_marker_fixture():
    tmp = tempfile.mkdtemp(prefix="e2e-waitappui-online-")
    xml = write(os.path.join(tmp, "online.xml"), UI_ONLINE_XML)
    env = focus_env(tmp, DRIVER_PKG, UI_XML_SRC=xml,
                    UI_XML=os.path.join(tmp, "w.xml"), E2E_ALLOW_FIXTURE="1")
    proc = fn_run(env, 'wait_app_ui "$PKG" 10 "+En ligne"', WAIT_FNS, DRIVER_PKG)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert not any("am start" in line for line in adb_lines(env)), \
        "un marqueur présent ne doit jamais déclencher de relance"


@case
def test_wait_app_ui_relaunches_an_app_that_lost_the_foreground_and_returns_3():
    tmp = tempfile.mkdtemp(prefix="e2e-waitappui-relaunch-")
    xml = write(os.path.join(tmp, "nomarker.xml"), UI_NO_MARKER_XML)
    env = focus_env(tmp, LAUNCHER_PKG, UI_XML_SRC=xml,
                    UI_XML=os.path.join(tmp, "w.xml"), E2E_ALLOW_FIXTURE="1",
                    RESOLVE_OUT=resolve_fixture(tmp), AM_START_FG=DRIVER_PKG,
                    WAIT_APP_UI_SLICE="2")
    proc = fn_run(env, 'wait_app_ui "$PKG" 8 "+En ligne"', WAIT_FNS, DRIVER_PKG)
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert any("am start" in line for line in adb_lines(env)), adb_lines(env)


@case
def test_wait_app_ui_returns_3_only_when_the_marker_never_matched():
    tmp = tempfile.mkdtemp(prefix="e2e-waitappui-timeout-")
    xml = write(os.path.join(tmp, "nomarker.xml"), UI_NO_MARKER_XML)
    env = focus_env(tmp, DRIVER_PKG, UI_XML_SRC=xml,
                    UI_XML=os.path.join(tmp, "w.xml"), E2E_ALLOW_FIXTURE="1")
    proc = fn_run(env, 'wait_app_ui "$PKG" 3 "+En ligne"', WAIT_FNS, DRIVER_PKG)
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)


# --------------------------- ISC-1 : grant_app_permissions before the first launch
@case
def test_grant_app_permissions_grants_permissions_appops_and_battery_exemption():
    tmp = tempfile.mkdtemp(prefix="e2e-grant-")
    whitelist = write(os.path.join(tmp, "whitelist.txt"),
                      f"user,{DRIVER_PKG},10234\n")
    env = make_env(tmp, DEVICEIDLE_OUT=whitelist)
    proc = bash('. "$1"\nlog() { printf "%s\\n" "$*" >> "$LOG_FILE"; }\n'
                'eval "$2"\ngrant_app_permissions "$3"',
                env, argv=(UI, extract_fn(RUN, "grant_app_permissions"), DRIVER_PKG))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    log = adb_lines(env)
    for perm in ("POST_NOTIFICATIONS", "ACCESS_FINE_LOCATION",
                 "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION"):
        assert any(line == f"shell pm grant {DRIVER_PKG} android.permission.{perm}"
                   for line in log), (perm, log)
    for op in ("RUN_IN_BACKGROUND", "RUN_ANY_IN_BACKGROUND"):
        assert any(line == f"shell appops set {DRIVER_PKG} {op} allow" for line in log), \
            (op, log)
    assert any(line == f"shell cmd deviceidle whitelist +{DRIVER_PKG}" for line in log), log
    assert any(line == "shell cmd deviceidle whitelist" for line in log), \
        "l'exemption batterie doit être RELUE, jamais supposée"
    journal = "\n".join(log_lines(env))
    assert f"battery exemption: {DRIVER_PKG} (ok)" in journal, journal


@case
def test_grant_app_permissions_reads_back_the_real_whitelist_shape():
    # `cmd deviceidle whitelist` prints `user,<pkg>,<uid>`; anything without the
    # package is a READBACK FAILED, never a silent success.
    cases = [
        (f"user,{DRIVER_PKG},10234\n", "ok"),
        (f"user,com.other.app,10100\nuser,{DRIVER_PKG},10234\n", "ok"),
        ("user,com.other.app,10100\n", "READBACK FAILED"),
        ("", "READBACK FAILED"),
        (f"user,com.other.{DRIVER_PKG},10234\n", "READBACK FAILED"),
    ]
    for content, want in cases:
        tmp = tempfile.mkdtemp(prefix="e2e-grant-readback-")
        whitelist = write(os.path.join(tmp, "whitelist.txt"), content)
        env = make_env(tmp, DEVICEIDLE_OUT=whitelist)
        proc = bash('. "$1"\nlog() { printf "%s\\n" "$*" >> "$LOG_FILE"; }\n'
                    'eval "$2"\ngrant_app_permissions "$3"',
                    env, argv=(UI, extract_fn(RUN, "grant_app_permissions"), DRIVER_PKG))
        assert proc.returncode == 0, (content, proc.returncode, proc.stderr)
        journal = "\n".join(log_lines(env))
        assert f"battery exemption: {DRIVER_PKG} ({want})" in journal, (content, journal)


@case
def test_grant_app_permissions_reads_both_real_whitelist_shapes_exactly():
    # `cmd deviceidle whitelist` prints either `user,<pkg>,<uid>` or `<pkg>,<uid>`
    # at the head of the line. The read-back is EXACT: a dot in the package name
    # is never an ERE wildcard, and a longer sibling name never matches.
    cases = [
        (f"user,{DRIVER_PKG},10234\n", "ok"),
        (f"{DRIVER_PKG},10234\n", "ok"),
        (f"user,foo.{DRIVER_PKG},10234\n", "READBACK FAILED"),
        (f"user,{DRIVER_PKG}2,10234\n", "READBACK FAILED"),
        # Old `grep -E "(^|,)$pkg,"` treated the dots as jokers and said ok here.
        ("user,snXgalassXgalassdriver,10234\n", "READBACK FAILED"),
        ("snXgalassXgalassdriver,10234\n", "READBACK FAILED"),
    ]
    for content, want in cases:
        tmp = tempfile.mkdtemp(prefix="e2e-grant-whitelist-shapes-")
        whitelist = write(os.path.join(tmp, "whitelist.txt"), content)
        env = make_env(tmp, DEVICEIDLE_OUT=whitelist)
        proc = bash('. "$1"\nlog() { printf "%s\\n" "$*" >> "$LOG_FILE"; }\n'
                    'eval "$2"\ngrant_app_permissions "$3"',
                    env, argv=(UI, extract_fn(RUN, "grant_app_permissions"), DRIVER_PKG))
        assert proc.returncode == 0, (content, proc.returncode, proc.stderr)
        journal = "\n".join(log_lines(env))
        assert f"battery exemption: {DRIVER_PKG} ({want})" in journal, (content, journal)


@case
def test_run_sh_grants_before_the_first_driver_launch():
    body = text(RUN)
    i_install = body.index('adb install -r -t -g "$APK_DRIVER"')
    i_grant = body.index('grant_app_permissions "$DRIVER_PKG"')
    i_launch = body.index('launch_fg "$DRIVER_PKG"')
    i_wait = body.index('wait_app_ui "$DRIVER_PKG"')
    i_snap = body.index("snap 33-driver-home-online")
    assert i_install < i_grant < i_launch < i_wait < i_snap, \
        (i_install, i_grant, i_launch, i_wait, i_snap)
    assert not re.search(r'(?m)^\s*launch "\$DRIVER_PKG"', body), \
        "le Driver ne doit plus être lancé sans preuve de premier plan"
    fn = extract_fn(RUN, "grant_app_permissions")
    assert "cmd deviceidle whitelist" in fn, "l'exemption batterie doit être relue"
    assert "pm grant" in fn and "appops set" in fn, fn


@case
def test_driver_block_waits_on_the_unchanged_online_marker():
    body = text(RUN)
    assert "MARK_driver_home_online='+En ligne'" in body, \
        "le marqueur « En ligne » ne doit pas être affaibli"
    assert re.search(r'wait_app_ui "\$DRIVER_PKG" "\$\{DRIVER_ONLINE_WAIT_SEC:-120\}" '
                     r'"\$MARK_driver_home_online"', body), body
    assert "${DRIVER_ONLINE_WAIT_SEC:-120}" in body, \
        "la borne ~120 s doit être surchargeable"
    # PASS/WARN du point inchangés.
    assert re.search(r'mark driver-home-online PASS "ui cap=33-driver-home-online\.png"', body)
    assert re.search(r'mark driver-home-online WARN "driver home sans « En ligne » \(33\)"', body)
    assert "driver-home-online" not in re.search(r'P0_POINTS="([^"]+)"', body, re.S).group(1)


# ------------------------------------------------------- F2 : bring_fg hardening
@case
def test_bring_fg_closes_overlays_and_keeps_its_monkey_fallback():
    fn = extract_fn(RUN, "bring_fg")
    assert "close_system_overlay" in fn, fn
    assert "monkey" in fn, fn
    assert fn.index("close_system_overlay") < fn.index("monkey"), \
        "les overlays doivent être refermés AVANT la bascule"


CLOSE_FNS = tuple(extract_fn(UI, name) for name in
                  ("close_system_overlay", "is_system_overlay", "fg_component"))
LAUNCH_FNS = tuple(extract_fn(UI, name) for name in
                   ("launch_fg", "is_system_overlay", "fg_component",
                    "launcher_component", "close_system_overlay"))
WAIT_FNS = tuple(extract_fn(UI, name) for name in
                 ("wait_app_ui", "close_system_overlay", "launch_fg",
                  "is_system_overlay", "fg_component", "launcher_component"))

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
    print(f"\n{len(CASES) - failed}/{len(CASES)} driver-startup cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
