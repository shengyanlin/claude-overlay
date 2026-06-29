"""Tests for win32utils — the Windows-only ctypes helpers. They call the real Win32
API, so the module is skipped off Windows, and assertions stay at the level of
contracts that hold even on a headless CI runner (which may enumerate zero monitors)."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only Win32 helpers")

if sys.platform == "win32":
    import win32utils


def test_capture_affinity_constants():
    assert win32utils.WDA_NONE == 0x00
    assert win32utils.WDA_EXCLUDEFROMCAPTURE == 0x11


def test_exstyle_constants():
    assert win32utils.GWL_EXSTYLE == -20
    assert win32utils.WS_EX_APPWINDOW == 0x00040000
    assert win32utils.WS_EX_TOOLWINDOW == 0x00000080


def test_clipboard_format_ids():
    # Used by the "is there an image on the clipboard?" probe; text ids must be right so a
    # text copy pastes as text, not as an image (the v1.7.1 fix).
    assert win32utils.CF_UNICODETEXT == 13
    assert win32utils.CF_TEXT == 1


def test_dpi_and_appid_calls_never_raise():
    # Both are best-effort and must never raise into the caller, regardless of OS state.
    win32utils.set_dpi_awareness()
    win32utils.set_app_user_model_id()


def test_enumerate_monitors_shape():
    mons = win32utils.enumerate_monitors()
    assert isinstance(mons, list)
    for m in mons:
        assert {"rect", "primary"} <= set(m)
        assert isinstance(m["rect"], tuple) and len(m["rect"]) == 4
        assert all(isinstance(v, int) for v in m["rect"])
        assert isinstance(m["primary"], bool)
    # primary-first ordering: if anything is flagged primary, it sorts to the front.
    if any(m["primary"] for m in mons):
        assert mons[0]["primary"] is True
