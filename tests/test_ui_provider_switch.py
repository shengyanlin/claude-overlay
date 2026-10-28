"""The titlebar's Claude ⇄ Codex switch: a live swap of agent AND look, with no relaunch.

What has to hold: the palettes can be mapped onto each other role for role; after a switch
no widget still wears the other agent's colours (except the collapsed orb, which keeps the
Clawd look on purpose); the old worker can't write into the new conversation; and the
Codex-only pieces (model list, ⚡ levels, allowance, approval cards) are wired up."""
import tkinter as tk

import pytest

import claude_overlay as co
from conftest import FakeWorker, chat_text


def _palette_pairs():
    for theme in ("light", "dark"):
        yield theme, co.THEMES[theme], co.CODEX_THEMES[theme]


@pytest.mark.parametrize("theme,claude,codex", list(_palette_pairs()))
def test_palettes_share_their_collisions(theme, claude, codex):
    """_recolor maps by VALUE, so two roles equal in one palette must be equal in the other —
    otherwise a recoloured widget can't know which role it was painted with."""
    roles = sorted(set(claude) & set(codex))
    assert set(claude) <= set(codex), "every Claude role needs a Codex counterpart"
    for i, a in enumerate(roles):
        for b in roles[i + 1:]:
            assert (claude[a].lower() == claude[b].lower()) == (codex[a].lower() == codex[b].lower()), \
                f"{theme}: {a}/{b} collide in one palette only"


def _colours_in_use(ov):
    """Every colour value on screen, minus the collapsed orb (deliberately Claude's)."""
    seen = set()
    stack = [ov.root]
    skip = {ov.orb, ov.orb_name}
    while stack:
        w = stack.pop()
        if w in skip:
            continue
        for opt in co._COLOR_OPTS:
            try:
                seen.add(str(w.cget(opt)).lower())
            except tk.TclError:
                pass
        if w.winfo_class() == "Canvas":
            for item in w.find_all():
                for opt in co._ITEM_COLOR_OPTS:
                    try:
                        seen.add(str(w.itemcget(item, opt)).lower())
                    except tk.TclError:
                        pass
        elif w.winfo_class() == "Text":
            for tag in w.tag_names():
                for opt in co._TAG_COLOR_OPTS:
                    try:
                        seen.add(str(w.tag_cget(tag, opt)).lower())
                    except tk.TclError:
                        pass
        stack.extend(w.winfo_children())
    return seen


def _only_in(a, b):
    return {v.lower() for v in a.values()} - {v.lower() for v in b.values()}


def test_switch_to_codex_and_back_recolours_everything(overlay):
    claude, codex = overlay._palette_for("claude"), overlay._palette_for("codex")
    overlay.add_user("before the switch")          # an embedded bubble, chips etc. on screen
    overlay.add_tool("Bash", {"command": "dir"})
    overlay.switch_provider("codex", force=True)   # content on screen: the lock is tested below
    assert overlay.provider == "codex"
    assert dict(co.T) == codex
    leftover = _colours_in_use(overlay) & _only_in(claude, codex)
    assert not leftover, f"Claude colours still on screen: {leftover}"
    overlay.add_user("after the switch")
    overlay.switch_provider("claude", force=True)
    assert dict(co.T) == claude
    leftover = _colours_in_use(overlay) & _only_in(codex, claude)
    assert not leftover, f"Codex colours still on screen: {leftover}"


def test_switch_swaps_the_worker_names_and_remembers(overlay):
    old = overlay.worker
    overlay.read_only = True                        # the new agent starts in the same lock
    overlay.switch_provider("codex")
    assert ("interrupt", ()) in old.calls and ("shutdown", ()) in old.calls
    assert overlay.worker is not old and ("start", ()) in overlay.worker.calls
    assert overlay.worker.permission_mode == "plan"
    assert overlay.title_lbl.cget("text") == "Codex"
    assert overlay.root.title() == "Codex"
    assert overlay.entry.get("1.0", "end-1c") == co.CODEX_PLACEHOLDER
    assert co._load_state().get("provider") == "codex"
    assert ">_ Codex" in chat_text(overlay)
    overlay.switch_provider("claude")
    assert overlay.title_lbl.cget("text") == "Claude"
    assert overlay.entry.get("1.0", "end-1c") == co.PLACEHOLDER
    assert co._load_state().get("provider") == "claude"


def test_a_custom_name_survives_the_switch(overlay):
    overlay._apply_name("Deck review")
    overlay.switch_provider("codex")
    assert overlay.title_lbl.cget("text") == "Deck review"


def test_the_old_worker_cannot_write_into_the_new_conversation(overlay):
    old = overlay.worker
    old.ui.put(("delta", "QUEUED-BEFORE"))          # already in ui_q at switch time
    overlay.switch_provider("codex")
    old.ui.put(("delta", "LATE-AFTER"))             # a put from a worker that was told to stop
    overlay.ui_q.put(("precapture_done", None))     # a UI-thread event must survive the purge
    overlay._poll()
    text = chat_text(overlay)
    assert "QUEUED-BEFORE" not in text and "LATE-AFTER" not in text
    overlay.worker.ui.put(("delta", "FROM-CODEX"))
    overlay._poll()
    assert "FROM-CODEX" in chat_text(overlay)


def test_collapsed_orb_keeps_the_clawd_look(overlay):
    before = (overlay.orb.cget("bg"), overlay.orb_name.cget("bg"))
    overlay.switch_provider("codex")
    assert (overlay.orb.cget("bg"), overlay.orb_name.cget("bg")) == before
    assert before[0] == co.THEMES[co.THEME]["bg"]


def test_switch_segment_click(overlay):
    overlay._paint_provider_switch()
    spans = dict((k, (x0, x1)) for k, x0, x1 in overlay._switch_spans)
    ev = type("E", (), {"x": (spans["codex"][0] + spans["codex"][1]) / 2})()
    overlay._provider_switch_click(ev)
    assert overlay.provider == "codex"
    overlay._provider_switch_click(ev)               # the live segment: nothing happens
    assert overlay.provider == "codex"


def test_codex_models_menu_and_label(overlay):
    overlay.switch_provider("codex")
    assert overlay._menu_models() == []
    overlay._handle("codex_models", [
        {"id": "gpt-x", "label": "GPT-X", "efforts": ["low", "high"]},
        {"id": "gpt-y", "label": "GPT-Y", "efforts": ["low", "ultra"]}])
    overlay._handle("model", "gpt-y")
    assert overlay._menu_models() == [("GPT-X", "gpt-x"), ("GPT-Y", "gpt-y")]
    assert overlay.statusline.cget("text").startswith("GPT-Y")
    values = [v for _, v in overlay._effort_levels()]
    assert values == ["low", "ultra", "auto", ""]      # what THIS model accepts, Auto, default
    overlay._switch_model("gpt-x")
    assert ("set_model", ("gpt-x",)) in overlay.worker.calls


def test_codex_effort_is_remembered_separately(overlay):
    overlay.switch_provider("codex")
    overlay._handle("effort", "high")
    assert co._load_state().get("codex_effort") == "high"
    assert co._startup_effort("codex") == "high"
    overlay.switch_provider("claude")
    assert overlay.effort == co._startup_effort("claude")


def test_codex_allowance_feeds_the_ring_and_claude_polls_are_ignored(overlay):
    overlay.switch_provider("codex")
    overlay._handle("quota_poll", {"windows": {"five_hour": {"utilization": 0.9}}})
    assert overlay._quota_polled is None               # Claude's number, not Codex's
    overlay._handle("codex_quota", {"windows": {
        "five_hour": {"utilization": 0.4, "resets_at": None},
        "codex_limit": {"utilization": 0.28, "resets_at": None}}, "status": None})
    arcs = overlay._ring_arcs()
    assert arcs["five_hour"][0] == pytest.approx(0.4)
    assert arcs["week"][0] == pytest.approx(0.28)      # the outer track takes the seat limit
    assert "5h 40%" in overlay.quota_lbl.cget("text")


def test_approval_card_answers_and_retires(overlay):
    overlay.switch_provider("codex")
    overlay._handle("approval", {"rid": 7, "kind": "command", "command": "Remove-Item build",
                                 "cwd": "C:\\work", "reason": "needs write"})
    assert 7 in overlay._approval_cards
    card = overlay._approval_cards[7]["card"]
    texts = [w.cget("text") for w in card.winfo_children() if isinstance(w, tk.Label)]
    assert "Codex wants to run a command" in texts and "Remove-Item build" in texts
    overlay._answer_approval(7, "accept")
    assert ("respond_approval", (7, "accept")) in overlay.worker.calls
    assert 7 not in overlay._approval_cards
    assert overlay._approval_cards == {}


def test_approval_resolved_elsewhere_and_expiry(overlay):
    overlay.switch_provider("codex")
    overlay._handle("approval", {"rid": 1, "kind": "files", "files": ["a.txt"]})
    overlay._handle("approval", {"rid": 2, "kind": "command", "command": "x"})
    overlay._handle("approval_resolved", 1)
    assert list(overlay._approval_cards) == [2]
    overlay._handle("turn_done", None)
    assert overlay._approval_cards == {}
    assert not any(c[0] == "respond_approval" for c in overlay.worker.calls)


def test_resume_record_is_per_agent(overlay):
    overlay.switch_provider("codex")
    overlay._session_id = "th-42"
    overlay._persist_session()
    st = co._load_state()
    assert st["codex_last_session"]["id"] == "th-42"
    assert (st.get("last_session") or {}).get("id") != "th-42"


def test_claude_login_watchdog_is_off_in_codex_mode(overlay, monkeypatch):
    monkeypatch.setattr(co.authstate, "dead_reason", lambda: "blank")
    overlay.switch_provider("codex")
    overlay.entry.delete("1.0", "end")
    overlay.entry.insert("1.0", "hi")
    overlay._ph_active = False
    assert overlay._auth_blocks_send() is False
    overlay._auth_watchdog(10 ** 9)
    assert overlay._auth_dead is False


def test_queued_messages_are_handed_back_on_a_forced_switch(overlay):
    overlay._queue = [{"text": "queued one", "images": [], "docs": []}]
    overlay.switch_provider("codex", force=True)
    assert "Switched to Codex" in chat_text(overlay) and "queued one" in chat_text(overlay)


# ── the switch only works on an EMPTY conversation ──

def _click(overlay, key):
    overlay._paint_provider_switch()
    x0, x1 = next((a, b) for k, a, b in overlay._switch_spans if k == key)
    overlay._lock_told = 0.0
    return overlay._provider_switch_click(type("E", (), {"x": (x0 + x1) / 2})())


def test_a_sent_message_locks_the_switch_until_clear(overlay):
    old = overlay.worker
    overlay.add_user("what's on my screen?")
    assert overlay.provider_switch.cget("cursor") == "arrow"
    _click(overlay, "codex")
    assert overlay.provider == "claude" and overlay.worker is old
    assert ("shutdown", ()) not in old.calls
    assert "click Clear first" in chat_text(overlay)
    overlay.reset()                                   # Clear → empty again → unlocked
    assert overlay.provider_switch.cget("cursor") == "hand2"
    _click(overlay, "codex")
    assert overlay.provider == "codex"


def test_no_switch_while_a_reply_is_streaming(overlay):
    overlay._set_busy(True)
    assert overlay.switch_provider("codex") is False
    assert overlay.provider == "claude"
    assert "let this reply finish" in chat_text(overlay)


def test_queued_or_resumed_conversations_count_as_content(overlay):
    overlay._queue = [{"text": "later", "images": [], "docs": []}]
    assert overlay._switch_locked() == "content"
    overlay._queue = []
    overlay._handle("resumed", None)
    assert overlay._switch_locked() == "content"


def test_typed_text_and_attachments_do_not_lock_and_come_along(overlay):
    overlay._ph_out()
    overlay.entry.insert("1.0", "half a question")
    overlay._ph_active = False
    overlay.pending_images = ["C:/shot.png"]
    assert overlay._switch_locked() is None
    assert overlay.switch_provider("codex") is True
    assert overlay.entry.get("1.0", "end-1c") == "half a question"
    assert overlay.pending_images == ["C:/shot.png"]


def test_the_lock_note_is_not_repeated_on_a_double_click(overlay):
    overlay.add_user("q")
    overlay._lock_told = 0.0
    overlay.switch_provider("codex")
    overlay.switch_provider("codex")
    assert chat_text(overlay).count("click Clear first") == 1


# ── a CLI that isn't installed can't be switched to ──

def _only(monkeypatch, *installed):
    monkeypatch.setattr(co.Overlay, "_cli_available", lambda self, p: p in installed)


def test_missing_codex_cli_disables_its_segment_and_says_how_to_install(overlay, monkeypatch):
    _only(monkeypatch, "claude")
    overlay._refresh_cli_ok()
    overlay._paint_provider_switch()
    assert overlay.provider_switch.cget("cursor") == "arrow"
    old = overlay.worker
    _click(overlay, "codex")
    assert overlay.provider == "claude" and overlay.worker is old
    text = chat_text(overlay)
    assert "Codex CLI (`codex`) isn't installed" in text
    assert "npm install -g @openai/codex" in text and "codex login" in text


def test_installing_the_cli_while_running_unlocks_it(overlay, monkeypatch):
    _only(monkeypatch, "claude")
    _click(overlay, "codex")
    assert overlay.provider == "claude"
    _only(monkeypatch, "claude", "codex")             # installed in a terminal meanwhile
    _click(overlay, "codex")
    assert overlay.provider == "codex"


def test_missing_claude_cli_when_in_codex_mode(overlay, monkeypatch):
    overlay.switch_provider("codex")
    _only(monkeypatch, "codex")
    _click(overlay, "claude")
    assert overlay.provider == "codex"
    text = chat_text(overlay)
    assert "Claude Code CLI (`claude`) isn't installed" in text
    assert "irm https://claude.ai/install.ps1" in text


def test_missing_cli_wins_over_the_empty_conversation_rule(overlay, monkeypatch):
    """With content AND no CLI, the message must be about the CLI — Clearing wouldn't help."""
    _only(monkeypatch, "claude")
    overlay.add_user("q")
    _click(overlay, "codex")
    text = chat_text(overlay)
    assert "isn't installed" in text and "click Clear first" not in text


@pytest.mark.parametrize("wanted,ok,expect,noted", [
    ("codex", {"claude": True, "codex": True}, "codex", False),
    ("codex", {"claude": True, "codex": False}, "claude", True),    # codex uninstalled
    ("claude", {"claude": False, "codex": True}, "codex", True),    # Codex-only machine
    ("codex", {"claude": False, "codex": False}, "codex", False),   # neither: keep the wish
])
def test_startup_opens_on_an_installed_agent(wanted, ok, expect, noted):
    got, note = co._resolve_startup_provider(wanted, ok)
    assert got == expect
    assert bool(note) is noted
    if note:
        assert "isn't installed" in note
