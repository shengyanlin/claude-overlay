# -*- coding: utf-8 -*-
"""The collapsed orb as a small desktop pet: Clawd blinks, glances around, waves, shuffles
while a reply is running and cheers when it lands.

Two halves, neither of which touches Tk:

  * the ARTIST redraws Clawd from the measured geometry of `claude_overlay_2.png`, one image
    per pose. Poses borrow from the handful Claude Code's own terminal mascot has (rest,
    look left, look right, arms up) plus a blink, a leg shuffle and a small hop.
  * the BRAIN decides which pose comes next and for how long. It is a plain state machine
    fed a clock, so it can be tested without a window or a timer.

What keeps this off the overlay's response time is the split of the work, and the overlay
relies on each part of it:

  * every frame is drawn ahead of time, on a background thread (`render_all`). A tick never
    draws anything; it swaps one ready image for another.
  * the brain hands back a DELAY with every pose, so between a blink and the next one there
    is one sleeping timer and nothing else. No frame loop runs while Clawd is standing still.
  * nothing here runs at all while the overlay is expanded. The host starts the timer on
    collapse and cancels it on expand.
"""
import math
import random
from collections import deque, namedtuple

from PIL import Image, ImageChops, ImageDraw

# ── geometry of the artwork, in its own pixels (measured off claude_overlay_2.png) ─────────
ART_W, ART_H = 600, 487
BODY = (86, 38, 515, 348)                 # x0, y0, x1, y1 (x1/y1 exclusive)
ARM_L = (38, 141, 86, 245)
ARM_R = (515, 141, 563, 245)
EYE_L = (134, 141, 181, 193)
EYE_R = (420, 141, 467, 193)
LEGS_X = ((86, 134), (181, 229), (372, 420), (467, 515))
LEG_Y = (348, 451)
KEYLINE = 18                              # the sticker's white outline
GAP_CLOSE = 8                             # extra reach that fills the slot between paired legs
UNIT = 48                                 # one "pixel" of the pixel art

BODY_RGB = (217, 119, 87)                 # the brand terracotta, #D97757
EYE_RGB = (0, 0, 0)
KEY_RGB = (255, 255, 255)

# eyes: "open" | "closed" | "up" | "wide";  look: -1 left, 0 ahead, +1 right;
# arm_l/arm_r: 0 down, 1 raised;  legs: four 0/1 flags, 1 = that leg is lifted;
# hop: steps off the ground (negative = ducked below the edge of the window);  squash: 0/1.
Pose = namedtuple("Pose", "eyes look arm_l arm_r legs hop squash")
Pose.__new__.__defaults__ = ("open", 0, 0, 0, (0, 0, 0, 0), 0, 0)

REST = Pose()
LOOK_SHIFT = 30                           # art px the pupils slide when glancing
LOOK_UP_SHIFT = 26                        # ... and when looking up
WIDE = 9                                  # art px a startled eye grows by, each side
ARM_RAISE = UNIT                          # art px a raised arm moves up
LEG_LIFT = 44                             # art px a lifted leg shortens by
HOP_STEP = 22                             # art px per hop step
SQUASH = 0.94                             # body height while breathing out


def silhouette_key(pose):
    """Poses that differ only in the eyes share one outline, so the host can skip the window
    region work (the only part of a tick that is not a plain image swap) between them."""
    return (pose.arm_l, pose.arm_r, pose.legs, pose.hop, pose.squash)


# ── artist ─────────────────────────────────────────────────────────────────────────────
def _shifted(mask, dx, dy, fill):
    out = Image.new("L", mask.size, fill)
    out.paste(mask, (dx, dy))
    return out


def _dilate(mask, r, steps=16):
    """Grow `mask` by a disc of radius r: the union of the mask moved to points round the
    circle. Enough for shapes at least r thick, which every part of Clawd is."""
    out = mask
    for k in range(steps):
        a = 2 * math.pi * k / steps
        out = ImageChops.lighter(out, _shifted(mask, round(r * math.cos(a)),
                                               round(r * math.sin(a)), 0))
    return out


def _erode(mask, r, steps=16):
    out = mask
    for k in range(steps):
        a = 2 * math.pi * k / steps
        out = ImageChops.darker(out, _shifted(mask, round(r * math.cos(a)),
                                              round(r * math.sin(a)), 255))
    return out


def _body_mask(pose):
    m = Image.new("L", (ART_W, ART_H), 0)
    d = ImageDraw.Draw(m)

    def box(x0, y0, x1, y1):
        if y1 > y0:
            d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=255)

    box(*BODY)
    for arm, up in ((ARM_L, pose.arm_l), (ARM_R, pose.arm_r)):
        dy = -ARM_RAISE * up
        box(arm[0], arm[1] + dy, arm[2], arm[3] + dy)
    for (x0, x1), lifted in zip(LEGS_X, pose.legs):
        box(x0, LEG_Y[0], x1, LEG_Y[1] - LEG_LIFT * lifted)
    return m


def _keyline_mask(body):
    """The sticker outline: the body grown by KEYLINE. Grown a little further first and then
    pulled back, so the narrow slot between each pair of legs fills in white the way it does
    in the artwork while the wide gap in the middle keeps its notch."""
    return _erode(_dilate(body, KEYLINE + GAP_CLOSE), GAP_CLOSE)


def render_art(pose, _keys=None):
    """One pose at the artwork's own resolution, as RGBA. `_keys` caches the outline per
    silhouette: a blink reuses the outline of the pose it interrupts."""
    body = _body_mask(pose)
    sk = silhouette_key(pose)[:3]          # hop and squash are applied later, at fit time
    key = _keys.get(sk) if _keys is not None else None
    if key is None:
        key = _keyline_mask(body)
        if _keys is not None:
            _keys[sk] = key
    art = Image.new("RGBA", (ART_W, ART_H), (0, 0, 0, 0))
    art.paste(KEY_RGB + (255,), (0, 0), key)
    art.paste(BODY_RGB + (255,), (0, 0), body)
    d = ImageDraw.Draw(art)
    dx = LOOK_SHIFT * pose.look
    for x0, y0, x1, y1 in (EYE_L, EYE_R):
        if pose.eyes == "closed":
            mid = (y0 + y1) // 2
            y0, y1 = mid - 6, mid + 8
        elif pose.eyes == "up":
            y0, y1 = y0 - LOOK_UP_SHIFT, y1 - LOOK_UP_SHIFT
        elif pose.eyes == "wide":
            x0, y0, x1, y1 = x0 - WIDE, y0 - WIDE, x1 + WIDE, y1 + WIDE
        d.rectangle([x0 + dx, y0, x1 + dx - 1, y1 - 1], fill=EYE_RGB + (255,))
    return art


def fit(art, pose, size, margin=0.04):
    """Scale a rendered pose into a size x size box exactly where the static orb artwork
    sits (same scale, same centring), then apply the hop and the squash. Both are done here,
    in whole screen pixels, so the sprite keeps hard pixel-art edges at any DPI."""
    scale = size * (1.0 - max(0.0, min(0.4, margin))) / ART_W
    nw, nh = max(1, round(ART_W * scale)), max(1, round(ART_H * scale))
    h2 = max(1, round(nh * SQUASH)) if pose.squash else nh
    small = art.resize((nw, h2), Image.LANCZOS)
    x, y = (size - nw) // 2, (size - nh) // 2
    y += nh - h2                                   # squash towards the feet
    y -= round(HOP_STEP * scale * pose.hop)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(small, (x, max(0, y)))
    return out


def render_all(size, margin=0.04, poses=None):
    """Every pose the brain can ask for, at one orb size: {pose: RGBA image}. Meant for a
    background thread: it is pure Pillow, and is the only place frames are ever drawn."""
    keys = {}
    return {p: fit(render_art(p, keys), p, size, margin) for p in (poses or all_poses())}


# ── clips: (pose, milliseconds) ────────────────────────────────────────────────────────
def _p(**kw):
    return REST._replace(**kw)


SHUT = _p(eyes="closed")
LOOK_L, LOOK_R = _p(look=-1), _p(look=1)
ARMS_UP = _p(arm_l=1, arm_r=1)
STEP_A, STEP_B = (1, 0, 1, 0), (0, 1, 0, 1)

BLINK = [(SHUT, 120)]
DOUBLE_BLINK = [(SHUT, 100), (REST, 140), (SHUT, 100)]
GLANCE = [(LOOK_L, 620), (REST, 220), (LOOK_R, 620)]
PEEK_L = [(LOOK_L, 900)]
PEEK_R = [(LOOK_R, 900)]
LOOK_UP = [(_p(eyes="up"), 950)]
STRETCH = [(_p(squash=1), 260), (ARMS_UP, 520), (REST, 120), (SHUT, 120)]
WAVE = [(_p(arm_r=1), 170), (REST, 150), (_p(arm_r=1), 170), (REST, 150), (_p(arm_r=1), 220)]
CHEER = [(ARMS_UP._replace(hop=1), 90), (ARMS_UP._replace(hop=2), 150),
         (ARMS_UP._replace(hop=1), 90), (ARMS_UP, 130)] * 2 + [(SHUT, 110)]
# More things a resting Clawd does. The motions are ideas seen in community Clawd pets
# (a dance, a crab-walk, peeking, a yawn before the nap, a startled wake); every frame is
# drawn here from the same geometry as the rest, and nothing is taken from their artwork.
NOD = [(_p(squash=1), 170), (REST, 170)] * 2
WIGGLE = [(_p(arm_l=1), 150), (_p(arm_r=1), 150)] * 2
TAP_L = [(_p(look=-1, legs=STEP_A), 190), (_p(look=-1, legs=STEP_B), 190)] * 3
TAP_R = [(_p(look=1, legs=STEP_A), 190), (_p(look=1, legs=STEP_B), 190)] * 3
BOUNCE = [(_p(hop=1), 80), (_p(hop=2), 130), (_p(hop=1), 80), (REST, 110)] * 2
DANCE = [(_p(arm_l=1, hop=1), 210), (_p(arm_r=1), 210)] * 4
JACKS = [(ARMS_UP._replace(legs=STEP_A, hop=1), 200), (REST, 200)] * 3
PEEKABOO = [(_p(hop=-3), 140), (_p(hop=-6), 650), (_p(hop=-3, look=-1), 420),
            (_p(hop=-3, look=1), 420), (_p(hop=1, eyes="wide"), 130), (REST, 100)]
YAWN = [(_p(eyes="closed", squash=1), 320), (ARMS_UP._replace(eyes="closed"), 760),
        (SHUT, 220)]
DOZE = [(SHUT, 500), (REST, 650), (SHUT, 900), (REST, 420)]       # nodding off, catching itself
WAKE = [(ARMS_UP._replace(hop=2, eyes="wide"), 170), (ARMS_UP._replace(hop=1, eyes="wide"), 110),
        (_p(eyes="wide"), 280), (SHUT, 110)]                      # startled awake
WATCH = "watch"                           # resolved when picked: a look towards the pointer
# Looped while the state lasts. Slow on purpose: a reply can run for minutes.
WORK = ([(_p(look=-1, legs=STEP_A), 300), (_p(look=-1, legs=STEP_B), 300)] * 2
        + [(_p(look=1, legs=STEP_A), 300), (_p(look=1, legs=STEP_B), 300)] * 2)
HELD = [(ARMS_UP._replace(legs=STEP_A), 380), (ARMS_UP._replace(legs=STEP_B), 380)]
SLEEP = [(_p(eyes="closed"), 1700), (_p(eyes="closed", squash=1), 1700)]

# (name, clip, weight). Eye-only clips carry most of the weight on purpose: they are free
# (no window-region change), and a pet that never stops dancing is tiring to sit next to.
IDLE_CLIPS = (("blink", BLINK, 30), ("double_blink", DOUBLE_BLINK, 7), ("glance", GLANCE, 8),
              ("peek_l", PEEK_L, 4), ("peek_r", PEEK_R, 4), ("watch", WATCH, 9),
              ("look_up", LOOK_UP, 5), ("stretch", STRETCH, 5), ("nod", NOD, 4),
              ("wiggle", WIGGLE, 4), ("tap_l", TAP_L, 2), ("tap_r", TAP_R, 2),
              ("bounce", BOUNCE, 4), ("dance", DANCE, 3), ("jacks", JACKS, 2),
              ("peekaboo", PEEKABOO, 3), ("yawn", YAWN, 2))
QUIET_CLIPS = ("blink", "double_blink")   # may repeat back to back; the showy ones may not
IDLE_GAP = (2.4, 6.5)                     # seconds Clawd stands still between idle clips
SLEEP_AFTER = 180.0                       # seconds without anything happening before a nap


def all_poses():
    seen = {REST: None}
    clips = [clip for _, clip, _ in IDLE_CLIPS if clip is not WATCH]
    for clip in clips + [WAVE, CHEER, DOZE, WAKE, WORK, HELD, SLEEP]:
        for pose, _ in clip:
            seen[pose] = None
    return list(seen)


# ── brain ──────────────────────────────────────────────────────────────────────────────
class PetBrain:
    """Which pose next, and for how long. `step(now)` is the whole interface for the timer;
    the event methods change what the next step returns, and the host re-arms its timer
    after calling one (the delay already handed out belongs to the old plan)."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self._q = deque()
        self._clip = None                 # name of what is queued, for set_busy(False)
        self.busy = False
        self.held = False
        self.asleep = False
        self.pointer = 0                  # -1 / 0 / +1: which side of the orb the pointer is on
        self.last_idle = None             # name of the idle clip played last (no encores)
        self._active = 0.0                # when something last happened (for the nap)

    # events ---------------------------------------------------------------------------
    def reset(self, now, busy=False):
        self._q.clear()
        self._clip = None
        self.busy, self.held, self.asleep = bool(busy), False, False
        self._active = now

    def _play(self, name, clip, now):
        self._q = deque(clip)
        self._clip = name
        self.asleep = False
        self._active = now

    def set_busy(self, busy, now):
        busy = bool(busy)
        if busy == self.busy:
            return
        self.busy = busy
        self.asleep = False
        self._active = now
        if busy or self._clip == "work":  # a cheer queued just before the flag drops survives
            self._q.clear()
            self._clip = None

    def celebrate(self, now):
        self._play("cheer", CHEER, now)

    def poke(self, now):
        """The pointer arrived. Says hello, or wakes up. Ignored mid-work and mid-carry."""
        if self.held or self.busy or self._clip in ("wave", "cheer", "wake"):
            return False
        if self.asleep:
            self._play("wake", WAKE, now)
        else:
            self._play("wave", WAVE, now)
        return True

    def hold(self, on, now):
        on = bool(on)
        if on == self.held:
            return
        self.held = on
        self.asleep = False
        self._active = now
        self._q.clear()
        self._clip = None

    # the timer --------------------------------------------------------------------------
    def step(self, now):
        """(pose, delay_ms): show this pose, call again after the delay."""
        if not self._q:
            self._refill(now)
        pose, ms = self._q.popleft()
        if not self._q:
            self._clip = None if self._clip in ("wave", "cheer", "wake", "idle") else self._clip
        return pose, int(ms)

    def _refill(self, now):
        if self.held:
            self._q.extend(HELD)
            self._clip = "held"
        elif self.busy:
            self._q.extend(WORK)
            self._clip = "work"
        elif self.asleep or now - self._active >= SLEEP_AFTER:
            if not self.asleep:           # going down: a yawn, a couple of nods, then out
                self._q.extend(YAWN + DOZE)
            self.asleep = True
            self._q.extend(SLEEP)
            self._clip = "sleep"
        else:
            pool = [c for c in IDLE_CLIPS if c[0] in QUIET_CLIPS or c[0] != self.last_idle]
            name, clip, _ = self.rng.choices(pool, [w for _, _, w in pool])[0]
            self.last_idle = name
            if clip is WATCH:             # towards the pointer; straight ahead → look about
                clip = {-1: [(LOOK_L, 1300)], 1: [(LOOK_R, 1300)]}.get(self.pointer, GLANCE)
            self._q.append((REST, self.rng.uniform(*IDLE_GAP) * 1000))
            self._q.extend(clip)
            self._q.append((REST, 1))    # land on the rest pose before the next gap is drawn
            self._clip = "idle"
