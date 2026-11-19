"""Start the app for real and make sure it stays up.

The unit suite builds the Overlay class inside pytest. This starts it the way a user does
— `python claude_overlay.py`, through the `__main__` block: the crash guard, DPI awareness,
the taskbar / menu-bar identity, the real worker import — on a real desktop session, and
fails if the process exits, writes a crash log, or Tk reports an exception in a callback.
It is what catches "every test passes and the app doesn't open" on the platform the
change was NOT written on.

Nothing needs a `claude` login, and none is used even where there is one: the CLIs'
config folders point at an empty temporary folder (so no stored login is read — on macOS
that also keeps the app away from the keychain), credential variables are dropped from the
environment, and every proxy variable points at a closed local port, so the version check
and anything a CLI tries fail fast instead of leaving the machine. With no usable CLI the
app says so in its chat and keeps running, which is all this asks of it. State, config and
the crash log go to the same temporary folder.

    python .github/scripts/smoke_launch.py [seconds]      (CI only; --force to run locally)

tests/test_smoke_launch.py runs this on CI, so every workflow that runs pytest runs it.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BAD = ("Exception in Tkinter callback", "Fatal Python error")
SECRET_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_", "CODEX_", "AWS_", "GOOGLE_")
NOWHERE = "http://127.0.0.1:9"          # the discard port: refused at once


def _isolated_env(tmp, cfg):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(SECRET_PREFIXES)}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        for key in [k for k in env if k.upper() == name]:
            del env[key]                 # one spelling each: Windows' names ignore case
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = NOWHERE
        if os.name != "nt":
            env[name.lower()] = NOWHERE
    env.update(CLAUDE_OVERLAY_CONFIG=cfg, LOCALAPPDATA=tmp,
               CLAUDE_CONFIG_DIR=os.path.join(tmp, "claude-config"),
               CODEX_HOME=os.path.join(tmp, "codex-home"),
               DO_NOT_TRACK="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return env


def main(argv):
    args = [a for a in argv if a != "--force"]
    if not os.environ.get("CI") and "--force" not in argv:
        print("smoke_launch: opens the real app window; meant for CI. Pass --force to run it here.")
        return 2
    seconds = float(args[0]) if args else 15.0
    # The app's output and a crash log quote source lines full of arrows, emoji and CJK; a
    # runner's console codec must not turn the report of a failure into a failure to report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
    # ignore_cleanup_errors: on Windows a helper the app started (the PowerShell that makes
    # its Start Menu shortcut) can still hold a file in here when we are done.
    with tempfile.TemporaryDirectory(prefix="overlay-smoke-", ignore_cleanup_errors=True) as tmp:
        cfg = os.path.join(tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"PERMISSION_MODE": "plan", "CLI_UPDATE_CHECK": False,
                       "CLI_AUTO_UPDATE": False, "OVERLAY_AUTO_UPDATE": False,
                       "TELEMETRY": False}, f)
        env = _isolated_env(tmp, cfg)
        log = os.path.join(tmp, "output.txt")
        crash = os.path.join(tmp, "claude-overlay", "crash.log")
        problems = []
        with open(log, "wb") as out:
            proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "claude_overlay.py")],
                                    cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
            started = time.monotonic()
            try:
                while time.monotonic() - started < seconds:
                    if proc.poll() is not None:
                        problems.append(f"the app exited by itself after "
                                        f"{time.monotonic() - started:.1f}s (exit code {proc.returncode})")
                        break
                    time.sleep(0.25)
            finally:
                if proc.poll() is None:
                    if os.name == "nt":      # the whole tree: its child processes too
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                       capture_output=True)
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)
        with open(log, "rb") as f:
            text = f.read().decode("utf-8", "replace")
        if os.path.exists(crash):
            problems.append("the app wrote a crash log:\n" + open(crash, encoding="utf-8",
                                                                   errors="replace").read())
        for marker in BAD:
            if marker in text:
                problems.append(f"its output contains {marker!r}")
        print("----- app output -----")
        print(text.strip() or "(none)")
        print("----------------------")
        if problems:
            for p in problems:
                print("FAIL:", p)
            return 1
        print(f"OK: the app started and ran for {seconds:.0f}s on {sys.platform} "
              f"(Python {sys.version.split()[0]}).")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
