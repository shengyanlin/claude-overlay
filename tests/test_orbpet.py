"""orbpet.py — the collapsed orb's pet: what it draws and what it decides. No Tk here."""
import random

import pytest

import orbpet as op


@pytest.fixture(scope="module")
def frames():
    return op.render_all(56)


# ── artist ────────────────────────────────────────────────────────────────────────────
def test_every_pose_a_clip_uses_is_rendered(frames):
    assert set(frames) == set(op.all_poses())
    assert op.REST in frames
    assert all(im.size == (56, 56) and im.mode == "RGBA" for im in frames.values())


def test_rest_pose_matches_the_shipped_icon(frames):
    """Switching the setting on must not make the icon jump: the redrawn rest pose has to
    sit where the PNG sits. Compared the way the window is clipped (alpha >= 110)."""
    from pathlib import Path
    from PIL import Image, ImageChops
    art = Image.open(Path(op.__file__).with_name("claude_overlay_2.png")).convert("RGBA")
    s, n = 56, 56 * 4
    sc = n * 0.96 / art.width
    a = art.resize((round(art.width * sc), round(art.height * sc)), Image.LANCZOS)
    c = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    c.alpha_composite(a, ((n - a.width) // 2, (n - a.height) // 2))
    still = c.resize((s, s), Image.LANCZOS)

    def solid(im):
        return im.split()[3].point(lambda v: 255 if v >= 110 else 0)

    diff = ImageChops.difference(solid(still), solid(frames[op.REST]))
    off = sum(1 for v in diff.tobytes() if v)
    total = sum(1 for v in solid(still).tobytes() if v)
    assert off <= total * 0.02, f"{off} of {total} outline pixels differ"


def test_eye_only_poses_share_an_outline(frames):
    """A blink or a glance must not need the window region rebuilt."""
    for pose in (op.SHUT, op.LOOK_L, op.LOOK_R):
        assert op.silhouette_key(pose) == op.silhouette_key(op.REST)
        assert frames[pose].split()[3].tobytes() == frames[op.REST].split()[3].tobytes()
        assert frames[pose].tobytes() != frames[op.REST].tobytes()      # but it IS a new frame


def test_poses_that_move_the_body_change_the_outline(frames):
    rest = frames[op.REST].split()[3].tobytes()
    for pose in (op.ARMS_UP, op.WORK[0][0], op.ARMS_UP._replace(hop=2), op._p(squash=1)):
        assert op.silhouette_key(pose) != op.silhouette_key(op.REST)
        assert frames[pose].split()[3].tobytes() != rest


@pytest.mark.parametrize("size", [56, 84, 112])
def test_no_pose_is_cut_off_by_the_orb_box(size):
    """The highest hop and the raised arms still fit inside the window at any DPI."""
    for pose, im in op.render_all(size, poses=[op.ARMS_UP._replace(hop=2), op.REST]).items():
        alpha = im.split()[3]
        top = alpha.crop((0, 0, size, 1)).getbbox()
        assert top is None, f"{pose} touches the top edge at {size}px"


# ── brain ─────────────────────────────────────────────────────────────────────────────
def _run(brain, now, steps):
    out = []
    for _ in range(steps):
        pose, ms = brain.step(now)
        out.append((pose, ms))
        now += ms / 1000
    return out, now


def test_idle_is_mostly_standing_still():
    """The whole point of the delay: between clips there is one long wait, not a frame loop."""
    b = op.PetBrain(random.Random(7))
    b.reset(0.0)
    steps, end = [], 0.0
    while end < op.SLEEP_AFTER - 10:                  # the waking stretch, before the nap
        more, end = _run(b, end, 1)
        steps += more
    still = sum(ms for pose, ms in steps if pose == op.REST)
    assert still / (end * 1000) > 0.75
    assert len(steps) / end < 2.0                     # under two timer fires a second, on average
    gaps = [ms for pose, ms in steps if pose == op.REST and ms >= 1000]
    assert gaps and min(gaps) >= op.IDLE_GAP[0] * 1000 and max(gaps) <= op.IDLE_GAP[1] * 1000


def test_every_delay_is_a_positive_integer():
    b = op.PetBrain(random.Random(1))
    b.reset(0.0)
    for state in ("idle", "busy", "held"):
        if state == "busy":
            b.set_busy(True, 0.0)
        if state == "held":
            b.hold(True, 0.0)
        for pose, ms in _run(b, 0.0, 60)[0]:
            assert isinstance(ms, int) and ms >= 1
            assert pose in op.all_poses()


def test_busy_loops_the_shuffle_until_it_ends():
    b = op.PetBrain(random.Random(3))
    b.reset(0.0)
    b.set_busy(True, 0.0)
    steps, now = _run(b, 0.0, 20)
    assert all(any(p.legs) for p, _ in steps)         # legs moving the whole time
    b.set_busy(False, now)
    assert b.step(now)[0] == op.REST                  # straight back to standing


def test_cheer_survives_the_busy_flag_dropping_after_it():
    """The reply lands (cheer), then _set_busy(False) arrives: the cheer must still play."""
    b = op.PetBrain(random.Random(3))
    b.reset(0.0, busy=True)
    b.step(0.0)
    b.celebrate(1.0)
    b.set_busy(False, 1.0)
    pose, _ = b.step(1.0)
    assert pose.arm_l and pose.arm_r and pose.hop


def test_poke_waves_and_is_ignored_mid_work_and_mid_wave():
    b = op.PetBrain(random.Random(3))
    b.reset(0.0)
    assert b.poke(0.0) is True
    assert b.step(0.0)[0].arm_r == 1
    assert b.poke(0.1) is False                       # already waving: no restart per <Enter>
    b.reset(0.0, busy=True)
    assert b.poke(0.0) is False


def test_naps_after_a_long_quiet_spell_and_wakes_on_a_poke():
    b = op.PetBrain(random.Random(3))
    b.reset(0.0)
    steps, now = _run(b, 0.0, 400)
    assert b.asleep and now >= op.SLEEP_AFTER
    pose, ms = b.step(now)
    assert pose.eyes == "closed" and ms >= 1000       # asleep = one timer every ~2 s
    assert b.poke(now) is True
    assert not b.asleep
    steps, _ = _run(b, now, len(op.WAKE) + 1)
    assert steps[-1][0] == op.REST


def test_being_carried_holds_the_arms_up_until_put_down():
    b = op.PetBrain(random.Random(3))
    b.reset(0.0)
    b.hold(True, 0.0)
    assert all(p.arm_l and p.arm_r for p, _ in _run(b, 0.0, 6)[0])
    b.hold(False, 3.0)
    assert b.step(3.0)[0] == op.REST


# ── the wider idle repertoire ───────────────────────────────────────────────────────────
def _idle_names(brain, n):
    names, now = [], 0.0
    for _ in range(n):
        brain.reset(now)                              # stay awake: only idle picks
        brain._refill(now)
        names.append(brain.last_idle)
        brain._q.clear()
    return names


def test_every_idle_clip_gets_played():
    names = _idle_names(op.PetBrain(random.Random(2)), 3000)
    assert set(names) == {name for name, _, _ in op.IDLE_CLIPS}


def test_showy_clips_never_play_twice_running():
    names = _idle_names(op.PetBrain(random.Random(4)), 3000)
    for a, b in zip(names, names[1:]):
        assert a != b or a in op.QUIET_CLIPS


def test_eye_only_clips_dominate_idle():
    """Most idle clips must be free ones (same outline as the rest pose), so a resting
    Clawd rarely asks for the window region to change."""
    free = sum(w for _, clip, w in op.IDLE_CLIPS if clip is op.WATCH or all(
        op.silhouette_key(p) == op.silhouette_key(op.REST) for p, _ in clip))
    assert free / sum(w for _, _, w in op.IDLE_CLIPS) > 0.6


def test_watch_looks_towards_the_pointer():
    for side, look in ((-1, -1), (1, 1)):
        b = op.PetBrain(random.Random(0))
        b.reset(0.0)
        b.pointer = side
        b.rng.choices = lambda pool, weights: [next(c for c in pool if c[0] == "watch")]
        poses = [p for p, _ in _run(b, 0.0, 3)[0]]
        assert poses[0] == op.REST and poses[1].look == look


def test_nap_starts_with_a_yawn():
    b = op.PetBrain(random.Random(3))
    b.reset(0.0)
    b._refill(op.SLEEP_AFTER + 1)
    first = [p for p, _ in list(b._q)[:len(op.YAWN)]]
    assert first == [p for p, _ in op.YAWN] and b.asleep


def test_ducked_poses_render_and_stay_inside_the_box(frames):
    duck = frames[op._p(hop=-6)]
    assert duck.size == (56, 56) and duck.split()[3].getbbox()[1] > frames[op.REST].split()[3].getbbox()[1]
