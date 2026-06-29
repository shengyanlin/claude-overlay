"""Shared fixtures for the UI feature tests.

Builds ONE real `Overlay` for the whole test session on a hidden Tk root, with every
external side effect neutralized — no worker thread / no `claude` connection
(FakeWorker), no global hotkey, no GitHub update check — so a test can drive the ACTUAL
Overlay feature methods (markdown render, collapse, zoom, tool chips, copy, compact,
toggles…) and assert on the resulting Tk widget state.

Why a single session-wide root: creating `tk.Tk()` once per test fails intermittently
in one process ("couldn't read file …/auto.tcl" on the 2nd/3rd init — Tcl can't be
cleanly re-initialised). So we build the Overlay once and reset it to a known state
before each test (`overlay` fixture → `_clean_overlay`). The window is withdrawn (never
shown: no flash, no focus steal). Skips cleanly if Tk has no display.
"""
import os
import queue
import sys

import pytest

# Belt-and-suspenders for the one Tk init (and any stray second root): pin the Tcl/Tk
# library dirs so the interpreter always finds them.
_tcl_root = os.path.join(sys.base_prefix, "tcl")
if os.path.isdir(_tcl_root):
    for _sub, _var in (("tcl8.6", "TCL_LIBRARY"), ("tk8.6", "TK_LIBRARY")):
        _d = os.path.join(_tcl_root, _sub)
        if os.path.isdir(_d):
            os.environ.setdefault(_var, _d)

import tkinter as tk


class FakeWorker:
    """Stand-in for ClaudeWorker: stores the UI queue, records calls for assertions,
    starts no thread and connects to nothing."""

    def __init__(self, ui_queue):
        self.ui = ui_queue
        self.req = queue.Queue()
        self.calls = []

    def _rec(self, name, *a):
        self.calls.append((name, a))

    def start(self):            self._rec("start")
    def ask(self, *a, **k):     self._rec("ask", *a)
    def reset(self):            self._rec("reset")
    def compact(self):          self._rec("compact")
    def set_model(self, *a):    self._rec("set_model", *a)
    def interrupt(self):        self._rec("interrupt")
    def shutdown(self):         self._rec("shutdown")
    def join(self, *a, **k):    self._rec("join")


@pytest.fixture(scope="session")
def _overlay_singleton():
    """The one Overlay/Tk root for the session. Built with side effects patched out."""
    import claude_overlay as co

    mp = pytest.MonkeyPatch()
    mp.setattr(co, "ClaudeWorker", FakeWorker)
    mp.setattr(co.Overlay, "_register_hotkey", lambda self: None)
    mp.setattr(co.Overlay, "_check_for_update", lambda self: None)
    try:
        ov = co.Overlay()
    except tk.TclError as e:
        mp.undo()
        pytest.skip(f"Tk unavailable (no display?): {e}")
    ov.root.withdraw()
    ov.root.update_idletasks()
    try:
        yield ov
    finally:
        try:
            ov.root.destroy()
        except Exception:
            pass
        mp.undo()


def _clean_overlay(ov):
    """Return the shared Overlay to a known baseline before each test."""
    import claude_overlay as co
    try:
        if not ov.expanded:
            ov.toggle_collapse()        # back to expanded
    except Exception:
        pass
    try:
        ov.reset()                      # clears chat + md state + badge + compact banner
    except Exception:
        pass
    try:
        ov._set_zoom(1.0)               # back to 100%
    except Exception:
        pass
    ov.auto_shot = co.AUTO_SCREENSHOT_DEFAULT
    ov.share_visible = co.SHOW_IN_SCREEN_SHARE_DEFAULT
    ov.overlay_name = ""
    ov.busy = False
    ov._model = None
    ov._ctx_pct = None
    ov.pending_images = []
    ov.pending_shot = None
    try:
        ov._refresh_send()
    except Exception:
        pass
    try:
        ov.worker.calls.clear()         # clear AFTER reset() so its interrupt/reset don't show
    except Exception:
        pass
    try:
        ov.root.update_idletasks()
    except Exception:
        pass


@pytest.fixture
def overlay(_overlay_singleton):
    """Per-test handle to the shared Overlay, reset to a clean baseline first."""
    _clean_overlay(_overlay_singleton)
    return _overlay_singleton


def chat_text(ov):
    """Convenience: the full transcript text in the chat Text widget."""
    return ov.chat.get("1.0", "end")
