# -*- coding: utf-8 -*-
"""The worker's import left the module's critical path (it was ~500 of the ~630ms it took
to import claude_overlay, paid before a single widget existed) and moved to a background
thread that Overlay.__init__ joins only AFTER the window has painted. What these pin is
the seam that move created: _make_worker must resolve the class from the right place in
each of its three worlds — patched (tests), imported (a real launch), failed (a machine
with a broken claude_agent_sdk)."""
import queue

import pytest

import claude_overlay as co


class _Sentinel:
    """A worker class distinguishable from both FakeWorker and the real thing."""
    def __init__(self, ui_q, permission_mode="default"):
        self.ui_q, self.permission_mode = ui_q, permission_mode


def test_the_patched_global_wins(overlay):
    """conftest setattr's a FakeWorker over co.ClaudeWorker; the Overlay the whole suite
    runs on must have been built from it — proof _make_worker consults the patch point
    first, and that no test ever talks to a real `claude` connection."""
    assert type(overlay.worker).__name__ == "FakeWorker"


def test_the_background_import_is_the_fallback(overlay, monkeypatch):
    """With the global unset (a real launch: it stays None so a stub can never leak into
    production), the class comes from whatever the import thread stashed."""
    monkeypatch.setattr(co, "ClaudeWorker", None)
    monkeypatch.setattr(co, "_worker_import", {"cls": _Sentinel})
    co._worker_ready.set()               # idempotent: the real thread set it long ago
    w = overlay._make_worker("plan")
    assert isinstance(w, _Sentinel) and w.permission_mode == "plan"


def test_a_failed_import_is_reported_not_swallowed(overlay, monkeypatch):
    """The module-level import used to fail the whole launch with the dialog that names
    update.cmd. Moving the import off-thread must not turn that into a silent blank
    window: the stashed error has to reach the same reporter."""
    seen = []
    monkeypatch.setattr(co, "ClaudeWorker", None)
    monkeypatch.setattr(co, "_worker_import", {"err": ImportError("no claude_agent_sdk")})
    monkeypatch.setattr(co, "_report_import_failure",
                        lambda e: (seen.append(e), (_ for _ in ()).throw(SystemExit(1)))[1])
    with pytest.raises(SystemExit):
        overlay._make_worker("default")
    assert seen and "claude_agent_sdk" in str(seen[0])


def test_the_interpreter_cache_records_this_interpreter(tmp_path, monkeypatch):
    """What the launcher's fast path reads (tests/test_launcher_cmd.py drives that end):
    one line, this process's own sys.executable, no trailing newline — cmd's `set /p`
    reads exactly one line and a path is one line. Pure ASCII bytes, because there is no
    codec that reliably matches the reader — `set /p` decodes redirected input in the
    CONSOLE code page (OEM, chcp-able), not the ANSI one — and ASCII alone is the same
    bytes in every page. Written only from __main__ (a pytest import must never cache
    the console python.exe it runs under), which is why the test calls the writer
    directly."""
    import sys
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    co._write_interpreter_cache()
    raw = (tmp_path / "claude-overlay" / "pythonw_path.txt").read_bytes()
    assert raw == sys.executable.encode("ascii")
    assert not raw.endswith(b"\n")


def test_a_non_ascii_interpreter_path_writes_nothing_and_truncates_nothing(
        tmp_path, monkeypatch):
    """write_text opens (and truncates) before it encodes, so an inexpressible path used
    to leave a ZERO-BYTE record behind — unreadable, undeletable-by-the-launcher (no PYW
    means the del line never runs), permanent. Encoding before open means a non-ASCII
    path either never creates the file or leaves an existing record exactly as it was."""
    import sys
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = tmp_path / "claude-overlay"
    d.mkdir()
    (d / "pythonw_path.txt").write_bytes(b"C:\\old\\pythonw.exe")
    monkeypatch.setattr(sys, "executable", "C:\\\u5de5\u5177\\pythonw.exe")
    with pytest.raises(UnicodeEncodeError):
        co._write_interpreter_cache()
    assert (d / "pythonw_path.txt").read_bytes() == b"C:\\old\\pythonw.exe", \
        "the existing record must survive a failed write untouched"


def test_only_a_windowless_interpreter_is_worth_caching():
    """The launcher's last-resort console fallback runs python.exe, which reaches
    __main__ like any launch. Caching THAT would pin a console flash onto every future
    start — the fast path never scans, so an installed pythonw.exe would never be found
    again. The gate is a function of the path so this can be pinned without faking
    sys.executable."""
    assert co._worth_caching(r"C:\Python314\pythonw.exe")
    assert co._worth_caching(r"C:\Python314\PYTHONW.EXE")
    assert not co._worth_caching(r"C:\Python314\python.exe")
    assert not co._worth_caching(r"C:\somewhere\pyw.exe")   # py launcher EXECS pythonw:
    # sys.executable is what actually runs, so a literal pyw.exe here is not the
    # windowless interpreter, it is a launcher — let the scan resolve it every time.


def test_the_window_takes_input_again_after_build(overlay):
    """_build paints the window inside root.update() — a nested event loop that could
    dispatch a queued click to a handler touching the not-yet-created worker — so the
    window is OS-level input-disabled across the paint. The disable must not outlive the
    build: a window that looks alive and eats every click is worse than a slow one."""
    assert not int(overlay.root.attributes("-disabled"))


def test_the_real_import_actually_happened(overlay):
    """The suite imports claude_overlay, which starts the background import of the real
    worker module; by now it must have finished and stashed a class (not an error), or a
    real launch on this machine would hang or wall. Also proves the thread did NOT write
    the module global — that would have clobbered conftest's FakeWorker patch mid-suite."""
    assert co._worker_ready.wait(timeout=30)
    assert "cls" in co._worker_import, co._worker_import.get("err")
    assert co._worker_import["cls"].__name__ == "ClaudeWorker"
    assert type(overlay.worker).__name__ == "FakeWorker"   # the global was left alone
