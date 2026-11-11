"""The collapsed orb as a desktop pet, wired into the Overlay (orbpet.py holds the logic).

The rule these tests guard is that the pet may not cost the overlay anything: no timer
while expanded, nothing drawn on a tick, and an off switch that really switches it off."""
import time

import pytest

import orbpet as op


@pytest.fixture
def pet(overlay):
    """The shared overlay with frames already drawn, so nothing waits on the render thread."""
    if not overlay._pet_frames:
        overlay._pet_frames = op.render_all(overlay.orb_size)
    overlay.orb_pet = True
    return overlay


def _gear(ov):
    return {lbl.strip("✓ "): (lbl, cmd) for lbl, cmd in ov._gear_items()}


# ── the switch ──────────────────────────────────────────────────────────────────────────
def test_setting_is_in_the_gear_menu_with_the_other_toggles(overlay):
    rows = [lbl.strip("✓ ") for lbl, _ in overlay._gear_items()]
    assert rows.index("Animated icon") < rows.index(overlay.GEAR_SEP)   # a setting, not an action
    lbl, cmd = _gear(overlay)["Animated icon"]
    assert cmd == overlay.toggle_orb_pet
    overlay.orb_pet = True
    assert "✓" in _gear(overlay)["Animated icon"][0]
    overlay.orb_pet = False
    assert "✓" not in _gear(overlay)["Animated icon"][0]


def test_toggle_is_remembered(pet):
    import claude_overlay as co
    pet.toggle_orb_pet()
    assert pet.orb_pet is False and co._load_state().get("orb_pet") is False
    pet.toggle_orb_pet()
    assert pet.orb_pet is True and co._load_state().get("orb_pet") is True


# ── costs nothing when it should cost nothing ───────────────────────────────────────────
def test_no_timer_while_expanded(pet):
    assert pet.expanded and pet._pet_after is None
    pet.toggle_collapse()
    assert pet._pet_after is not None
    pet.toggle_collapse()
    assert pet.expanded and pet._pet_after is None


def test_switched_off_never_arms_a_timer_and_shows_the_still_icon(pet):
    pet.orb_pet = False
    pet.toggle_collapse()
    assert pet._pet_after is None
    assert not pet._pet_live()
    assert pet._pet_region(pet.orb_size, pet.orb_size) is None       # still silhouette path
    pet._pet_tick()                                                  # a stray tick does nothing
    assert pet._pet_after is None


def test_switching_off_while_collapsed_stops_it_at_once(pet):
    pet.toggle_collapse()
    assert pet._pet_after is not None
    pet.toggle_orb_pet()
    assert pet._pet_after is None and not pet._pet_live()


def test_switching_on_while_collapsed_starts_it(pet):
    pet.orb_pet = False
    pet.toggle_collapse()
    pet.toggle_orb_pet()
    assert pet._pet_after is not None


def test_a_tick_only_swaps_a_ready_image(pet, monkeypatch):
    """Nothing is rendered on the UI thread once the frames exist."""
    pet.toggle_collapse()
    pet._pet_tick()                                   # first frame (may build its PhotoImage)
    for pose in op.all_poses():
        pet._pet_photo(pose)                          # warm every PhotoImage
    monkeypatch.setattr(op, "render_all", lambda *a, **k: pytest.fail("rendered on a tick"))
    monkeypatch.setattr(op, "render_art", lambda *a, **k: pytest.fail("rendered on a tick"))
    made = []
    import claude_overlay as co
    real = co.ImageTk.PhotoImage
    monkeypatch.setattr(co.ImageTk, "PhotoImage", lambda *a, **k: made.append(1) or real(*a, **k))
    pet._pet_brain.set_busy(True, time.monotonic())
    for _ in range(12):
        pet._pet_stop()
        pet._pet_tick()
    assert made == []


def test_blink_leaves_the_window_region_alone(pet, monkeypatch):
    pet.toggle_collapse()
    pet._pet_show(op.REST)
    calls = []
    monkeypatch.setattr(pet, "_pet_set_region", lambda: calls.append(1))
    pet._pet_show(op.SHUT)
    pet._pet_show(op.LOOK_L)
    pet._pet_show(op.REST)
    assert calls == []
    pet._pet_show(op.ARMS_UP)                         # the outline moved: one region update
    assert calls == [1]


def test_tick_cost_is_far_below_a_frame(pet):
    """Generous bound (CI machines vary): a tick averages well under 2 ms."""
    pet.toggle_collapse()
    pet._pet_brain.set_busy(True, time.monotonic())   # the busiest clip: outline moves per frame
    for _ in range(10):                               # warm the caches
        pet._pet_stop()
        pet._pet_tick()
    t0 = time.perf_counter()
    n = 200
    for _ in range(n):
        pet._pet_stop()
        pet._pet_tick()
    assert (time.perf_counter() - t0) / n < 0.002


# ── behaviour ───────────────────────────────────────────────────────────────────────────
def test_region_follows_the_frame_and_is_a_fresh_handle_each_time(pet):
    import claude_overlay as co
    pet.toggle_collapse()
    pet._pet_show(op.ARMS_UP)
    s = pet.orb_size
    a, b = pet._pet_region(s, s), pet._pet_region(s, s)
    try:
        assert a and b and a != b                     # Windows takes ownership: never the master
        assert a not in pet._pet_rgns.values()
        assert pet._pet_region(s + 1, s) is None      # wrong window size → still fallback
    finally:
        for r in (a, b):
            if r:
                co._gdi32.DeleteObject(r)


def test_named_overlay_keeps_its_name_in_the_region(pet):
    import claude_overlay as co
    pet.overlay_name = "Deck review"
    pet.toggle_collapse()
    pet._pet_show(op.REST)
    W, H, x_orb = pet._collapsed_layout()
    assert H > pet.orb_size and pet._pet_extras_rgn()
    rgn = pet._pet_region(W, H)
    try:
        assert rgn
        box = co.wt.RECT()
        co._gdi32.GetRgnBox(rgn, co.ctypes.byref(box))
        assert box.bottom > pet.orb_size              # reaches down over the name
    finally:
        co._gdi32.DeleteObject(rgn)


def test_reply_landing_while_collapsed_makes_it_cheer(pet):
    pet.toggle_collapse()
    pet._pet_tick()
    pet._turn_raw = "done"
    pet._maybe_flag_done()
    assert pet._pet_pose.arm_l and pet._pet_pose.arm_r           # kicked: already showing it
    assert pet._task_done_badge


def test_busy_reaches_the_brain(pet):
    pet.toggle_collapse()
    pet._set_busy(True)
    assert pet._pet_brain.busy and any(pet._pet_pose.legs)
    pet._set_busy(False)
    assert not pet._pet_brain.busy and pet._pet_pose == op.REST


def test_hover_waves_instead_of_redrawing(pet, monkeypatch):
    pet.toggle_collapse()
    pet._pet_tick()
    monkeypatch.setattr(pet, "_draw_orb", lambda *a, **k: pytest.fail("still-icon hover path"))
    pet._orb_hover(True)
    assert pet._pet_pose.arm_r == 1
    pet._orb_hover(False)


def test_drag_picks_it_up_and_release_puts_it_down(pet):
    class E:
        x_root, y_root = 400, 300
    pet.toggle_collapse()
    pet._pet_tick()
    pet._orb_press(E)
    pet._orb_drag(E)
    assert pet._pet_brain.held and pet._pet_pose.arm_l
    pet._orb_release(E)
    assert not pet._pet_brain.held and not pet.expanded           # a drag is not a click


def test_custom_artwork_stays_still(pet, monkeypatch):
    import claude_overlay as co
    monkeypatch.setattr(co, "ORB_IMAGE", "my_logo.png")
    pet.toggle_collapse()
    assert pet._pet_after is None and not pet._pet_live()
