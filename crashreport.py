# -*- coding: utf-8 -*-
"""Make a startup crash SAY something instead of vanishing.

WHY THIS EXISTS
The overlay is launched with pythonw (deliberately: a console window would defeat the
point of a frameless floating chat). pythonw has no stdout/stderr, the app writes no log
unless CLAUDE_OVERLAY_DEBUG_LOG is set, and Tk is not up yet during startup — so ANY
exception before the window appears kills the process with no window, no message and no
trace. To the user that is "I double-clicked it and nothing happened"; to whoever has to
fix it, it is a bug report with zero information, on a machine they don't have.

That is exactly the failure this module removes. It gives the app three things, all
stdlib-only so they work even when the app's own dependencies are the thing that broke:

  * a crash log     — %LOCALAPPDATA%\\claude-overlay\\crash.log, with the traceback AND the
                      environment facts that decide these bugs (which interpreter, which
                      package versions, which install path);
  * a dialog        — a native MessageBox, because a log nobody knows about is not a
                      diagnostic. Shown only when there is no console to print to, so a
                      console run (or CI) prints instead of blocking on a modal;
  * excepthooks     — sys.excepthook and threading.excepthook, so a crash AFTER startup
                      (including a worker thread dying silently) also lands in the log.

DOCTRINE
Nothing here may raise. A reporter that throws while reporting turns one legible failure
into two illegible ones, so every entry point is wrapped and degrades to doing less
(no dialog, no log, no environment block) rather than to failing.

Leaf module: stdlib only, no project imports — it has to survive `import config` itself
being the thing that blew up.
"""

import os
import sys
import time
import traceback

APP_NAME = "Claude Overlay"
_MAX_LOG_BYTES = 512 * 1024        # rotate at ~0.5MB; a crash log is append-mostly and tiny
_LOG_NAME = "crash.log"
_installed = False


# ── where things go ─────────────────────────────────────────────────────────
def log_dir():
    """The per-machine app directory (same one config.py keeps state.json in), resolved
    WITHOUT importing config — config may be what failed."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "claude-overlay")


def log_path():
    return os.path.join(log_dir(), _LOG_NAME)


# ── environment facts (the half of a bug report users never think to include) ──
def _pkg_version(mod_name):
    """Installed version of a distribution, or a short reason we couldn't get one. Uses
    importlib.metadata so it does NOT import the package (importing is what may crash)."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version(mod_name)
        except PackageNotFoundError:
            return "NOT INSTALLED"
    except Exception as e:                                     # pragma: no cover - defensive
        return f"unknown ({e.__class__.__name__})"


def environment_lines(app_version=""):
    """The facts that actually decide these bugs. Deliberately includes sys.executable:
    the classic Windows failure is two Pythons — `update.cmd` upgrading packages in one
    interpreter while the launcher runs the app in another — and the only way to SEE that
    is to print which interpreter is speaking."""
    out = []
    try:
        out.append(f"app          : {APP_NAME} {app_version or '(version unknown)'}")
        out.append(f"time         : {time.strftime('%Y-%m-%d %H:%M:%S')}")
        out.append(f"python       : {sys.version.split()[0]}  ({sys.executable})")
        out.append(f"platform     : {sys.platform}")
        out.append(f"script       : {os.path.abspath(sys.argv[0]) if sys.argv else '?'}")
        out.append(f"cwd          : {os.getcwd()}")
        out.append(f"console      : {'yes' if has_console() else 'no (pythonw)'}")
        for dist in ("claude-agent-sdk", "pillow", "keyboard", "anyio"):
            out.append(f"{dist:<13}: {_pkg_version(dist)}")
    except Exception:                                          # pragma: no cover - defensive
        pass
    return out


def has_console():
    """True when this process owns a console we can print to. Under pythonw there is
    none, which is precisely when a silent death is invisible and a dialog is the only
    channel left."""
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleWindow():
            return True
    except Exception:
        pass
    # No console window (or not Windows): fall back to whether stderr survived. pythonw
    # gives sys.stderr = None, so this is a second independent signal, not a duplicate.
    return bool(getattr(sys, "stderr", None))


# ── the two output channels ─────────────────────────────────────────────────
def _append_log(text):
    """Append to crash.log, rotating first if it got big. Returns the path on success,
    None if we couldn't write at all (read-only profile, redirected LOCALAPPDATA, …)."""
    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, _LOG_NAME)
        try:
            if os.path.getsize(p) > _MAX_LOG_BYTES:
                os.replace(p, p + ".old")      # keep one generation; the previous crash
        except OSError:                        # (missing file / can't stat) → nothing to rotate
            pass
        with open(p, "a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        return p
    except Exception:
        return None


def _dialog(title, body):
    """Native MessageBox. Best-effort: if user32 isn't reachable we simply lose the
    dialog — the log is still written, and the caller still exits."""
    try:
        import ctypes
        MB_OK, MB_ICONERROR, MB_SETFOREGROUND, MB_TOPMOST = 0x0, 0x10, 0x10000, 0x40000
        ctypes.windll.user32.MessageBoxW(
            None, str(body)[:3500], str(title)[:200],
            MB_OK | MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST)
        return True
    except Exception:
        return False


def _dialogs_allowed():
    """Show a modal only when there is nobody to print to. A console run (and CI, which
    always has one) prints instead — a blocking dialog in an automated run is a hang, and
    a hang is a worse bug than the one being reported. CLAUDE_OVERLAY_DIALOG forces
    either way: 1 = always, 0 = never."""
    forced = (os.environ.get("CLAUDE_OVERLAY_DIALOG") or "").strip()
    if forced in ("0", "false", "no", "off"):
        return False
    if forced in ("1", "true", "yes", "on"):
        return True
    return not has_console()


# ── public API ──────────────────────────────────────────────────────────────
def report(summary, detail="", app_version="", dialog=True, title=None):
    """Record one failure: always to the log, and to the user if there's no console.
    Returns the log path (or None). Never raises."""
    try:
        block = ["", "=" * 72, f"{summary}", "-" * 72]
        block += environment_lines(app_version)
        if detail:
            block += ["-" * 72, detail.rstrip()]
        path = _append_log("\n".join(block))

        where = f"\n\nFull details were saved to:\n{path}" if path else ""
        body = f"{summary}\n\n{detail.strip()}{where}"
        if dialog and _dialogs_allowed():
            _dialog(title or f"{APP_NAME} - startup failed", body)
        elif has_console():
            try:
                sys.stderr.write(f"\n[{APP_NAME}] {summary}\n{detail}\n{where}\n")
            except Exception:
                pass
        return path
    except Exception:
        return None


def report_exception(exc, context="", app_version="", dialog=True):
    """report() for a caught exception, with its traceback as the detail."""
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:                                          # pragma: no cover - defensive
        tb = repr(exc)
    head = f"{exc.__class__.__name__}: {exc}"
    summary = f"{context}: {head}" if context else head
    return report(summary, tb, app_version=app_version, dialog=dialog)


def fatal(summary, detail="", app_version="", code=1):
    """Report, then leave. os._exit (not sys.exit) because a failed startup may already
    have non-daemon threads or a half-built Tk behind it, and an orphaned invisible
    process is the same "nothing happened" symptom we're here to remove."""
    report(summary, detail, app_version=app_version)
    try:
        sys.stderr and sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def install(app_version=""):
    """Route unhandled exceptions — main thread AND background threads — into the log.

    Threads matter as much as the main thread here: the worker runs the entire Claude
    conversation on a daemon thread, so before this hook existed a worker that died on
    an unexpected exception left a window that simply never answered again, with nothing
    written anywhere. Idempotent."""
    global _installed
    if _installed:
        return
    try:
        prev = sys.excepthook

        def _hook(etype, value, tb):
            try:
                report_exception(value, "Unhandled error", app_version=app_version)
            finally:
                try:
                    prev(etype, value, tb)
                except Exception:
                    pass

        sys.excepthook = _hook

        import threading
        prev_t = getattr(threading, "excepthook", None)

        def _thook(args):
            try:
                # A daemon thread torn down at interpreter exit raises with exc_value
                # None — that is shutdown noise, not a crash, and logging it would train
                # people to ignore the log.
                if args.exc_value is not None:
                    report_exception(
                        args.exc_value,
                        f"Unhandled error in thread {getattr(args.thread, 'name', '?')}",
                        app_version=app_version, dialog=False)
            finally:
                try:
                    prev_t and prev_t(args)
                except Exception:
                    pass

        if prev_t is not None:
            threading.excepthook = _thook
        _installed = True
    except Exception:
        pass


class guard:
    """Context manager for a block whose failure must not be silent.

        with crashreport.guard("starting up", __version__):
            Overlay().run()

    Reports and exits; re-raising would just put the traceback back into the void."""

    def __init__(self, context="starting up", app_version="", code=1):
        self.context = context
        self.app_version = app_version
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, etype, exc, tb):
        if exc is None or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return False
        report_exception(exc, f"{APP_NAME} failed while {self.context}",
                         app_version=self.app_version)
        os._exit(self.code)
