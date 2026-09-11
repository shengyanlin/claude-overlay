"""Read the Claude CLI's transcript store so the overlay can show you your own history.

The CLI writes one .jsonl per conversation under ~/.claude/projects/<slug>/, where <slug> is
that conversation's working directory with every non-alphanumeric character replaced by '-'.
`--resume` only works from the directory the session was recorded in, so a listing is always
scoped to one project dir; showing sessions from elsewhere would offer resumes that fail.

Each line is one record. Four types matter here:

  ai-title   the CLI's own generated title ("Claude Overlay setup"). Rewritten every turn
             with the same value, and the first copy lands around line 10, so it is cheap
             to reach. Not every session has one -- the CLI generates it, and the overlay's
             SDK path does not appear to trigger it, so treat it as a bonus, not a given.
  user       one message. `message.content` is a list of blocks: {"type":"text"} and
             {"type":"image"} whose source.data is base64 JPEG.
  assistant  the reply. Not read; user messages alone give a good "how much happened here".
  summary    written by /compact.

Why the scanning looks the way it does: an overlay session embeds every screenshot as base64
in the transcript, so these files reach several MB across only a few hundred lines -- one
line can be 200 KB. So nothing is parsed that isn't needed (a substring test gates every
json.loads), the file is streamed rather than read whole, and a full scan is cached against
(size, mtime) so a rescan costs one stat() per session.
"""
import base64
import binascii
import io
import json
import re
import shutil
import time
from pathlib import Path

TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
THUMB_WIDTH = 128          # enough to recognise a window at a glance, ~3 KB on disk

# The overlay prefixes a screenshot turn with a bracketed note (see _compose_note in
# claude_overlay.py). Those notes are machinery, not something you typed, so they are
# stripped before a message is used as a title -- otherwise every overlay session would be
# called "[Attached: a live screenshot of my screen...".
_NOTE = re.compile(r"\[(?:Attached:|My screen)[^\]]*\]\s*", re.S)
# ...except for one part worth keeping: a window-scoped shot names the window you were in.
# That is the best short label this product can offer, because it says what you were DOING
# rather than what you typed. Only present when Window-only was on for that turn.
_WINDOW = re.compile(r"ACTIVE WINDOW only\s*[\u2014-]\s*[\u201c\"]([^\u201d\"]+)[\u201d\"]")
_LEGACY_NOTE = re.compile(r"^\[ATTACHMENTS\][^\n]*\n?", re.M)
# Slash-command wrappers, shell echoes and interrupt markers are recorded as user turns but
# are not things you typed at Claude. See _typed() for why this matters so much.
_MACHINERY = re.compile(
    r"^\s*(?:<(?:command-(?:name|message|args)|local-command-[a-z]+|"
    r"bash-(?:input|stdout|stderr))>|\[Request interrupted)", re.I)


def project_slug(cwd):
    """The CLI's folder name for a working directory. Case is preserved, drive letter
    included: 'C:\\Users\\u' and 'c:\\Users\\u' really are two different folders in the
    store, so this mirrors that rather than normalising and reading the wrong one."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_dir(cwd, root=None):
    return (Path(root) if root is not None else TRANSCRIPT_ROOT) / project_slug(cwd)


class Session:
    """One conversation on disk. `title` is what to show; `subtitle` is the supporting line
    (empty when there is nothing worth adding)."""
    __slots__ = ("id", "path", "title", "subtitle", "messages", "size", "mtime", "thumb")

    def __init__(self, id, path, title, subtitle, messages, size, mtime, thumb=None):
        self.id, self.path = id, path
        self.title, self.subtitle = title, subtitle
        self.messages, self.size, self.mtime, self.thumb = messages, size, mtime, thumb

    @property
    def age(self):
        return max(0.0, time.time() - self.mtime)

    def __repr__(self):
        return f"<Session {self.id[:8]} {self.title!r} msgs={self.messages}>"


def _loads(line):
    try:
        return json.loads(line)
    except Exception:
        return None      # a torn last line is normal while the CLI is mid-write


def _typed(rec):
    """(text, first base64 image) for a message a human actually typed — or None for the
    many 'user' records that are machinery.

    A user record is not the same thing as a message. In one real overlay session, 128 user
    records broke down as 95 tool_result blocks (the CLI writes tool output back as a user
    turn), 20 slash-command wrappers, 5 isMeta notes, 3 interrupt markers — and only 11
    messages. Counting them all made an 11-message conversation report 128 and take its
    title from a caveat banner, so this filter is what makes both numbers mean anything.

    Tolerates both content shapes: the CLI writes a list of blocks, but a plain string is
    also valid in the format.
    """
    if rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    img = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "tool_result":
                return None                    # a tool turn wearing the user role
            if kind == "text":
                parts.append(b.get("text") or "")
            elif kind == "image" and img is None:
                src = b.get("source") or {}
                if src.get("type") == "base64" and src.get("data"):
                    img = src["data"]
        text = "\n".join(parts)
    else:
        return None
    if _MACHINERY.match(text or ""):
        return None
    return text, img


def _clean(text):
    """A message with the overlay's bracketed screenshot notes removed."""
    text = _LEGACY_NOTE.sub("", text or "")
    return " ".join(_NOTE.sub("", text).split())


def _title_from(text, window, ai_title):
    """Pick the best label available, best first.

    ai_title is the CLI's own, and it read the conversation to write it -- nothing cheaper
    beats it. Failing that, what you were LOOKING AT beats what you typed, which is why a
    window name outranks the message: 'LINE' locates a conversation faster than its opening
    sentence does. Truncating the message is the last resort, not the plan.
    """
    if ai_title:
        return ai_title, _short(text, 60)
    if window:
        return window, _short(text, 60)
    return (_short(text, 48) or "(no messages)"), ""


def _short(text, n):
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n - 1].rstrip() + "\u2026"


def _scan_file(path):
    """Stream one transcript. Returns (title, subtitle, messages, first_image_b64)."""
    ai_title = window = None
    first_text = ""
    first_img = None
    seen_first = False
    messages = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Cheap gate first: these lines are frequently 200 KB of base64, and json.loads
            # on every one of them is the whole cost of a scan.
            if ai_title is None and "ai-title" in line:
                rec = _loads(line)
                if rec and rec.get("type") == "ai-title":
                    ai_title = (rec.get("aiTitle") or "").strip() or None
                continue
            if '"user"' not in line:
                continue
            rec = _loads(line)
            if not rec or rec.get("type") != "user":
                continue          # e.g. a message that merely contains the word "user"
            got = _typed(rec)
            if got is None:
                continue          # tool result / slash command / meta note — not a message
            messages += 1
            if seen_first:
                continue
            raw, img = got
            if window is None:
                m = _WINDOW.search(raw or "")
                if m:
                    window = m.group(1).strip() or None
            cleaned = _clean(raw)
            if cleaned or img:
                seen_first = True
                first_text, first_img = cleaned, img
    title, subtitle = _title_from(first_text, window, ai_title)
    return title, subtitle, messages, first_img


def _thumb_path(cache_dir, sid):
    return Path(cache_dir) / f"{sid}.jpg"


def _write_thumb(b64, dest):
    """Decode a transcript's first screenshot down to a recognisable strip. Best-effort:
    a session with no usable image simply has no thumbnail, which the UI must handle
    anyway (pasted-image-only and text-only sessions exist)."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        raw = base64.b64decode(b64, validate=False)
        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w = THUMB_WIDTH
        img.thumbnail((w, w), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=72)
        return dest
    except (binascii.Error, OSError, ValueError):
        return None


class Store:
    """A project's transcripts, with a scan cache.

    Paths are injected rather than read from config so the whole thing is testable against a
    temp directory, and so a caller can list a project other than its own working dir.
    """

    def __init__(self, cwd, root=None, cache_dir=None):
        self.dir = project_dir(cwd, root)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else self.dir / ".overlay-cache"
        self._index_path = self.cache_dir / "index.json"
        self._index = self._load_index()

    # ── cache ────────────────────────────────────────────────────────────────
    def _load_index(self):
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}      # absent or corrupt → rebuild; the cache is never authoritative

    def _save_index(self):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._index), encoding="utf-8")
            tmp.replace(self._index_path)     # atomic: a killed write can't corrupt the cache
        except Exception:
            pass           # a read-only cache dir costs speed, never correctness

    # ── listing ──────────────────────────────────────────────────────────────
    def list(self, thumbs=True):
        """Every session in this project, newest first. Slow enough (MBs of JSON) that
        callers should run it off the UI thread the first time; cached runs are stat()-only."""
        out, changed = [], False
        for path in sorted(self.dir.glob("*.jsonl")) if self.dir.is_dir() else []:
            try:
                stat = path.stat()
            except OSError:
                continue
            sid = path.stem
            hit = self._index.get(sid)
            fresh = (isinstance(hit, dict) and hit.get("size") == stat.st_size
                     and hit.get("mtime") == stat.st_mtime)
            if not fresh:
                title, subtitle, messages, img = _scan_file(path)
                thumb = None
                if thumbs and img:
                    made = _write_thumb(img, _thumb_path(self.cache_dir, sid))
                    thumb = str(made) if made else None
                hit = {"size": stat.st_size, "mtime": stat.st_mtime, "title": title,
                       "subtitle": subtitle, "messages": messages, "thumb": thumb}
                self._index[sid] = hit
                changed = True
            thumb = hit.get("thumb")
            if thumb and not Path(thumb).exists():
                thumb = None          # cache dir wiped by hand; the row still works
            out.append(Session(sid, path, hit["title"], hit.get("subtitle", ""),
                               hit["messages"], hit["size"], hit["mtime"], thumb))
        if changed:
            self._prune_index({p.stem for p in self.dir.glob("*.jsonl")} if self.dir.is_dir() else set())
            self._save_index()
        out.sort(key=lambda s: s.mtime, reverse=True)
        return out

    def _prune_index(self, live):
        for sid in [k for k in self._index if k not in live]:
            self._index.pop(sid, None)

    # ── mutation ─────────────────────────────────────────────────────────────
    def delete(self, session):
        """Remove a transcript and its cached thumbnail. Returns True when the file is gone
        afterwards -- including when it was already gone, since the caller's goal is the end
        state, not who did it."""
        path = Path(getattr(session, "path", session))
        sid = path.stem
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        try:
            _thumb_path(self.cache_dir, sid).unlink()
        except OSError:
            pass
        if self._index.pop(sid, None) is not None:
            self._save_index()
        return not path.exists()

    def clear_cache(self):
        self._index = {}
        try:
            shutil.rmtree(self.cache_dir)
        except OSError:
            pass
