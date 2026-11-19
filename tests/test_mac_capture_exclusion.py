"""macOS: keeping the overlay out of screenshots and screen shares.

Windows' WDA_EXCLUDEFROMCAPTURE is a guarantee; NSWindowSharingNone is a request Apple says
not to rely on. So the port asks for it but only TRUSTS it after seeing it work on this Mac
(macutils.verify_capture_exclusion), and otherwise takes the overlay out of its own
screenshots by fading it — and says so instead of promising privacy it can't deliver.

No test here takes a real screenshot: every grab is a fake, so the verdicts are the test's.
"""
import sys

import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only capture exclusion")

if sys.platform == "darwin":
    import claude_overlay as co
    import macutils
    from conftest import chat_text


def _marker(magenta=(255, 0, 255), green=(0, 255, 0), size=(32, 32)):
    im = Image.new("RGB", size, magenta)
    im.paste(green, (size[0] // 2, 0, size[0], size[1]))
    return im


@pytest.fixture
def fresh(monkeypatch):
    """No verdict yet, and Screen Recording 'granted' — whatever this machine says."""
    monkeypatch.setattr(macutils, "_capture_exclusion", None)
    monkeypatch.setattr(macutils, "_exclusion_tries", 0)
    monkeypatch.setattr(macutils, "screen_capture_allowed", lambda: True)


# ── recognising the probe window in a grab ──

def test_the_marker_is_recognised_in_srgb_and_in_a_wide_gamut_capture():
    assert macutils._marker_seen(_marker())
    # the same two colours as a Display P3 capture reports them
    assert macutils._marker_seen(_marker((234, 51, 247), (117, 251, 76)))
    assert macutils._marker_seen(_marker(size=(16, 16)))         # a 1x display


@pytest.mark.parametrize("img", [
    Image.new("RGB", (32, 32), (128, 128, 128)),                 # a grey wallpaper
    Image.new("RGB", (32, 32), (255, 0, 255)),                   # magenta all over: not the pair
    Image.new("RGB", (32, 32), (0, 255, 0)),
    Image.new("RGB", (4, 2), (255, 0, 255)),                     # too small to judge
    None,
])
def test_anything_else_is_not_the_marker(img):
    assert macutils._marker_seen(img) is False


def test_the_marker_the_wrong_way_round_is_not_the_marker():
    assert macutils._marker_seen(_marker((0, 255, 0), (255, 0, 255))) is False


# ── the verdict ──

def test_no_verdict_without_screen_recording_permission(fresh, monkeypatch):
    """Without the permission a capture is the wallpaper: the marker is never in it, which
    would read as 'excluding works'. The question isn't asked, and isn't settled."""
    monkeypatch.setattr(macutils, "screen_capture_allowed", lambda: False)
    monkeypatch.setattr(macutils, "_probe_exclusion", lambda grab: pytest.fail("probed"))
    assert macutils.verify_capture_exclusion(None) is False
    assert not macutils.capture_exclusion_tested()
    assert not macutils.capture_exclusion_verified()


def test_a_verdict_is_reached_once_and_remembered(fresh, monkeypatch):
    calls = []
    monkeypatch.setattr(macutils, "_probe_exclusion", lambda grab: calls.append(1) or True)
    assert macutils.verify_capture_exclusion(None) is True
    assert macutils.verify_capture_exclusion(None) is True
    assert calls == [1]
    assert macutils.capture_exclusion_tested() and macutils.capture_exclusion_verified()


def test_a_mac_that_ignores_the_request_is_remembered_as_such(fresh, monkeypatch):
    monkeypatch.setattr(macutils, "_probe_exclusion", lambda grab: False)
    assert macutils.verify_capture_exclusion(None) is False
    assert macutils.capture_exclusion_tested()
    assert not macutils.capture_exclusion_verified()


def test_an_inconclusive_round_is_retried_then_counts_as_no(fresh, monkeypatch):
    """A round whose control grab failed proves nothing, so it is asked again — but not
    forever: three inconclusive rounds settle on 'not verified', the safe answer."""
    rounds = []

    def probe(grab):
        rounds.append(1)
        if len(rounds) == 2:
            raise RuntimeError("the window server said no")     # an error is inconclusive too
        return None

    monkeypatch.setattr(macutils, "_probe_exclusion", probe)
    assert macutils.verify_capture_exclusion(None) is False
    assert not macutils.capture_exclusion_tested()
    assert macutils.verify_capture_exclusion(None) is False
    assert not macutils.capture_exclusion_tested()
    assert macutils.verify_capture_exclusion(None) is False
    assert macutils.capture_exclusion_tested() and not macutils.capture_exclusion_verified()
    assert macutils.verify_capture_exclusion(None) is False
    assert len(rounds) == 3


def test_a_later_round_can_still_succeed(fresh, monkeypatch):
    answers = iter([None, True])
    monkeypatch.setattr(macutils, "_probe_exclusion", lambda grab: next(answers))
    assert macutils.verify_capture_exclusion(None) is False
    assert macutils.verify_capture_exclusion(None) is True
    assert macutils.capture_exclusion_verified()


# ── the probe itself: a real window, fake grabs ──

def _probe_with(overlay, pictures):
    """Run one probe round whose grabs return `pictures` in order (the last one repeats).
    Returns (verdict, the boxes it asked for)."""
    boxes, it = [], iter(pictures)
    last = [None]

    def grab(bbox):
        boxes.append(bbox)
        last[0] = next(it, last[0])
        return last[0]

    verdict = macutils._probe_exclusion(grab)
    return verdict, boxes


def test_probe_says_yes_when_the_marker_disappears_once_marked_hidden(overlay):
    blank = Image.new("RGB", (32, 32), (40, 40, 40))
    verdict, boxes = _probe_with(overlay, [_marker(), blank])
    assert verdict is True
    l, t, r, b = boxes[0]
    assert (r - l, b - t) == (macutils._PROBE_SIZE, macutils._PROBE_SIZE)
    assert len(set(boxes)) == 1                         # both grabs are of the same rectangle


def test_probe_says_no_when_the_marker_is_still_in_the_picture(overlay):
    verdict, _ = _probe_with(overlay, [_marker(), _marker()])
    assert verdict is False


def test_probe_is_inconclusive_when_even_the_control_grab_misses_the_marker(overlay):
    blank = Image.new("RGB", (32, 32), (40, 40, 40))
    verdict, boxes = _probe_with(overlay, [blank])
    assert verdict is None
    assert len(boxes) == 4                              # it waited a few frames first


def test_probe_never_touches_the_overlay_window(overlay):
    w = macutils._ns_window()
    sharing, alpha = w.sharingType(), w.alphaValue()
    _probe_with(overlay, [_marker(), Image.new("RGB", (32, 32), (40, 40, 40))])
    assert (w.sharingType(), w.alphaValue()) == (sharing, alpha)


# ── fading the overlay out ──

class _FakeWindow:
    def __init__(self, alpha=1.0):
        self.alpha = alpha
        self.set_to = []

    def alphaValue(self):
        return self.alpha

    def setAlphaValue_(self, a):
        self.alpha = a
        self.set_to.append(a)


def test_fade_out_waits_for_the_window_server_and_hands_back_the_undo(monkeypatch):
    win = _FakeWindow(alpha=0.92)
    seen = iter([0.92, 0.5, 0.0])
    monkeypatch.setattr(macutils, "_ns_window", lambda: win)
    monkeypatch.setattr(macutils, "_server_alpha", lambda: next(seen))
    restore = macutils.fade_out()
    assert callable(restore) and win.alpha == 0.0
    restore()
    assert win.alpha == 0.92                            # WINDOW_ALPHA, not a hard-coded 1.0


def test_an_unconfirmed_fade_is_undone_and_reported(monkeypatch):
    """If the window server never reports the window invisible, a grab taken now could have
    the overlay in it: the caller must know, and the window must not be left transparent."""
    win = _FakeWindow(alpha=1.0)
    monkeypatch.setattr(macutils, "_ns_window", lambda: win)
    monkeypatch.setattr(macutils, "_server_alpha", lambda: 1.0)
    assert macutils.fade_out(timeout=0.05) is None
    assert win.alpha == 1.0


def test_fade_out_without_a_window_is_a_no(monkeypatch):
    monkeypatch.setattr(macutils, "_ns_window", lambda: None)
    assert macutils.fade_out() is None


# ── the overlay: what capture() does about it ──

@pytest.fixture
def ov(overlay, monkeypatch):
    """The overlay with the verdict under the test's control and no real probe or grab."""
    state = {"verified": False, "tested": False, "probes": 0}

    def verify(grab):
        state["probes"] += 1
        state["tested"] = True
        return state["verified"]

    monkeypatch.setattr(co, "verify_capture_exclusion", verify)
    monkeypatch.setattr(co, "capture_exclusion_verified", lambda: state["tested"] and state["verified"])
    monkeypatch.setattr(co, "capture_exclusion_tested", lambda: state["tested"])
    overlay.__dict__.pop("_share_warned", None)
    overlay.state = state
    yield overlay
    overlay.__dict__.pop("_share_warned", None)
    overlay.__dict__.pop("state", None)
    overlay.share_visible = co.SHOW_IN_SCREEN_SHARE_DEFAULT
    monkeypatch.undo()
    overlay._apply_share_visibility()


def test_exclusion_is_not_claimed_before_it_has_been_seen_working(ov):
    ov.share_visible = False
    ov._apply_share_visibility()
    assert macutils._ns_window().sharingType() == co.AppKit.NSWindowSharingNone   # asked for…
    assert ov._capture_excluded is False                                          # …not trusted


def test_private_and_verified_needs_no_hiding_at_all(ov):
    ov.state["verified"] = True
    ov.share_visible = False
    ov._apply_share_visibility()
    assert ov._mac_hide_for_capture() is None
    assert ov._capture_excluded is True                 # later captures skip the dance


def test_shareable_and_verified_is_marked_hidden_only_for_the_grab(ov):
    ov.state["verified"] = True
    ov.share_visible = True
    ov._apply_share_visibility()
    w = macutils._ns_window()
    undo = ov._mac_hide_for_capture()
    assert w.sharingType() == co.AppKit.NSWindowSharingNone
    assert w.alphaValue() > 0.5                         # no blink
    undo()
    assert w.sharingType() == co.AppKit.NSWindowSharingReadOnly
    assert ov._capture_excluded is False


def test_a_request_that_fails_at_grab_time_falls_back_to_fading(ov, monkeypatch):
    """Proven to work on this Mac, but THIS call to mark the window hidden failed: the grab
    must not go ahead with the overlay in the picture."""
    faded = []
    monkeypatch.setattr(co, "fade_out", lambda: faded.append("out") or (lambda: faded.append("in")))
    ov.state["verified"] = True
    ov.share_visible = True
    ov._apply_share_visibility()
    monkeypatch.setattr(co._user32, "SetWindowDisplayAffinity", lambda hwnd, affinity: 0)
    undo = ov._mac_hide_for_capture()
    assert faded == ["out"]
    undo()
    assert faded == ["out", "in"]
    assert "may be visible in a screen share" not in chat_text(ov)     # the Mac is fine


def test_an_unverified_mac_fades_the_overlay_instead(ov, monkeypatch):
    faded = []
    monkeypatch.setattr(co, "fade_out", lambda: faded.append("out") or (lambda: faded.append("in")))
    ov.share_visible = True
    ov._apply_share_visibility()
    undo = ov._mac_hide_for_capture()
    assert faded == ["out"]
    undo()
    assert faded == ["out", "in"]


def test_a_fade_that_cannot_be_confirmed_falls_back_to_unmapping(ov, monkeypatch):
    monkeypatch.setattr(co, "fade_out", lambda: None)
    ov.share_visible = True
    ov._apply_share_visibility()
    undo = ov._mac_hide_for_capture()
    assert not ov.root.winfo_viewable()
    undo()
    ov.root.update()
    assert ov.root.winfo_viewable()


def test_a_mac_that_ignores_the_request_is_said_out_loud_once(ov, monkeypatch):
    monkeypatch.setattr(co, "fade_out", lambda: (lambda: None))
    ov.share_visible = False
    ov._apply_share_visibility()
    ov._mac_hide_for_capture()()
    assert "may be visible in a screen share" in chat_text(ov)
    ov._mac_hide_for_capture()()
    assert chat_text(ov).count("may be visible in a screen share") == 1


def test_no_warning_while_shareable_or_before_any_verdict(ov, monkeypatch):
    monkeypatch.setattr(co, "fade_out", lambda: (lambda: None))
    ov.share_visible = True
    ov._apply_share_visibility()
    ov._mac_hide_for_capture()()                        # tested, failed — but Shareable is on
    assert "may be visible in a screen share" not in chat_text(ov)
    ov.state["tested"] = False
    ov.share_visible = False
    ov._mac_warn_exclusion()                            # private, but nothing known yet
    assert "may be visible in a screen share" not in chat_text(ov)


def test_capture_undoes_the_hiding_even_when_the_grab_fails(ov, monkeypatch):
    faded = []
    monkeypatch.setattr(co, "fade_out", lambda: faded.append("out") or (lambda: faded.append("in")))
    monkeypatch.setattr(ov, "_grab_shots_scoped",
                        lambda mons: (_ for _ in ()).throw(RuntimeError("no display")))
    ov.share_visible = True
    ov._apply_share_visibility()
    with pytest.raises(RuntimeError):
        ov.capture(announce=False, quiet=True)
    assert faded == ["out", "in"]


def test_type_ahead_capture_waits_for_a_proven_exclusion(ov, monkeypatch):
    """Precapture grabs on a background thread and never hides the window — fine on Windows,
    where exclusion is a guarantee; on a Mac it would put the overlay in the frame."""
    started = []
    monkeypatch.setattr(co, "PRECAPTURE_ON_TYPING", True)
    monkeypatch.setattr(co.threading, "Thread",
                        lambda *a, **k: started.append(1) or pytest.fail("precapture ran"))
    ov.auto_shot, ov.busy = True, False
    ov._precaptured, ov._capture_busy = None, False
    ov._capture_excluded = False
    ov._do_precapture()
    assert started == [] and ov._capture_busy is False


def test_a_type_ahead_frame_grabbed_before_shareable_changed_is_thrown_away(ov, monkeypatch):
    """The background grab started while the overlay was excluded; Shareable was switched on
    before it came back. Its frame may have the overlay in it, so it is neither cached nor,
    if it was cached already, kept for the next send."""
    shot = [{"path": "/tmp/x.png", "primary": True, "index": 1}]
    monkeypatch.setattr(ov, "_grab_shots_scoped", lambda mons: (shot, None))
    ov.state["verified"] = ov.state["tested"] = True
    ov.share_visible = False
    ov._apply_share_visibility()
    assert ov._capture_excluded is True

    ov._precaptured = (shot, 0.0)                       # an older cached frame…
    gen = ov._precapture_gen
    ov.toggle_screen_share()                            # …is void once Shareable flips
    assert ov._precaptured is None and ov._precapture_gen != gen

    ov._precapture_bg(gen)                              # the in-flight grab lands late
    kind, payload = ov.ui_q.get_nowait()
    ov._handle(kind, payload)
    assert ov._precaptured is None and ov._capture_busy is False

    ov._precapture_bg(ov._precapture_gen)               # one started under the current state
    kind, payload = ov.ui_q.get_nowait()
    ov._handle(kind, payload)
    assert ov._precaptured and ov._precaptured[0] == shot
    ov._precaptured = None


def test_turning_shareable_off_does_not_promise_privacy(ov):
    ov.share_visible = True
    ov.toggle_screen_share()
    txt = chat_text(ov)
    assert "not a guarantee" in txt
    assert "only you can see it" not in txt


# ── one capture at a time ──

def test_a_capture_started_inside_another_does_not_undo_its_hiding(ov, monkeypatch):
    """On Tk 8.6 committing the 'hidden' mark turns the event loop, so a second Enter (or a
    Snap) waiting in the queue runs INSIDE the first capture. Finishing first, it used to
    put the window back to shareable — and the outer capture then grabbed the overlay."""
    w = macutils._ns_window()
    seen = {"nested": None, "sharing_at_grab": []}
    shot = [{"path": "/tmp/x.png", "primary": True, "index": 1}]

    def commit():                                       # what root.update() could dispatch
        if seen["nested"] is None:
            seen["nested"] = ov.capture(announce=False)

    def grab(mons):
        seen["sharing_at_grab"].append(w.sharingType())
        return shot, None

    monkeypatch.setattr(co, "commit_window_changes", commit)
    monkeypatch.setattr(ov, "_grab_shots_scoped", grab)
    ov.state["verified"] = ov.state["tested"] = True
    ov.share_visible = True
    ov._apply_share_visibility()
    assert ov.capture(announce=False) == shot
    assert seen["nested"] == []                         # the inner one took nothing…
    assert seen["sharing_at_grab"] == [co.AppKit.NSWindowSharingNone]   # …and undid nothing
    assert w.sharingType() == co.AppKit.NSWindowSharingReadOnly         # put back once, after
    assert ov._capturing is False


def test_a_second_enter_during_the_screenshot_sends_nothing_twice(ov, monkeypatch):
    collected = []

    def commit():
        collected.append(ov._collect_send())            # the queued second Enter

    monkeypatch.setattr(co, "commit_window_changes", commit)
    monkeypatch.setattr(ov, "_grab_shots_scoped",
                        lambda mons: ([{"path": "/tmp/x.png", "primary": True, "index": 1}], None))
    ov.state["verified"] = ov.state["tested"] = True
    ov.share_visible = True
    ov._apply_share_visibility()
    ov.entry.insert("1.0", "what is on my screen?")
    ov.auto_shot, ov._precaptured = True, None
    item = ov._collect_send()
    assert collected == [None]                          # nothing consumed by the inner one
    assert item is not None                             # the first send has text and shot


def test_snap_and_the_shareable_toggle_wait_for_the_capture_in_progress(ov, monkeypatch):
    later = []
    monkeypatch.setattr(ov.root, "after", lambda ms, fn=None, *a: later.append((ms, fn)))
    monkeypatch.setattr(ov, "capture", lambda **k: pytest.fail("a nested Snap captured"))
    before = ov.share_visible
    ov._capturing = True
    try:
        ov.snap_now()
        ov.toggle_screen_share()
    finally:
        ov._capturing = False
    assert ov.share_visible is before                   # not flipped under the capture…
    assert [fn for _ms, fn in later] == [ov.toggle_screen_share]        # …but not lost either


def test_the_capture_flag_is_cleared_when_the_grab_blows_up(ov, monkeypatch):
    monkeypatch.setattr(co, "fade_out", lambda: (lambda: None))
    monkeypatch.setattr(ov, "_grab_shots_scoped",
                        lambda mons: (_ for _ in ()).throw(RuntimeError("no display")))
    ov.share_visible = True
    ov._apply_share_visibility()
    with pytest.raises(RuntimeError):
        ov.capture(announce=False, quiet=True)
    assert ov._capturing is False


# ── Cocoa off the main thread ──

def test_helpers_used_from_background_threads_bring_their_own_autorelease_pool(tmp_path):
    """The type-ahead grab and the clipboard read run on plain Python threads, which have no
    autorelease pool: every temporary Cocoa object leaked. With OBJC_DEBUG_MISSING_POOLS the
    runtime reports each one, so a child process says whether any slipped through."""
    import os
    import subprocess
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys, threading\n"
        f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})\n"
        "import AppKit, macutils\n"
        "pb = AppKit.NSPasteboard.pasteboardWithUniqueName()\n"          # never the real clipboard
        "pb.clearContents()\n"
        "pb.writeObjects_([AppKit.NSURL.fileURLWithPath_(sys.argv[1])])\n"
        "macutils._pasteboard = lambda: pb\n"
        "out = {}\n"
        "def work():\n"
        "    out['paths'] = macutils.grab_clipboard()\n"
        "    out['has'] = macutils._clipboard_has(macutils.CF_HDROP)\n"
        "    out['mons'] = macutils.enumerate_monitors()\n"
        "    macutils.virtual_screen_metrics(); macutils.screen_capture_allowed()\n"
        "    hwnd = macutils.foreground_capture_window()\n"
        "    macutils.window_title(hwnd or 1); macutils.window_bbox(hwnd or 1)\n"
        "    macutils.window_is_own(hwnd or 1); macutils.window_capturable(hwnd or 1)\n"
        "t = threading.Thread(target=work); t.start(); t.join()\n"
        "pb.releaseGlobally()\n"
        "print('RESULT', out['paths'], out['has'], bool(out['mons']))\n", encoding="utf-8")
    target = tmp_path / "a picture.png"
    target.write_bytes(b"")
    env = dict(os.environ, OBJC_DEBUG_MISSING_POOLS="YES", CLAUDE_OVERLAY_TELEMETRY="0")
    r = subprocess.run([sys.executable, str(script), str(target)], capture_output=True,
                       text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr[-2000:]
    assert f"RESULT ['{target}'] True" in r.stdout.replace("/private", "") or \
        f"RESULT ['{os.path.realpath(target)}'] True" in r.stdout, r.stdout
    assert "MISSING POOLS" not in r.stderr, r.stderr[-2000:]
