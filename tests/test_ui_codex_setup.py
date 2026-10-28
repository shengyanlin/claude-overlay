"""The one-click "Set up Codex" button (install-codex.cmd --from-app).

Field report behind it: an install updated from 1.23 to 1.25 had no codex CLI, so the Codex
side of the titlebar switch was struck through, and all the overlay offered was a terminal
command to type. Now every place that hits a missing or signed-out Codex — a click on the
struck-through side, the one-time introduction, a sign-out mid-session — carries a button
that runs the shared installer in its own console and reports its exit code in words.
install-codex.cmd is never spawned: run_codex_setup is stubbed wherever a click runs."""
import tkinter as tk

import claude_overlay as co
from conftest import chat_text


def _drain(ov):
    while True:
        try:
            ov.ui_q.get_nowait()
        except Exception:
            return


def _buttons(ov):
    """(canvas, its drawn label) for every embedded button, in reading order — Tk lists
    embedded windows in no particular order, so [-1] is only "the newest" after sorting."""
    out = []
    names = sorted(ov.chat.window_names(),
                   key=lambda n: tuple(int(x) for x in ov.chat.index(n).split(".")))
    for name in names:
        w = ov.root.nametowidget(name)
        if isinstance(w, tk.Canvas):
            texts = [w.itemcget(i, "text") for i in w.find_all() if w.type(i) == "text"]
            out.append((w, " ".join(texts)))
    return out


class _RunNow:
    """Runs the click's background work inline, so its result is queued before we look."""
    def __init__(self, target=None, **k):
        self.target = target

    def start(self):
        self.target()


def _no_codex(monkeypatch, overlay):
    state = {"codex": False}
    monkeypatch.setattr(co.Overlay, "_cli_available",
                        lambda self, p: state["codex"] if p == "codex" else True)
    monkeypatch.setattr(co.Overlay, "_can_setup_codex", lambda self: True)
    overlay._cli_ok = {"claude": True, "codex": False}
    overlay._lock_told = 0.0
    return state


def test_clicking_the_missing_side_offers_the_setup_button(overlay, monkeypatch):
    _no_codex(monkeypatch, overlay)
    assert overlay.switch_provider("codex") is False
    assert overlay.provider == "claude"
    assert "Can't switch to Codex yet" in chat_text(overlay)
    labels = [t for _, t in _buttons(overlay)]
    assert any("Install Codex CLI & sign in" in t for t in labels)


def test_without_the_script_it_falls_back_to_the_commands(overlay, monkeypatch):
    _no_codex(monkeypatch, overlay)
    monkeypatch.setattr(co.Overlay, "_can_setup_codex", lambda self: False)
    before = list(overlay.chat.window_names())
    overlay.switch_provider("codex")
    assert "npm install -g @openai/codex" in chat_text(overlay)
    assert list(overlay.chat.window_names()) == before


def test_a_finished_setup_unlocks_the_switch_and_done_switches(overlay, monkeypatch):
    state = _no_codex(monkeypatch, overlay)

    def fake_setup():
        state["codex"] = True            # the console installed it and signed in
        return 0
    monkeypatch.setattr(co, "run_codex_setup", fake_setup)
    monkeypatch.setattr(co.threading, "Thread", _RunNow)
    overlay.switch_provider("codex")
    btn = [c for c, t in _buttons(overlay) if "Install Codex CLI" in t][-1]
    _drain(overlay)
    btn._click(None)
    kind, payload = overlay.ui_q.get_nowait()
    assert (kind, payload) == ("codex_setup_result", (True, "0"))
    overlay._handle(kind, payload)
    assert btn._ustate == "done"
    assert overlay._cli_ok["codex"] is True
    assert "Codex is set up" in chat_text(overlay)
    btn._click(None)                     # "✓ Codex is ready — switch to it"
    assert overlay.provider == "codex"


def test_installed_but_not_signed_in_still_unlocks_and_offers_a_retry(overlay, monkeypatch):
    state = _no_codex(monkeypatch, overlay)
    overlay.switch_provider("codex")
    btn = [c for c, _ in _buttons(overlay)][-1]
    overlay._codex_setup_btn_ref = btn
    state["codex"] = True
    overlay._handle("codex_setup_result", (False, "2"))
    assert btn._ustate == "error"
    assert overlay._cli_ok["codex"] is True          # the CLI did install
    assert "not signed in yet" in chat_text(overlay)


def test_a_failed_install_says_how_to_do_it_by_hand(overlay, monkeypatch):
    _no_codex(monkeypatch, overlay)
    overlay._handle("codex_setup_result", (False, "3"))
    assert "didn't install" in chat_text(overlay)
    assert "npm install -g @openai/codex" in chat_text(overlay)
    overlay._handle("codex_setup_result", (False, ""))
    assert "Couldn't start install-codex.cmd" in chat_text(overlay)


def test_a_second_offer_while_one_runs_does_not_stack_a_button(overlay, monkeypatch):
    _no_codex(monkeypatch, overlay)
    overlay.switch_provider("codex")
    btn = [c for c, _ in _buttons(overlay)][-1]
    btn._set_ustate("working")
    overlay._codex_setup_btn_ref = btn
    n = len(_buttons(overlay))
    overlay._lock_told = 0.0
    overlay.switch_provider("codex")
    assert len(_buttons(overlay)) == n
    assert "already running in its console window" in chat_text(overlay)


class _HoldThread:
    """Starts nothing, so the first click's setup stays 'running'; counts the starts."""
    started = []

    def __init__(self, target=None, **k):
        pass

    def start(self):
        _HoldThread.started.append(1)


def test_two_offers_already_on_screen_still_run_one_setup_at_a_time(overlay, monkeypatch):
    """The introduction's button and the struck-through side's button can both be there;
    clicking both must not open two installers whose results land on one button."""
    _no_codex(monkeypatch, overlay)
    overlay._offer_codex_setup("first offer")
    overlay._offer_codex_setup("second offer")
    first, second = [c for c, _ in _buttons(overlay)][-2:]
    _HoldThread.started = []
    monkeypatch.setattr(co.threading, "Thread", _HoldThread)
    first._click(None)
    second._click(None)
    assert _HoldThread.started == [1]
    assert first._ustate == "working" and second._ustate == "idle"
    assert overlay._codex_setup_btn_ref is first
    assert "already running in its console window" in chat_text(overlay)


def test_a_mid_session_sign_out_offers_sign_in_and_success_rechecks(overlay, monkeypatch):
    monkeypatch.setattr(co.Overlay, "_can_setup_codex", lambda self: True)
    assert overlay.switch_provider("codex", force=True)
    overlay._auth_dead = False
    overlay._handle("codex_auth", {"ok": False})
    assert overlay._auth_dead is True
    btn, label = _buttons(overlay)[-1]
    assert "Sign in to Codex" in label
    rechecked = []
    overlay.worker.recheck_auth = lambda: rechecked.append(1)
    overlay._codex_setup_btn_ref = btn
    overlay._handle("codex_setup_result", (True, "0"))
    assert rechecked == [1]
    assert "Codex is signed in" in chat_text(overlay)
