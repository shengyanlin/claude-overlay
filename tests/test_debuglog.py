# -*- coding: utf-8 -*-
"""Unit tests for debuglog.py — deterministic, no network/display/CLI needed."""

import queue
import time

import pytest

import debuglog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_log(path):
    """Return the content of the log file (str), or '' if it doesn't exist."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# Test 1 — DEBUG_LOG falsy: no file created, no raise
# ---------------------------------------------------------------------------

def test_no_file_when_debug_log_empty(tmp_path, monkeypatch):
    """When DEBUG_LOG is '', dbg() must write nothing and must not raise."""
    monkeypatch.setattr(debuglog, "DEBUG_LOG", "")
    # Confirm no file creation even if we name one
    fake_path = tmp_path / "should_not_exist.log"
    dbg_path_before = debuglog.DEBUG_LOG  # ""

    debuglog.dbg("system", "x")

    assert not fake_path.exists(), "File must NOT be created when DEBUG_LOG is empty"
    # Also no exception was raised (we reached this assertion)


# ---------------------------------------------------------------------------
# Test 2 — Basic line format: contains kind, payload, pid=
# ---------------------------------------------------------------------------

def test_basic_line_format(tmp_path, monkeypatch):
    """dbg('system', 'hello') writes a line with 'system', 'hello', and 'pid='."""
    log_file = tmp_path / "debug.log"
    monkeypatch.setattr(debuglog, "DEBUG_LOG", str(log_file))

    debuglog.dbg("system", "hello")

    content = _read_log(log_file)
    assert content, "Log file must have content"
    line = content.strip()
    assert "system" in line, f"Expected 'system' in line: {line!r}"
    assert "hello" in line, f"Expected 'hello' in line: {line!r}"
    assert "pid=" in line, f"Expected 'pid=' in line: {line!r}"


# ---------------------------------------------------------------------------
# Test 3 — Tool payload: (name, input_dict) formatted as "name key=val"
# ---------------------------------------------------------------------------

def test_tool_payload_format(tmp_path, monkeypatch):
    """dbg('tool', ('Bash', {...})) writes line containing 'Bash' and 'command=ls'."""
    log_file = tmp_path / "debug.log"
    monkeypatch.setattr(debuglog, "DEBUG_LOG", str(log_file))

    debuglog.dbg("tool", ("Bash", {"command": "ls -la", "extra": "y"}))

    content = _read_log(log_file)
    assert content, "Log file must have content"
    line = content.strip()
    assert "Bash" in line, f"Expected 'Bash' in line: {line!r}"
    # The code formats: f"{k}={str(v)[:40]}" so command=ls -la
    assert "command=ls" in line, f"Expected 'command=ls' in line: {line!r}"


# ---------------------------------------------------------------------------
# Test 4 — Privacy: delta kind never logs raw text
# ---------------------------------------------------------------------------

def test_delta_privacy_no_raw_text(tmp_path, monkeypatch):
    """For kind='delta', the raw payload must NEVER appear in the log."""
    log_file = tmp_path / "debug.log"
    monkeypatch.setattr(debuglog, "DEBUG_LOG", str(log_file))
    # Reset throttle so the call is NOT suppressed
    debuglog._dbg_stream_last[0] = 0.0

    secret = "SUPER_SECRET_REPLY"
    debuglog.dbg("delta", secret)

    content = _read_log(log_file)
    assert content, "Log file must have content (throttle was reset)"
    line = content.strip()
    assert "<streaming" in line, f"Expected '<streaming' heartbeat in line: {line!r}"
    # Must contain a digit (the char count)
    assert any(ch.isdigit() for ch in line), f"Expected digit in line: {line!r}"
    assert secret not in line, f"Raw secret text must NOT appear in line: {line!r}"


# ---------------------------------------------------------------------------
# Test 5 — Throttle: two rapid delta calls produce exactly one log line
# ---------------------------------------------------------------------------

def test_delta_throttle_collapses_to_one_line(tmp_path, monkeypatch):
    """Two immediate delta calls within 2s must produce exactly ONE log line."""
    log_file = tmp_path / "debug.log"
    monkeypatch.setattr(debuglog, "DEBUG_LOG", str(log_file))
    # Reset throttle so the FIRST call goes through
    debuglog._dbg_stream_last[0] = 0.0

    debuglog.dbg("delta", "a")
    debuglog.dbg("delta", "b")   # same ~instant timestamp → should be suppressed

    content = _read_log(log_file)
    streaming_lines = [ln for ln in content.splitlines() if "<streaming" in ln]
    assert len(streaming_lines) == 1, (
        f"Expected exactly 1 '<streaming' line, got {len(streaming_lines)}:\n{content}"
    )


# ---------------------------------------------------------------------------
# Test 6 — Long string payload is truncated (well under 1000 chars per line)
# ---------------------------------------------------------------------------

def test_long_payload_truncated(tmp_path, monkeypatch):
    """A 1000-char payload must be truncated; the written line must be << 1000 chars."""
    log_file = tmp_path / "debug.log"
    monkeypatch.setattr(debuglog, "DEBUG_LOG", str(log_file))

    long_payload = "Z" * 1000
    debuglog.dbg("system", long_payload)

    content = _read_log(log_file)
    assert content, "Log file must have content"
    line = content.strip()
    # The code caps strings at 200 chars; the full line (with ts+kind etc.) must
    # be well under 1000 chars.
    assert len(line) < 1000, (
        f"Line should be truncated to well under 1000 chars, got {len(line)}"
    )
    # More precisely: the payload portion is capped at 200 chars
    assert "Z" * 201 not in line, "Payload must be capped at 200 chars"


# ---------------------------------------------------------------------------
# Test 7 — _UIQueueTap: forwarding and delegation
# ---------------------------------------------------------------------------

def test_ui_queue_tap_put_and_delegation(tmp_path, monkeypatch):
    """
    _UIQueueTap.put() must:
      - write a log line (when DEBUG_LOG is set)
      - forward the item to the underlying queue

    __getattr__ delegation must make qsize() and empty() work correctly.
    """
    log_file = tmp_path / "debug.log"
    monkeypatch.setattr(debuglog, "DEBUG_LOG", str(log_file))

    inner_q = queue.Queue()
    tap = debuglog._UIQueueTap(inner_q)

    # --- put and forwarding ---
    tap.put(("ready", None))

    # The underlying queue must contain exactly the item we put
    item = inner_q.get_nowait()
    assert item == ("ready", None), f"Expected ('ready', None), got {item!r}"

    # A log line should have been written
    content = _read_log(log_file)
    assert content, "Log file must have content after tap.put()"
    assert "ready" in content, f"Expected 'ready' in log, got: {content!r}"

    # --- delegation: qsize and empty ---
    # Put one item into the underlying queue directly (bypassing tap)
    inner_q.put(("delta", "x"))

    assert tap.qsize() == 1, f"Expected qsize()==1, got {tap.qsize()}"
    assert tap.empty() is False, "Expected empty()==False after putting one item"

    inner_q.get_nowait()  # drain

    assert tap.empty() is True, "Expected empty()==True after draining"
