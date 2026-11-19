# -*- coding: utf-8 -*-
"""Pure screen geometry shared by the platform layers (win32utils on Windows, macutils on
macOS): clipping a capture rectangle to the desktop, and deciding where to pull a window
that has been stranded off every monitor. No OS calls and no project imports, so both
platform modules import it and it is unit-testable anywhere."""


def clamp_bbox(rect, vbox):
    """Intersect a window rect (l,t,r,b) with the virtual-desktop box (x,y,w,h) so a
    half-dragged-offscreen window grabs only its visible part; None when the visible
    overlap is degenerate (<8px a side — nothing worth sending). Pure math, no Win32."""
    if not rect:
        return None
    l, t, r, b = rect
    if vbox:
        x, y, w, h = vbox
        l, t = max(l, x), max(t, y)
        r, b = min(r, x + w), min(b, y + h)
    if r - l < 8 or b - t < 8:
        return None
    return (l, t, r, b)


def _mon_rect(m):
    """The usable rectangle of a monitor dict: prefer the work area, fall back to the full rect."""
    return m.get("work") or m.get("rect")


def _visible_extent(win, rect):
    """Visible (w, h) of the overlap of window win=(x,y,w,h) with a monitor rect=(l,t,r,b)."""
    x, y, w, h = win
    iw = min(x + w, rect[2]) - max(x, rect[0])
    ih = min(y + h, rect[3]) - max(y, rect[1])
    return max(0, iw), max(0, ih)


def _clamp_into(x, y, w, h, rect):
    """Slide a (w, h) window so it sits fully inside monitor rect=(l,t,r,b); top-left wins if the
    window is larger than the monitor."""
    l, t, r, b = rect
    if x + w > r:
        x = r - w
    if y + h > b:
        y = b - h
    return max(x, l), max(y, t)


def compute_onscreen_move(win, monitors, min_vis_w=48, min_vis_h=32):
    """Decide whether a window has drifted off EVERY connected monitor and, if so, where to
    move its top-left back to (keeping its size).

    win       -- (x, y, w, h) window rectangle, in virtual-desktop pixels.
    monitors  -- an enumerate_monitors()-style list (each entry's 'work' rect is used when
                 present, else 'rect'; a falsy rect is ignored).
    Returns (nx, ny) when the window must move, or None when it's still reachable — i.e. at
    least min_vis_w x min_vis_h of it shows on some monitor. The threshold means a window the
    user deliberately parked slightly off an edge is left alone; only a fully-stranded window
    (e.g. it was on a monitor that got unplugged) is pulled back, onto the monitor whose centre
    is nearest the window's centre.

    Pure geometry — no Win32 calls — so it's unit-testable off Windows."""
    x, y, w, h = win
    if w <= 0 or h <= 0:
        return None
    rects = [r for r in (_mon_rect(m) for m in (monitors or [])) if r]
    if not rects:
        return None
    need_w, need_h = min(min_vis_w, w), min(min_vis_h, h)
    for rect in rects:
        vw, vh = _visible_extent(win, rect)
        if vw >= need_w and vh >= need_h:
            return None                       # still reachable on this monitor → leave it
    cx, cy = x + w / 2.0, y + h / 2.0         # off every monitor → clamp onto the nearest one
    best, best_d = None, None
    for rect in rects:
        d = ((rect[0] + rect[2]) / 2.0 - cx) ** 2 + ((rect[1] + rect[3]) / 2.0 - cy) ** 2
        if best_d is None or d < best_d:
            best, best_d = rect, d
    return _clamp_into(x, y, w, h, best)
