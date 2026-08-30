# -*- coding: utf-8 -*-
"""Answer "why won't it start?" in one screen, before the user has to ask anyone.

WHY THIS EXISTS
Every way the overlay fails to launch on someone else's machine looks identical from the
outside — you double-click, nothing appears. The causes are not identical, and they are
all boring:

  * the packages went missing or were half-upgraded (an interrupted / proxy-blocked
    `pip install --upgrade` can leave Pillow uninstalled and nothing else);
  * there are TWO Pythons, and the one `pythonw` resolves to is not the one the packages
    were installed into;
  * claude-agent-sdk is older than the code (the overlay imports StreamEvent, which the
    SDK only started exporting in 0.1.49 — an older SDK is an ImportError at line 1);
  * only `claude_overlay.py` was replaced during a manual ZIP "update", so the sibling
    modules it now imports aren't there;
  * Python was installed without Tk, or the Microsoft Store alias stub is what `pythonw`
    actually finds.

Each of those has a different one-line fix and none of them is guessable from "nothing
happened". This module names which one it is.

It is used from three places: the startup import guard (so the dialog says what to do,
not just what broke), `Diagnose.cmd` (so a colleague can send back one readable report),
and the end of `update.cmd` (so a broken update is caught before the next double-click).

Leaf module: stdlib only, and it must run to completion on a machine where the app's own
dependencies are missing — so it imports nothing from the project at module level.
"""

import os
import subprocess
import sys

MIN_PYTHON = (3, 10)

# Fallback if worker.py can't be parsed. The live list is derived from worker.py itself
# (see required_sdk_symbols) so this check can never drift away from the actual import.
_FALLBACK_SDK_SYMBOLS = (
    "ClaudeSDKClient", "ClaudeAgentOptions", "AssistantMessage", "TextBlock",
    "ToolUseBlock", "ResultMessage", "StreamEvent", "PermissionResultAllow",
)

FAIL, WARN = "FAIL", "WARN"


class Problem:
    """One finding: what's wrong, and the single command that fixes it."""

    def __init__(self, level, key, what, fix=""):
        self.level, self.key, self.what, self.fix = level, key, what, fix

    def __repr__(self):                                        # pragma: no cover - debug aid
        return f"<Problem {self.level} {self.key}>"

    def text(self):
        return f"[{self.level}] {self.what}" + (f"\n       fix: {self.fix}" if self.fix else "")


def repo_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _console_twin(interpreter):
    """The python.exe sitting NEXT TO a pythonw.exe — same install, same site-packages,
    but WITH a console. The app runs under pythonw, so sys.executable here usually IS
    pythonw — and advice that says "run pythonw -m pip ..." is advice nobody can follow:
    pythonw shows no window and prints nothing, so to the person pasting the command the
    fix itself looks broken. When the interpreter isn't pythonw, or the sibling isn't
    there, the interpreter itself is still the right target — a silent install into the
    right Python beats a visible one into the wrong Python."""
    try:
        if os.path.basename(interpreter).lower() == "pythonw.exe":
            cand = os.path.join(os.path.dirname(interpreter), "python.exe")
            if os.path.isfile(cand):
                return cand
    except Exception:                                          # pragma: no cover - defensive
        pass
    return interpreter


def pip_command(interpreter=None):
    """The install command for THIS interpreter. Spelled with a full path rather than
    a bare `pip`, because "I already installed it" almost always means "into the other
    Python" — this is the only form that can't land in the wrong one. The path is the
    console twin of the interpreter (see _console_twin): same environment, visible output.

    Installs from requirements.txt rather than naming the packages, so following this
    advice cannot land a version the app was never tested against. Naming them here is how
    the pinned file ended up constraining nobody."""
    return '"%s" -m pip install --upgrade -r "%s"' % (
        _console_twin(interpreter or sys.executable),
        os.path.join(repo_dir(), "requirements.txt"))


def required_sdk_symbols():
    """The names worker.py imports from claude_agent_sdk WITHOUT a try/except.

    Read out of worker.py's AST rather than duplicated here on purpose: a copied list is
    a second source of truth that silently goes stale the day someone adds an import, and
    a stale preflight reports "environment fine" for a crash it was written to catch.
    Only unguarded imports count — the ones inside try/except already degrade by design."""
    try:
        import ast
        src = open(os.path.join(repo_dir(), "worker.py"), "r", encoding="utf-8").read()
        tree = ast.parse(src)
        names = []
        for node in tree.body:                       # module level only: a try/except at
            if isinstance(node, ast.ImportFrom):     # this level is a GUARDED import, and
                if node.module == "claude_agent_sdk":  # ast.Try is not an ImportFrom, so it
                    names += [a.name for a in node.names]   # is skipped for free
        return tuple(names) or _FALLBACK_SDK_SYMBOLS
    except Exception:
        return _FALLBACK_SDK_SYMBOLS


def _dist_version(dist):
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version(dist)
        except PackageNotFoundError:
            return None
    except Exception:                                          # pragma: no cover - defensive
        return None


def _scan_programs_python(filename, _depth=6):
    """The launcher's SECOND candidate source, emulated: the first `filename` found under
    %LOCALAPPDATA%\\Programs\\Python — the folder setup.cmd installs into — walking the
    way `dir /b /s /a-d /o-n` walks it (descending name order, files of a directory
    before its subdirectories). The launcher probes each candidate by running it; this
    feeds a WARN, so existing is enough. Depth-capped because uv creates junctions in
    that tree and a cyclic one would otherwise walk forever."""
    root = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs", "Python")

    def first(d, depth):
        if depth <= 0:
            return None
        try:
            entries = sorted(os.listdir(d), key=str.lower, reverse=True)
        except OSError:
            return None
        cand = os.path.join(d, filename)
        if any(e.lower() == filename for e in entries) and os.path.isfile(cand):
            return cand
        for e in entries:
            sub = os.path.join(d, e)
            if os.path.isdir(sub):
                hit = first(sub, depth - 1)
                if hit:
                    return hit
        return None

    return first(root, _depth) if os.path.isdir(root) else None


def launcher_python():
    """The interpreter "Start Claude Overlay.cmd" will actually run the app with, found
    the way the LAUNCHER finds it: `where pythonw` first, then the scan of the folder
    setup.cmd installs into — or None when neither knows of one. PATH alone used to be
    the whole search here, which kept this check silent on exactly the machines
    setup.cmd provisions: their Python is never on PATH, so the one mismatch this line
    exists to catch ("packages installed, app still won't start") went unreported.

    Returns the console twin (the python.exe beside that pythonw) when it exists,
    because every caller builds runnable advice out of the answer."""
    import shutil
    pyw = shutil.which("pythonw") or _scan_programs_python("pythonw.exe")
    if not pyw:
        return None
    return _console_twin(pyw)


def _same_interpreter(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:                                          # pragma: no cover - defensive
        return a == b


def _claude_cli_path():
    """Where the `claude` CLI is, searching the same two extra locations worker.py adds to
    PATH (native installer → ~/.local/bin, global npm → %APPDATA%\\npm) so this agrees with
    what the app will actually find at run time."""
    import shutil
    extra = [os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin"),
             os.path.join(os.environ.get("APPDATA", ""), "npm")]
    path = os.pathsep.join([p for p in extra if p] + [os.environ.get("PATH", "")])
    return shutil.which("claude", path=path)


def check():
    """Every environment problem we can name, worst first. Pure inspection — nothing here
    starts the app, opens a window or contacts the network."""
    problems = []

    if sys.version_info < MIN_PYTHON:
        problems.append(Problem(
            FAIL, "python",
            f"Python {sys.version.split()[0]} is too old (need "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+): {sys.executable}",
            "Install Python 3.10 or newer from https://www.python.org/downloads/"))

    # --- the app's own modules -------------------------------------------------
    # A manual ZIP "update" that replaced only claude_overlay.py leaves the sibling
    # modules at their old version or absent; the app then dies on `import authstate`
    # with no window. Checked as files, so it reports even when nothing is importable.
    missing = [m for m in ("config.py", "worker.py", "debuglog.py", "win32utils.py",
                           "modelresolve.py", "cliupdate.py", "authstate.py",
                           "crashreport.py", "usage.py")
               if not os.path.isfile(os.path.join(repo_dir(), m))]
    if missing:
        problems.append(Problem(
            FAIL, "files", f"Part of the app is missing from {repo_dir()}: "
                           f"{', '.join(missing)}",
            "The app is several files, not one. Re-download the whole folder "
            "(git pull, or unzip ALL files over it) - replacing only "
            "claude_overlay.py leaves it unable to start."))

    # --- interpreter capabilities ----------------------------------------------
    try:
        import tkinter  # noqa: F401
    except Exception as e:
        problems.append(Problem(
            FAIL, "tkinter", f"This Python has no working tkinter ({e.__class__.__name__}: {e})",
            "Re-install Python from python.org with the 'tcl/tk and IDLE' option ticked "
            "(the Microsoft Store build and some slim installs omit it)."))

    # --- third-party dependencies ----------------------------------------------
    try:
        from PIL import Image, ImageTk  # noqa: F401
    except Exception as e:
        problems.append(Problem(
            FAIL, "pillow",
            f"Pillow is missing or broken ({e.__class__.__name__}: {e}); "
            f"metadata says {_dist_version('pillow') or 'not installed'}",
            pip_command()))

    sdk_ver = _dist_version("claude-agent-sdk")
    try:
        import claude_agent_sdk
        wanted = required_sdk_symbols()
        absent = [s for s in wanted if not hasattr(claude_agent_sdk, s)]
        if absent:
            problems.append(Problem(
                FAIL, "sdk-old",
                f"claude-agent-sdk {sdk_ver or '?'} is too old for this version of the "
                f"overlay - it doesn't provide: {', '.join(absent)}",
                pip_command()))
    except Exception as e:
        problems.append(Problem(
            FAIL, "sdk",
            f"claude-agent-sdk is missing or broken ({e.__class__.__name__}: {e}); "
            f"metadata says {sdk_ver or 'not installed'}",
            pip_command()))

    try:
        import keyboard  # noqa: F401
    except Exception as e:
        problems.append(Problem(
            WARN, "keyboard",
            f"the `keyboard` package isn't usable ({e.__class__.__name__}) - the overlay "
            f"still runs, but the Ctrl+Alt+Space global hotkey won't work",
            pip_command()))

    # --- am I even inspecting the right Python? ---------------------------------
    # Reported as a WARN, not a FAIL: this report may legitimately be run by a different
    # interpreter than the launcher uses. But when everything above looks fine and the
    # app still won't start, this line is usually the whole answer.
    # Both sides are normalised to their console twin first: the launcher answer already
    # is one, and a report RUN under pythonw (Diagnose.cmd does exactly that) would
    # otherwise compare pythonw.exe against the python.exe beside it — two file names,
    # one environment — and cry "two Pythons" on every perfectly healthy machine.
    launcher = launcher_python()
    if launcher and not _same_interpreter(launcher, _console_twin(sys.executable)):
        problems.append(Problem(
            WARN, "two-pythons",
            "this report is about a DIFFERENT Python from the one that launches the "
            f"overlay:\n         this report : {sys.executable}\n"
            f"         the launcher: {launcher}\n"
            "       Packages installed here do nothing for the app.",
            pip_command(launcher)))

    # --- the thing the overlay actually drives ----------------------------------
    if not _claude_cli_path():
        problems.append(Problem(
            WARN, "cli",
            "the `claude` CLI isn't on PATH - the window will open but every message "
            "will fail",
            "Install it (npm install -g @anthropic-ai/claude-code) and sign in with "
            "`claude` once in a terminal."))

    problems.sort(key=lambda p: 0 if p.level == FAIL else 1)
    return problems


def summary(problems):
    """One line for a dialog: the FAIL findings, or a short all-clear."""
    fails = [p for p in problems if p.level == FAIL]
    if not fails:
        return "No blocking environment problems found."
    return "\n\n".join(p.text() for p in fails)


def environment_block():
    """Reused by the crash reporter and printed by Diagnose.cmd."""
    try:
        import crashreport
        return crashreport.environment_lines(_app_version())
    except Exception:
        return [f"python       : {sys.version.split()[0]}  ({sys.executable})"]


def _app_version():
    """The version WITHOUT importing config (which pulls in the whole config module and
    may itself be the broken thing) - read it straight out of the file."""
    try:
        import re
        src = open(os.path.join(repo_dir(), "config.py"), "r", encoding="utf-8").read()
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', src, re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _import_smoke():
    """Import the app in a SUBPROCESS and hand back (ok, output).

    A subprocess, not a plain import, for two reasons: the startup guard exits the
    process on an import failure (so an in-process attempt would take this report down
    with it), and a subprocess reproduces the real launch — same interpreter, same cwd,
    same module search path — which is the whole point of the check."""
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import claude_overlay; print('import OK', claude_overlay.__version__)"],
            cwd=repo_dir(), capture_output=True, text=True, timeout=120,
            env=dict(os.environ, CLAUDE_OVERLAY_DIALOG="0"))   # never pop a modal in a report
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out.strip()
    except Exception as e:
        return False, f"couldn't run the import test: {e.__class__.__name__}: {e}"


def run(deep=True):
    """Produce the report and the verdict together: (text, ok).

    One pass, one answer. Reporting and deciding used to be two separate calls to
    check(), which is a quiet way to end up printing one conclusion and exiting with
    another — and a green exit code over a red report is worse than no check at all.
    `ok` is False if any FAIL was found OR (when deep) the app didn't actually load:
    the import test is the only one that can catch a break nothing here thought to
    look for, so it must be able to fail the run."""
    lines = ["", "=" * 72, " Claude Overlay - environment report", "=" * 72]
    lines += environment_block()
    lines += [f"install dir  : {repo_dir()}"]

    problems = check()
    ok = not any(p.level == FAIL for p in problems)
    lines += ["", "-" * 72]
    if not problems:
        lines.append("[OK] Everything the overlay needs is present.")
    else:
        for p in problems:
            lines.append(p.text())

    if deep:
        lines += ["", "-" * 72, "Trying to load the app the same way the launcher does..."]
        loaded, out = _import_smoke()
        lines.append(out or ("import OK" if loaded else "(no output)"))
        lines.append("[OK] The app loads." if loaded else
                     "[FAIL] The app cannot load - the traceback above is the reason it "
                     "vanishes when you double-click it.")
        ok = ok and loaded

    try:
        import crashreport
        p = crashreport.log_path()
        if os.path.isfile(p):
            lines += ["", "-" * 72, f"Last entries from {p}:"]
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read()[-4000:]
            lines.append(tail.strip())
    except Exception:
        pass

    lines += ["", "=" * 72, ""]
    return "\n".join(lines), ok


def report_text(deep=True):
    """Just the text (the report is also useful without the verdict)."""
    return run(deep=deep)[0]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    text, ok = run(deep="--quick" not in argv)
    try:
        sys.stdout.write(text + "\n")
    except Exception:                                          # pragma: no cover - defensive
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
