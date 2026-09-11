"""Tests for sessions.py — reading the CLI's transcript store.

Everything runs against synthetic transcripts in tmp_path rather than the developer's real
~/.claude, so the suite is hermetic and cannot delete somebody's history when the delete
tests run.

The record shapes here are copied from real transcripts, including the parts that made the
first implementation wrong: user records that are tool results, slash-command wrappers and
isMeta notes. See test_machinery_is_not_counted_as_messages.
"""
import base64
import io
import json

import pytest

import sessions


# ── helpers ──────────────────────────────────────────────────────────────────

def _user(text=None, blocks=None, meta=False):
    rec = {"type": "user", "userType": "external",
           "message": {"role": "user", "content": blocks if blocks is not None else text}}
    if meta:
        rec["isMeta"] = True
    return rec


def _jpeg_b64(size=(40, 30), colour=(200, 90, 60)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _write(project, sid, records):
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                    encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path):
    """A Store over tmp_path, plus the project dir to write transcripts into."""
    cwd = r"C:\Users\someone"
    root = tmp_path / "projects"
    project = root / sessions.project_slug(cwd)
    project.mkdir(parents=True)

    def make(**kw):
        return sessions.Store(cwd, root=root, cache_dir=tmp_path / "cache", **kw)

    make.project = project
    make.root = root
    make.cwd = cwd
    return make


# ── project_slug ──────────────────────────────────────────────────────────────

def test_project_slug_matches_the_cli_layout():
    """Every non-alphanumeric character becomes '-', so a drive-letter path gains a double
    dash where ':' and '\\' meet. This is checked against a full Windows project path."""
    assert sessions.project_slug(r"C:\Users\user") == "C--Users-user"
    assert (sessions.project_slug(r"C:\Users\user\Desktop\projects\claude-overlay")
            == "C--Users-user-Desktop-projects-claude-overlay")


def test_project_slug_preserves_case():
    """'c:\\...' and 'C:\\...' are genuinely different folders in the store -- both exist in
    a real one. Normalising case here would read the wrong directory."""
    assert sessions.project_slug(r"c:\Users\user") == "c--Users-user"
    assert sessions.project_slug(r"c:\Users\user") != sessions.project_slug(r"C:\Users\user")


# ── titles ────────────────────────────────────────────────────────────────────

def test_ai_title_wins(store):
    _write(store.project, "s1", [
        _user("this is what I typed first"),
        {"type": "ai-title", "aiTitle": "Taiwan Lamination product catalog", "sessionId": "s1"},
    ])
    s = store().list()[0]
    assert s.title == "Taiwan Lamination product catalog"
    assert s.subtitle == "this is what I typed first"


def test_window_name_beats_the_message(store):
    """What you were LOOKING AT locates a conversation faster than its opening sentence."""
    note = ('[Attached: a live screenshot of my ACTIVE WINDOW only \u2014 \u201cJason \u6797\u8056\u664f \u2014 LINE\u201d '
            '\u2014 not the full screen; other windows and monitors are not visible to you.]\n\n'
            'how should I reply to my uncle')
    _write(store.project, "s1", [_user(blocks=[{"type": "text", "text": note}])])
    s = store().list()[0]
    assert s.title == "Jason \u6797\u8056\u664f \u2014 LINE"
    assert s.subtitle == "how should I reply to my uncle"


def test_falls_back_to_the_first_message(store):
    _write(store.project, "s1", [_user("just a plain question about pricing")])
    s = store().list()[0]
    assert s.title == "just a plain question about pricing"


def test_screenshot_note_is_stripped_from_the_title(store):
    """Every overlay turn is prefixed with a bracketed note. Left in, every session would be
    titled '[Attached: a live screenshot of my screen...'."""
    _write(store.project, "s1", [_user(blocks=[
        {"type": "text",
         "text": "[Attached: a live screenshot of my screen \u2014 monitor 1 (primary).]\n\nfix my bug"},
    ])])
    assert store().list()[0].title == "fix my bug"


def test_long_titles_are_truncated_with_an_ellipsis(store):
    _write(store.project, "s1", [_user("x" * 200)])
    t = store().list()[0].title
    assert len(t) <= 48 and t.endswith("\u2026")


def test_session_with_nothing_typed_says_so(store):
    _write(store.project, "s1", [_user("<command-name>/model</command-name>")])
    s = store().list()[0]
    assert s.title == "(no messages)"
    assert s.messages == 0


# ── machinery filtering ───────────────────────────────────────────────────────

def test_machinery_is_not_counted_as_messages(store):
    """The bug this filter exists for: a real 11-message overlay session reported 128,
    because tool results, slash commands and isMeta notes all wear the 'user' record type.
    The mix below is the shape of a real transcript, scaled down.
    """
    records = [_user("the one thing I actually asked")]
    records += [_user(blocks=[{"type": "tool_result", "tool_use_id": f"t{i}", "content": "..."}])
                for i in range(9)]
    records += [_user("<command-name>/model</command-name>\n<command-args>opus</command-args>")]
    records += [_user("<local-command-caveat>Caveat: generated while running local commands",
                      meta=True)]
    records += [_user(blocks=[{"type": "text", "text": "[Request interrupted by user]"}])]
    records += [_user("a second real question")]
    _write(store.project, "s1", records)

    s = store().list()[0]
    assert s.messages == 2, "only the two typed messages should count"
    assert s.title == "the one thing I actually asked"


def test_meta_notes_are_not_messages(store):
    """isMeta records carry ordinary-looking prose -- 'Continue from where you left off.' is
    a real one -- so the machinery regex cannot catch them. Only the isMeta flag can, and
    without this case a dropped flag check goes unnoticed."""
    _write(store.project, "s1", [
        _user("the thing I asked"),
        _user("Continue from where you left off.", meta=True),
    ])
    s = store().list()[0]
    assert s.messages == 1, "an isMeta note was counted as a message"
    assert s.title == "the thing I asked"


def test_tool_heavy_session_still_reports_one_message(store):
    """A big transcript is not a long conversation: size comes from tool output, and a real
    1.3 MB session had exactly one thing typed in it."""
    records = [_user("do the whole thing for me")]
    records += [_user(blocks=[{"type": "tool_result", "tool_use_id": "t", "content": "x" * 5000}])
                for _ in range(17)]
    _write(store.project, "s1", records)
    assert store().list()[0].messages == 1


# ── counting / ordering / robustness ──────────────────────────────────────────

def test_sessions_are_newest_first(store):
    import os
    a = _write(store.project, "old", [_user("first")])
    b = _write(store.project, "new", [_user("second")])
    os.utime(a, (1_600_000_000, 1_600_000_000))
    os.utime(b, (1_700_000_000, 1_700_000_000))
    assert [s.id for s in store().list()] == ["new", "old"]


def test_torn_last_line_is_tolerated(store):
    """The CLI may be mid-write. A half-written line must not lose the whole session."""
    path = _write(store.project, "s1", [_user("a real message")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"user","message":{"content":"trunc')
    s = store().list()[0]
    assert s.messages == 1 and s.title == "a real message"


def test_missing_project_dir_lists_nothing(tmp_path):
    st = sessions.Store(r"C:\nope", root=tmp_path / "projects", cache_dir=tmp_path / "cache")
    assert st.list() == []


# ── thumbnails ────────────────────────────────────────────────────────────────

def test_thumbnail_is_written_from_the_first_screenshot(store):
    _write(store.project, "s1", [_user(blocks=[
        {"type": "text", "text": "look at this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": _jpeg_b64(size=(640, 480))}},
    ])])
    s = store().list()[0]
    assert s.thumb, "no thumbnail produced"
    from PIL import Image
    with Image.open(s.thumb) as im:
        assert max(im.size) <= sessions.THUMB_WIDTH, f"thumbnail not downscaled: {im.size}"


def test_unreadable_image_does_not_sink_the_session(store):
    """A session whose image will not decode still lists, just without a thumbnail --
    text-only and pasted-image sessions exist and must not raise."""
    _write(store.project, "s1", [_user(blocks=[
        {"type": "text", "text": "broken picture"},
        {"type": "image", "source": {"type": "base64", "data": "not-base64-at-all!!"}},
    ])])
    s = store().list()[0]
    assert s.title == "broken picture"
    assert s.thumb is None


# ── cache ─────────────────────────────────────────────────────────────────────

def test_second_scan_does_not_reparse(store, monkeypatch):
    """The point of the cache: a warm list() is stat()-only. Real transcripts are megabytes,
    so a rescan on every open would jank the UI."""
    _write(store.project, "s1", [_user("something")])
    store().list()                       # warm the cache

    calls = []
    real = sessions._scan_file
    monkeypatch.setattr(sessions, "_scan_file", lambda p: (calls.append(p), real(p))[1])
    store().list()
    assert calls == [], "a cached session was parsed again"


def test_cache_invalidates_when_the_transcript_grows(store, monkeypatch):
    path = _write(store.project, "s1", [_user("first question")])
    assert store().list()[0].messages == 1

    _write(store.project, "s1", [_user("first question"), _user("second question")])
    assert store().list()[0].messages == 2, "a changed transcript was served from cache"


def test_cache_invalidates_on_mtime_even_at_the_same_size(store):
    """An append changes the size, so the size check alone hides a missing mtime check.
    A same-length rewrite is the case that separates them."""
    import os
    path = _write(store.project, "s1", [_user("aaaa")])
    store().list()
    before = path.stat().st_size

    _write(store.project, "s1", [_user("bbbb")])
    assert path.stat().st_size == before, "test needs a same-size rewrite to mean anything"
    os.utime(path, (1_800_000_000, 1_800_000_000))

    assert store().list()[0].title == "bbbb", "same-size edit was served from cache"


def test_corrupt_cache_is_rebuilt_not_fatal(store, tmp_path):
    _write(store.project, "s1", [_user("hello")])
    store().list()
    (tmp_path / "cache" / "index.json").write_text("{{{ not json", encoding="utf-8")
    assert store().list()[0].title == "hello"


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_removes_the_transcript(store):
    _write(store.project, "s1", [_user("throwaway")])
    st = store()
    s = st.list()[0]
    assert st.delete(s) is True
    assert not s.path.exists()
    assert st.list() == []


def test_delete_removes_the_cached_thumbnail(store):
    _write(store.project, "s1", [_user(blocks=[
        {"type": "text", "text": "with a picture"},
        {"type": "image", "source": {"type": "base64", "data": _jpeg_b64()}},
    ])])
    st = store()
    s = st.list()[0]
    from pathlib import Path
    thumb = Path(s.thumb)
    assert thumb.exists()
    st.delete(s)
    assert not thumb.exists(), "thumbnail outlived its session"


def test_delete_is_idempotent(store):
    """The caller wants the session gone; it already being gone is success, not an error."""
    _write(store.project, "s1", [_user("throwaway")])
    st = store()
    s = st.list()[0]
    assert st.delete(s) is True
    assert st.delete(s) is True


def test_delete_leaves_the_other_sessions_alone(store):
    _write(store.project, "keep", [_user("important")])
    _write(store.project, "drop", [_user("throwaway")])
    st = store()
    target = next(s for s in st.list() if s.id == "drop")
    st.delete(target)
    assert [s.id for s in st.list()] == ["keep"]


def test_delete_drops_the_index_entry(store, tmp_path):
    """list() only looks up sessions whose file still exists, so a stale entry cannot
    resurrect one -- but it would accumulate forever across deletes. Asserted on the index
    file itself, because nothing in list()'s output would ever show the leak."""
    import json as _json
    _write(store.project, "keep", [_user("important")])
    _write(store.project, "drop", [_user("throwaway")])
    st = store()
    st.delete(next(s for s in st.list() if s.id == "drop"))

    index = _json.loads((tmp_path / "cache" / "index.json").read_text(encoding="utf-8"))
    assert "drop" not in index, "deleted session left an entry in the cache index"
    assert "keep" in index, "delete pruned an unrelated session"
    assert [s.id for s in store().list()] == ["keep"]
