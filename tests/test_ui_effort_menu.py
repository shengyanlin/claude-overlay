"""The ⚡ effort switcher on the statusline.

Effort was a config constant applied at connect and otherwise invisible; a user whose CLI
sits at xhigh paid 14-24s per one-line overlay reply (measured 2026-10-14) with no way to
see why or change it without editing config.json. Now it is a statusline segment with a
popup, like the model switcher. These pin the three contracts that make it trustworthy:
the tick and label follow the worker's CONFIRMATION, never the click; a confirmed change
is remembered across launches; a remembered value is only honoured if the menu offers it.
"""
import json

import pytest

import claude_overlay as co


@pytest.fixture
def ov(overlay):
    """The session Overlay with effort state saved and restored around the test, and the
    FakeWorker's call log cleared, so one test's switch can't leak into the next."""
    before = overlay.effort
    overlay.worker.calls.clear()
    busy = overlay.busy
    yield overlay
    overlay.effort = before
    overlay.busy = busy
    overlay._refresh_statusline()


def _ticked(ov):
    return [v for lbl, v in ov._effort_items() if lbl.startswith("✓")]


def test_menu_lists_every_level_in_config_order(ov):
    assert [v for _, v in ov._effort_items()] == [v for _, v in co.EFFORT_LEVELS]


def test_exactly_the_active_level_is_ticked(ov):
    ov.effort = "medium"
    assert _ticked(ov) == ["medium"]
    ov.effort = ""                                   # "inherit" is a level too
    assert _ticked(ov) == [""]


def test_label_shows_the_active_level(ov):
    ov.effort = "high"
    ov._refresh_statusline()
    assert "⚡ high" in ov.effort_lbl.cget("text")
    ov.effort = ""
    ov._refresh_statusline()
    assert "⚡ default" in ov.effort_lbl.cget("text")


class _FakeMenu:
    built = []

    def __init__(self, *a, **k):
        self.items = []
        _FakeMenu.built.append(self)

    def add_command(self, label=None, command=None, **k):
        self.items.append((label, command))

    def tk_popup(self, *a, **k):
        pass

    def grab_release(self):
        pass


def test_popup_row_asks_the_worker_and_does_not_flip_the_label(ov, monkeypatch):
    ov.effort = "low"
    ov._refresh_statusline()          # paint the starting label; the assertion below is that
                                      # the CLICK doesn't repaint it (the test once passed only
                                      # because "low" happened to be the shipped default)
    _FakeMenu.built = []
    monkeypatch.setattr(co.tk, "Menu", _FakeMenu)

    class E:
        x_root = y_root = 10
    ov._effort_menu(E())

    assert len(_FakeMenu.built) == 1
    rows = _FakeMenu.built[0].items
    assert [lbl.strip("✓ ") for lbl, _ in rows] == [lbl for lbl, _ in co.EFFORT_LEVELS]
    rows[2][1]()                                     # "High" — each command carries its own value
    assert ("set_effort", ("high",)) in ov.worker.calls
    assert ov.effort == "low"                        # the label waits for the confirmation
    assert "⚡ low" in ov.effort_lbl.cget("text")


def test_picking_the_ticked_row_is_a_no_op(ov):
    ov.effort = "low"
    ov._switch_effort("low")
    assert ov.worker.calls == []                     # no relaunch for no change


def test_mid_reply_switch_is_deferred(ov):
    ov.effort = "low"
    ov.busy = True
    ov._switch_effort("high")
    assert ov.worker.calls == []
    assert "Finish (or Stop)" in ov.chat.get("1.0", "end")


def test_confirmation_updates_label_and_persists(ov):
    ov.effort = "low"
    ov._apply_effort("xhigh")
    assert ov.effort == "xhigh"
    assert "⚡ xhigh" in ov.effort_lbl.cget("text")
    assert json.loads(co.STATE_FILE.read_text("utf-8"))["effort"] == "xhigh"
    assert "Effort is now Extra high" in ov.chat.get("1.0", "end")


def test_confirmation_of_the_same_level_persists_nothing(ov, monkeypatch):
    # The worker re-sends the OLD level when a relaunch fails; that must not be written as
    # a "choice", and must not add a chat line claiming a change that didn't happen.
    ov.effort = "low"
    writes = []
    monkeypatch.setattr(co, "_save_state", lambda **kw: writes.append(kw))
    ov._apply_effort("low")
    assert writes == []


class TestStartupEffort:
    def _state(self, tmp_path, monkeypatch, **state):
        p = tmp_path / "state.json"
        p.write_text(json.dumps(state), "utf-8")
        monkeypatch.setattr(co, "STATE_FILE", p)

    def test_remembered_level_wins_over_config(self, tmp_path, monkeypatch):
        self._state(tmp_path, monkeypatch, effort="xhigh")
        assert co._startup_effort() == "xhigh"

    def test_remembered_inherit_is_honoured(self, tmp_path, monkeypatch):
        # "" is a real choice (Default row), distinct from "nothing remembered".
        self._state(tmp_path, monkeypatch, effort="")
        assert co._startup_effort() == ""

    def test_unknown_remembered_value_falls_back_to_config(self, tmp_path, monkeypatch):
        # A stale or hand-edited state must not launch the CLI with a value --effort rejects.
        self._state(tmp_path, monkeypatch, effort="ultra")
        assert co._startup_effort() == co.EFFORT

    def test_nothing_remembered_uses_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(co, "STATE_FILE", tmp_path / "absent.json")
        assert co._startup_effort() == co.EFFORT


def test_worker_is_launched_with_the_startup_effort(overlay):
    # The remembered level has to reach the CLI's command line, which means the worker's
    # constructor — not a set_effort after connect (that would relaunch for nothing).
    assert overlay.worker.effort == overlay.effort
