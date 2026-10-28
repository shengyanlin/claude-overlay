"""Codex mode does what Claude mode does (Claude skills aside): login notices and the send
gate, the allowance notice + retry machinery, past conversations, the compaction progress
bar's own history, ⚡ Auto, and the CLI's one-click update."""
import pytest

import claude_overlay as co
from conftest import chat_text


@pytest.fixture
def codex(overlay):
    overlay.switch_provider("codex", force=True)
    overlay.worker.calls.clear()
    return overlay


def _type(ov, text):
    ov._ph_out()
    ov.entry.delete("1.0", "end")
    ov.entry.insert("1.0", text)
    ov._ph_active = False


def test_signed_out_codex_is_announced_and_holds_sends(codex):
    codex._handle("codex_auth", {"ok": False})
    assert codex._auth_dead is True
    assert "codex login" in chat_text(codex)
    _type(codex, "are you there?")
    assert codex._auth_blocks_send() is True
    assert codex._entry_text() == "are you there?"              # nothing was consumed
    codex._handle("codex_auth", {"ok": True})
    assert codex._auth_dead is False
    assert "Codex login restored" in chat_text(codex)
    assert codex._auth_blocks_send() is False


def test_a_signed_in_start_says_nothing(codex):
    before = chat_text(codex)
    codex._handle("codex_auth", {"ok": True})
    assert chat_text(codex) == before


def test_allowance_notice_speaks_in_codex_mode(codex):
    codex._handle("quota", {"status": "allowed_warning", "utilization": 0.85,
                            "resets_at": None, "window": "five_hour"})
    assert "85% of your 5h allowance" in chat_text(codex)


def test_a_refused_turn_offers_the_timed_retry(codex):
    import time
    codex._handle("quota", {"status": "rejected", "utilization": 1.0,
                            "resets_at": time.time() + 3600, "window": "five_hour"})
    codex._last_sent = ("redo the chart", [])
    codex._handle("result", {"is_error": True, "subtype": "rate_limit_error",
                             "result": "You've hit your usage limit."})
    assert codex._entry_text() == "redo the chart"              # handed back
    assert codex._retry is not None and codex._retry["text"] == "redo the chart"


def test_past_codex_conversations_render_as_cards(codex):
    codex.show_sessions()
    assert ("list_sessions", ()) in codex.worker.calls
    before = len(codex.chat.window_names())
    codex._handle("codex_sessions", [
        {"id": "th-a", "title": "Summarise the deck", "subtitle": "gpt-x", "mtime": 1790000000},
        {"id": "th-b", "title": "Excel macro", "subtitle": "gpt-y", "mtime": 1790000500}])
    names = codex.chat.window_names()
    assert len(names) == before + 2                             # no count → never folded away
    cards = [codex.chat.nametowidget(n) for n in names[before:]]
    cards[1]._on_click()
    assert ("resume", ("th-b",)) in codex.worker.calls
    cards[0]._arm()
    cards[0]._delete()
    assert ("archive", ("th-a",)) in codex.worker.calls         # archived, not unlinked


def test_compaction_history_is_kept_per_agent(codex):
    codex._ctx_tokens = 5000
    codex._handle("compacting", {"pre_tokens": 5000})
    codex._handle("compact_done", {"status": "ok", "detail": None,
                                   "meta": {"pre_tokens": 5000, "post_tokens": 800,
                                            "duration_ms": 4000}})
    assert co._compact_history("codex_compact_runs")[-1] == (5000, 4.0)
    assert (5000, 4.0) not in co._compact_history("compact_runs")


def test_auto_effort_is_offered_with_the_models_default(codex):
    codex._handle("codex_models", [{"id": "gpt-x", "label": "GPT-X", "efforts": ["low", "high"],
                                    "default_effort": "medium"}])
    codex._handle("model", "gpt-x")
    assert ("Auto (this model's default: Medium)", "auto") in codex._effort_levels()
    codex._switch_effort("auto")
    assert ("set_effort", ("auto",)) in codex.worker.calls


def test_codex_cli_update_notice_only_for_the_live_agent(codex):
    codex._handle("cli_update", {"installed": "0.160.1", "latest": "0.161.0", "behind": True,
                                 "cli": "claude"})
    assert "out of date" not in chat_text(codex)                # Claude's, while on Codex
    codex._handle("cli_update", {"installed": "0.160.1", "latest": "0.161.0", "behind": True,
                                 "cli": "codex"})
    assert "Your Codex CLI is out of date (v0.160.1 → v0.161.0)" in chat_text(codex)


def test_codex_update_pauses_and_restarts_codex_on_the_same_conversation(codex):
    old = codex.worker
    codex._session_id = "th-9"
    codex._pause_codex()
    assert ("shutdown", ()) in old.calls and codex.busy
    codex._cli_update_target = "codex"
    codex._show_cli_update_result((True, "0.161.0"))
    assert codex.worker is not old
    assert ("start", ()) in codex.worker.calls and ("resume", ("th-9",)) in codex.worker.calls
    assert not codex.busy
    assert "Codex CLI updated to v0.161.0" in chat_text(codex)


# ── regressions from the Codex code review ──

def test_pausing_for_an_update_drops_the_old_workers_queued_events(codex):
    old = codex.worker
    old.ui.put(("result", {"is_error": False}))      # already in ui_q when Update is clicked
    old.ui.put(("turn_done", None))
    codex._pause_codex()
    codex._poll()
    assert codex.busy is True                        # still held: no send into a dead worker


def test_messages_typed_during_the_update_go_out_after_it(codex):
    codex._session_id = None
    codex._pause_codex()
    codex._queue = [{"text": "after the update", "images": [], "docs": [], "shots": [],
                     "unchanged": False}]
    codex._cli_update_target = "codex"
    codex._show_cli_update_result((True, "0.161.0"))
    assert codex.busy is False and codex._queue_after is not None   # the flush is scheduled


def test_a_history_load_in_flight_does_not_survive_the_switch(codex):
    codex.show_sessions()
    assert codex._sessions_loading is True
    codex.switch_provider("claude")                  # empty conversation → allowed
    assert codex._sessions_loading is False


def test_a_permissions_card_says_what_is_asked(codex):
    codex._handle("approval", {"rid": 3, "kind": "permissions",
                               "permissions": "network access", "reason": "to fetch the docs"})
    card = codex._approval_cards[3]["card"]
    texts = [w.cget("text") for w in card.winfo_children() if w.winfo_class() == "Label"]
    assert "Codex wants extra permissions" in texts and "network access" in texts


# ── the one-time "Codex mode is here" note for installs updating to it ──

@pytest.mark.parametrize("installed,auth_file,expect", [
    (False, False, "npm install -g @openai/codex"),
    (True, True, "nothing to set up"),
    (True, False, "Sign in once first with  codex login"),
])
def test_codex_mode_is_introduced_once(overlay, tmp_path, monkeypatch, installed, auth_file, expect):
    home = tmp_path / "codexhome"
    home.mkdir()
    if auth_file:
        (home / "auth.json").write_text("{}", "utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    overlay._cli_ok = {"claude": True, "codex": installed}
    co._save_state(codex_intro_shown=None)
    overlay._maybe_announce_codex()
    assert expect in chat_text(overlay)
    before = chat_text(overlay)
    overlay._maybe_announce_codex()                  # once per install, never again
    assert chat_text(overlay) == before
