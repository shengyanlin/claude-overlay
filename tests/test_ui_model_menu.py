"""The model switcher offers only models this login can actually pick.

The overlay shipped one hardcoded entry per family, so a colleague whose account has no
Fable was still offered Fable — and choosing it changed nothing visible, because the CLI
does not error on an unentitled --model, it silently runs the account default. From the
user's side that is indistinguishable from a broken overlay. These tests pin the menu to
the CLI's own entitlement record, and pin the three ways it must degrade rather than hide
a model someone actually has.
"""
import json

import pytest

import claude_overlay as co
import modelresolve as mr


@pytest.fixture(autouse=True)
def _clear_ent_memo():
    mr._ENT_MEMO["key"] = mr._ENT_MEMO["families"] = None
    yield
    mr._ENT_MEMO["key"] = mr._ENT_MEMO["families"] = None


def _entitlement(tmp_path, monkeypatch, *families):
    """Point modelresolve at a .claude.json entitling exactly these families."""
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"modelAccessCache": [
        {"apiName": f"claude-{f}-9", "entitled": True} for f in families]}), "utf-8")
    monkeypatch.setattr(mr, "_CLAUDE_JSON", p)
    return p


def _labels(ov):
    return [lbl for lbl, _ in ov._menu_models()]


def test_menu_hides_a_family_this_login_lacks(overlay, tmp_path, monkeypatch):
    _entitlement(tmp_path, monkeypatch, "opus", "sonnet", "haiku")
    labels = _labels(overlay)
    assert "Fable" not in labels and "Fable (1M)" not in labels
    assert labels == ["Opus", "Opus (1M)", "Sonnet", "Haiku"]


def test_menu_shows_a_family_this_login_has(overlay, tmp_path, monkeypatch):
    _entitlement(tmp_path, monkeypatch, "opus", "fable", "sonnet", "haiku")
    assert _labels(overlay) == [lbl for lbl, _ in co.MODELS]


def test_menu_is_unfiltered_when_entitlement_is_unreadable(overlay, tmp_path, monkeypatch):
    # Missing file = "unknown", NOT "nothing available": never hide on a failed reading.
    monkeypatch.setattr(mr, "_CLAUDE_JSON", tmp_path / "absent.json")
    assert _labels(overlay) == [lbl for lbl, _ in co.MODELS]


def test_menu_is_unfiltered_when_the_reading_raises(overlay, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("entitlement reading exploded")
    monkeypatch.setattr(co.modelresolve, "available_models", boom)
    assert _labels(overlay) == [lbl for lbl, _ in co.MODELS]


def test_filter_can_be_switched_off(overlay, tmp_path, monkeypatch):
    # The escape hatch for an entitlement the CLI's cache hasn't caught up with yet.
    _entitlement(tmp_path, monkeypatch, "opus")
    monkeypatch.setattr(co, "MODEL_MENU_FILTER", False)
    assert _labels(overlay) == [lbl for lbl, _ in co.MODELS]


class _FakeMenu:
    """Records what the popup would contain; pops nothing (a real tk_popup grabs the
    screen and blocks the suite)."""
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


def test_popup_is_built_from_the_filtered_list(overlay, tmp_path, monkeypatch):
    _entitlement(tmp_path, monkeypatch, "opus", "sonnet", "haiku")
    _FakeMenu.built = []
    monkeypatch.setattr(co.tk, "Menu", _FakeMenu)

    class E:
        x_root = y_root = 10
    overlay._model_menu(E())

    assert len(_FakeMenu.built) == 1
    labels = [lbl for lbl, _ in _FakeMenu.built[0].items]
    assert labels == ["Opus", "Opus (1M)", "Sonnet", "Haiku"]
    # …and the entries still switch: each command carries its own spec (late-binding bug).
    _FakeMenu.built[0].items[2][1]()          # "Sonnet"
    assert ("set_model", ("sonnet",)) in overlay.worker.calls
