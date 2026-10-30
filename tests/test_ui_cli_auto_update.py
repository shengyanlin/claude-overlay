"""UI tests for the launch-time CLI auto-update (CLI_AUTO_UPDATE, on by default).

At every launch the overlay looks at BOTH CLIs; one that's behind and that npm installed is
updated without a click. The live agent's CLI is held open by its worker (claude.exe, Codex's
app-server), so that worker is paused for the update -- never mid-reply -- and resumed on the
same conversation. One update runs at a time, the button's and the automatic ones alike.
No npm is ever run: cliupdate is patched, and background threads run inline so each test
reads the outcome straight off the UI queue."""
import time
import tkinter as tk

import pytest

import claude_overlay as co
import cliupdate
import conftest
from conftest import chat_text

INFO = {"installed": "2.1.198", "latest": "2.1.204", "behind": True}


class _Inline:
    """threading.Thread that runs its target on start(), on this thread."""

    def __init__(self, target=None, name=None, daemon=None, args=(), kwargs=None):
        self._target, self._args, self.name = target, args, name

    def start(self):
        self._target(*self._args)


@pytest.fixture
def ov(overlay, monkeypatch):
    overlay._cli_update_checked = set()
    overlay._cli_update_shown_for = set()
    overlay._session_id = None
    monkeypatch.setattr(co.threading, "Thread", _Inline)
    monkeypatch.setattr(overlay.root, "after", lambda ms, fn=None, *a: "after#")
    yield overlay
    overlay._cli_auto_after = None


def _drain(ov):
    out = []
    while not ov.ui_q.empty():
        out.append(ov.ui_q.get_nowait())
    return out


def _deliver(ov):
    for kind, payload in _drain(ov):
        ov._handle(kind, payload)


def _patch_check(monkeypatch, behind=("claude",), managed=("claude", "codex")):
    asked = []

    def check_update(force=False, cli="claude"):
        asked.append((cli, force))
        return dict(INFO, behind=cli in behind)
    monkeypatch.setattr(cliupdate, "check_update", check_update)
    monkeypatch.setattr(cliupdate, "npm_managed", lambda cli="claude": cli in managed)
    return asked


def _patch_update(monkeypatch, result=(True, "2.1.204")):
    ran = []

    def run_update(cli="claude"):
        ran.append(cli)
        return result
    monkeypatch.setattr(cliupdate, "run_update", run_update)
    return ran


# ── the launch check ──

def test_the_launch_check_asks_about_both_clis_fresh(ov, monkeypatch):
    asked = _patch_check(monkeypatch, behind=("claude", "codex"))
    conftest.REAL_CHECK_CLI_UPDATE(ov)
    assert asked == [("claude", True), ("codex", True)]       # the live agent's first, no cache
    kinds = [(k, p["cli"]) for k, p in _drain(ov)]
    assert kinds == [("cli_auto_update", "claude"), ("cli_auto_update", "codex")]


def test_a_cli_npm_did_not_install_gets_the_notice_instead(ov, monkeypatch):
    """Claude's native install updates itself; an npm install beside it would never run."""
    _patch_check(monkeypatch, managed=())
    conftest.REAL_CHECK_CLI_UPDATE(ov)
    assert [k for k, _ in _drain(ov)] == ["cli_update"]


def test_auto_update_off_is_the_old_check(ov, monkeypatch):
    monkeypatch.setattr(co, "CLI_AUTO_UPDATE", False)
    asked = _patch_check(monkeypatch)
    conftest.REAL_CHECK_CLI_UPDATE(ov)
    assert asked == [("claude", False)]                      # live agent only, daily cache
    assert [k for k, _ in _drain(ov)] == ["cli_update"]


def test_the_switch_does_not_ask_again(ov, monkeypatch):
    asked = _patch_check(monkeypatch, behind=())
    conftest.REAL_CHECK_CLI_UPDATE(ov)
    conftest.REAL_CHECK_CLI_UPDATE(ov)
    assert len(asked) == 2


# ── the update ──

def test_the_live_cli_waits_for_the_reply_then_restarts_on_the_conversation(ov, monkeypatch):
    ran = _patch_update(monkeypatch)
    old = ov.worker
    ov._session_id = "sess-1"
    ov.busy = True                                           # a reply is streaming
    ov._handle("cli_auto_update", dict(INFO, cli="claude"))
    assert ran == [] and ("shutdown", ()) not in old.calls   # nothing touched mid-reply
    ov.busy = False
    ov._cli_auto_next()                                      # the two-second re-check
    assert ran == ["claude"] and ("shutdown", ()) in old.calls
    assert "Updating the Claude CLI v2.1.198 → v2.1.204 on its own" in chat_text(ov)
    _deliver(ov)
    assert ov.worker is not old
    assert ("start", ()) in ov.worker.calls and ("resume", ("sess-1",)) in ov.worker.calls
    assert ov.busy is False and ov._cli_op is None
    assert "Claude CLI updated to v2.1.204 — Claude has restarted on it" in chat_text(ov)


def test_the_other_agents_cli_goes_at_once_without_a_pause(ov, monkeypatch):
    ran = _patch_update(monkeypatch, (True, "0.161.0"))
    old = ov.worker
    ov.busy = True                                           # Claude is mid-reply: irrelevant
    ov._handle("cli_auto_update", dict(INFO, cli="codex", installed="0.160.1", latest="0.161.0"))
    assert ran == ["codex"] and ("shutdown", ()) not in old.calls
    _deliver(ov)
    assert ov.worker is old
    assert "Codex CLI updated to v0.161.0." in chat_text(ov)
    ov.busy = False


def test_two_updates_run_one_after_the_other(ov, monkeypatch):
    ran = _patch_update(monkeypatch)
    ov._handle("cli_auto_update", dict(INFO, cli="codex"))
    ov._handle("cli_auto_update", dict(INFO, cli="claude"))
    assert ran == ["codex"]                                  # claude waits its turn
    _deliver(ov)                                             # codex's result -> claude starts
    assert ran == ["codex", "claude"]
    _deliver(ov)
    assert ov._cli_auto_queue == [] and ov._cli_op is None


def test_a_failure_says_so_keeps_the_conversation_and_leaves_a_retry_button(ov, monkeypatch):
    _patch_update(monkeypatch, (False, "a running Claude process is using the CLI"))
    old = ov.worker
    before = set(ov.chat.window_names())
    ov._handle("cli_auto_update", dict(INFO, cli="claude"))
    _deliver(ov)
    assert "Couldn't update the Claude CLI on its own — a running Claude process" in chat_text(ov)
    assert ov.worker is not old and ov.busy is False          # back on a live worker
    new = [n for n in ov.chat.window_names() if n not in before]
    assert any(isinstance(ov.root.nametowidget(n), tk.Canvas) for n in new)


def test_an_exception_in_the_update_is_a_failure_not_a_crash(ov, monkeypatch):
    def boom(cli="claude"):
        raise OSError("npm vanished")
    monkeypatch.setattr(cliupdate, "run_update", boom)
    ov._handle("cli_auto_update", dict(INFO, cli="claude"))
    _deliver(ov)
    assert "Couldn't update the Claude CLI on its own — OSError" in chat_text(ov)
    assert ov._cli_op is None and ov._codex_paused is False


def test_bad_payloads_are_ignored(ov, monkeypatch):
    ran = _patch_update(monkeypatch)
    ov._handle("cli_auto_update", "not-a-dict")
    ov._handle("cli_auto_update", {"cli": "vim"})
    ov._handle("cli_auto_result", None)
    assert ran == [] and ov._cli_op is None


# ── one update at a time, never under a reply (the Codex review's findings) ──

def test_the_pause_happens_in_the_callback_that_saw_the_agent_idle(ov):
    """No gap for Enter or the line-up to start a reply between the idle check and the pause."""
    old = ov.worker
    assert ov._begin_cli_update("claude") == "ok"
    assert ("shutdown", ()) in old.calls and ov.busy is True and ov._cli_op == "claude"
    assert ov._begin_cli_update("codex") == "taken"
    ov._end_cli_update()
    assert ov.busy is False and ov._cli_op is None


def test_a_click_while_the_agent_replies_is_refused_and_says_so(ov, monkeypatch):
    ran = _patch_update(monkeypatch)
    old = ov.worker
    ov.busy = True
    btn = ov._cli_update_btn("2.1.204", "claude")
    btn._click(None)
    assert btn._ustate == "idle" and ran == [] and ("shutdown", ()) not in old.calls
    assert "waits for the reply to finish" in chat_text(ov)
    ov.busy = False


def test_a_click_while_another_update_runs_is_refused(ov, monkeypatch):
    ran = _patch_update(monkeypatch)
    ov._cli_op = "codex"                               # an automatic Codex update is running
    btn = ov._cli_update_btn("2.1.204", "claude")
    btn._click(None)
    assert btn._ustate == "idle" and ran == []
    assert "Another CLI update is running" in chat_text(ov)


def test_a_click_updates_the_cli_it_was_for_and_releases_the_claim(ov, monkeypatch):
    ran = _patch_update(monkeypatch)
    claude_btn = ov._cli_update_btn("2.1.204", "claude")
    codex_btn = ov._cli_update_btn("0.161.0", "codex")   # made last: must not take over
    claude_btn._click(None)
    assert ran == ["claude"]
    _deliver(ov)
    assert ov._cli_op is None and claude_btn._ustate == "done"
    assert "Claude CLI updated to v2.1.204 — Claude has restarted on it" in chat_text(ov)
    assert codex_btn._ustate == "idle"


def test_no_switch_while_a_cli_update_runs(ov):
    """Codex updating in the background while Claude is live: switching would start
    codex.exe while npm replaces it."""
    ov._cli_op = "codex"
    assert ov.switch_provider("codex") is False
    assert ov.provider == "claude"
    assert "A CLI update is running" in chat_text(ov)


def test_clear_during_the_update_starts_the_new_conversation(ov):
    """The Clear goes to the stopped worker, whose reset_done never comes: the fresh worker
    is the new conversation, and the line-up isn't held forever."""
    ov._session_id = "sess-old"
    assert ov._begin_cli_update("claude") == "ok"
    ov.reset()                                         # Clear, mid-update
    assert ov._discard_pending is True
    ov._end_cli_update()
    assert ov._discard_pending is False
    assert not [c for c in ov.worker.calls if c[0] == "resume"]


def test_a_worker_that_never_stops_means_npm_never_runs(ov, monkeypatch):
    ran = _patch_update(monkeypatch)

    class Stuck:
        def __init__(self):
            self.calls = []
        def interrupt(self): pass
        def shutdown(self): pass
        def join(self, *a): pass
        def is_alive(self): return True
    ov.worker = Stuck()
    ov._handle("cli_auto_update", dict(INFO, cli="claude"))
    assert ran == []
    _deliver(ov)
    assert "Claude didn't stop in time, so the update wasn't started" in chat_text(ov)
    assert ov._cli_op is None and ov.busy is False


def test_a_resume_on_its_way_counts_as_busy(ov):
    """Resume last conversation leaves busy False while the worker swaps sessions: stopping
    it then would purge the session event, and the restart would start fresh."""
    ov._resume_started = time.monotonic()
    assert ov._begin_cli_update("claude") == "busy"
    ov._resume_started = time.monotonic() - 120          # never landed: don't wait forever
    assert ov._begin_cli_update("claude") == "ok"
    ov._end_cli_update()


def test_the_restarted_worker_gets_the_model_picked_from_the_menu(ov):
    ov._session_id = "sess-2"
    ov._switch_model("sonnet")
    assert ov._begin_cli_update("claude") == "ok"
    ov._end_cli_update()
    calls = ov.worker.calls
    assert ("set_model", ("sonnet",)) in calls
    assert calls.index(("resume", ("sess-2",))) < calls.index(("set_model", ("sonnet",)))


def test_the_other_agents_cli_is_claimed_without_touching_the_live_worker(ov):
    old = ov.worker
    assert ov._begin_cli_update("codex") == "ok"       # Claude is live
    assert ("shutdown", ()) not in old.calls and ov.busy is False
    assert ov._wait_cli_released() is True
    ov._end_cli_update()
    assert ov.worker is old
