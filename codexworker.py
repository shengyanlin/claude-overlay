# -*- coding: utf-8 -*-
"""The background Codex worker: the agent behind the titlebar's Claude ⇄ Codex switch.

Drives the `codex` CLI over `codex app-server` — newline-delimited JSON-RPC on stdio, the
same protocol the Codex desktop app and IDE extension use — and speaks the UI's existing
event vocabulary ("delta", "think", "tool", "result", "turn_done", "ctx", …), so the chat
renders a Codex turn with exactly the code that renders a Claude one. The public surface
(ask / reset / compact / resume / interrupt / set_model / set_permission_mode / set_effort /
shutdown) is ClaudeWorker's; respond_approval is the one addition, because Codex can stop
mid-turn to ask before it runs something and the overlay has to be able to answer.

Threads: run() owns all protocol state and is the only reader of the inbox. A reader thread
per app-server process turns stdout lines into inbox items (responses go straight to the
caller waiting in _Rpc.request), so a long turn never blocks a UI request behind it.
Imports config + debuglog (one-way), like worker.py."""

import collections
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from config import *
from config import __version__
from debuglog import dbg, DEBUG_LOG, _UIQueueTap

# The same PATH help worker.py gives the `claude` CLI: a global npm install lands in
# %APPDATA%\npm, which a session started before the install can't see yet. Node's own
# folders too (install-node.ps1's per-user one, the MSI's machine-wide one): an overlay that
# restarts itself after an update keeps the PATH it started with, and when codex_command
# can't reach past the npm shim, the shim needs `node`.
def _env_dir(var, *sub):
    base = os.environ.get(var)
    return os.path.join(base, *sub) if base else ""   # never a relative PATH entry


os.environ["PATH"] = os.pathsep.join(filter(None, [
    _env_dir("APPDATA", "npm"),
    os.environ.get("PATH", ""),
    _env_dir("LOCALAPPDATA", "Programs", "nodejs"),
    _env_dir("ProgramFiles", "nodejs"),
]))

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
CLIENT_NAME = "claude_overlay"
_INIT_TIMEOUT = 30          # initialize + the first thread/start (MCP servers start here)
_CALL_TIMEOUT = 30          # every other request; the slow parts of a turn are notifications

# Server→client requests the overlay answers itself, and how. Approvals become an in-chat
# card (see Overlay._approval_card); everything else gets the answer that lets the turn go
# on: there is no UI to fill in a form or a structured question, and no client-side tools.
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "files",
    "item/permissions/requestApproval": "permissions",
    "execCommandApproval": "command",          # v1 spellings, still sent by older servers
    "applyPatchApproval": "files",
}
_SANDBOX_ORDER = ("danger-full-access", "workspace-write", "read-only")
_SANDBOX_POLICY = {"danger-full-access": {"type": "dangerFullAccess"},
                   "workspace-write": {"type": "workspaceWrite"},
                   "read-only": {"type": "readOnly"}}
_SANDBOX_NAMES = {"dangerFullAccess": "danger-full-access",
                  "workspaceWrite": "workspace-write", "readOnly": "read-only"}
# Whether this machine's Windows sandbox can actually run a command in workspace-write,
# remembered per Codex version (see _probe_workspace_write).
_PROBE_FILE = STATE_FILE.parent / "codex-sandbox-probe.json"
_PROBE_TIMEOUT = 15
_RATE_POLL_S = 60          # the allowance, re-read while idle — usage.py's cadence for Claude
_QUOTA_WARN_AT = 0.80      # Codex sends no "warning" status of its own; this is the tier at
                           # which the UI's once-per-transition notice speaks up


def codex_command():
    """The argv that starts `codex`, or None when it isn't installed.

    CLAUDE_OVERLAY_CODEX_CMD (a JSON list) replaces it outright — the test suite's fake
    app-server goes in through here. Otherwise the `codex` on PATH, resolved past the npm
    shim to the native binary when there is one: the shim is a .cmd that runs node, which
    runs codex.exe — two extra processes, a console window under pythonw, and a Ctrl+C
    path that orphans the real server."""
    override = os.environ.get("CLAUDE_OVERLAY_CODEX_CMD")
    if override:
        try:
            argv = json.loads(override)
            if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
                return argv
        except Exception:
            pass
    exe = shutil.which("codex")
    if not exe:
        return None
    if os.path.splitext(exe)[1].lower() in (".cmd", ".bat", ".ps1", ""):
        native = _npm_native_exe(os.path.dirname(exe))
        if native:
            return [native]
    return [exe]


def _npm_native_exe(npm_dir):
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    triple = "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"
    root = os.path.join(npm_dir, "node_modules", "@openai", "codex")
    for base in (os.path.join(root, "node_modules", "@openai", f"codex-win32-{arch}", "vendor"),
                 os.path.join(root, "vendor")):
        exe = os.path.join(base, triple, "bin", "codex.exe")
        if os.path.isfile(exe):
            return exe
    return None


class RpcError(Exception):
    def __init__(self, error):
        self.error = error if isinstance(error, dict) else {"message": str(error)}
        super().__init__(self.error.get("message") or str(error))


class _Rpc:
    """One `codex app-server` process and the JSON-RPC plumbing around it."""

    def __init__(self, argv, inbox, gen):
        env = dict(os.environ)
        if any("node_modules" in a for a in argv[:1]):
            env.setdefault("CODEX_MANAGED_BY_NPM", "1")   # what the npm launcher would set
        self.proc = subprocess.Popen(
            argv + ["app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=WORKING_DIR, env=env, creationflags=_CREATE_NO_WINDOW)
        self.inbox, self.gen = inbox, gen
        self._wlock = threading.Lock()
        self._plock = threading.Lock()
        self._pending = {}           # id → [Event, response-or-None]
        self._next_id = 0
        self.alive = True
        self.stderr_tail = collections.deque(maxlen=20)
        threading.Thread(target=self._read, name="codex-rpc-out", daemon=True).start()
        threading.Thread(target=self._drain_err, name="codex-rpc-err", daemon=True).start()

    def _read(self):
        try:
            for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" not in msg and "id" in msg:      # a response to one of ours
                    with self._plock:
                        slot = self._pending.pop(msg.get("id"), None)
                    if slot is not None:
                        slot[1] = msg
                        slot[0].set()
                    continue
                self.inbox.put(("srv", (self.gen, msg)))
        except Exception:
            pass
        finally:
            self.alive = False
            with self._plock:
                pending, self._pending = self._pending, {}
            for slot in pending.values():
                slot[0].set()                                # waiters see None → ConnectionError
            self.inbox.put(("exit", self.gen))

    def _drain_err(self):
        try:
            for raw in self.proc.stderr:
                s = raw.decode("utf-8", "replace").rstrip()
                if s:
                    self.stderr_tail.append(s)
                    dbg("codex_stderr", s[:300])
        except Exception:
            pass

    def _write(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except Exception as e:
                self.alive = False
                raise ConnectionError(f"codex app-server is gone ({type(e).__name__})")

    def request(self, method, params=None, timeout=_CALL_TIMEOUT):
        """Send a request and wait for its response; the result, or RpcError."""
        ev, slot = self._send(method, params)
        if not ev.wait(timeout):
            with self._plock:
                self._pending.pop(slot[2], None)
            raise TimeoutError(f"{method} got no answer in {timeout}s")
        resp = slot[1]
        if resp is None:
            raise ConnectionError("codex app-server exited")
        if "error" in resp:
            raise RpcError(resp["error"])
        return resp.get("result")

    def request_nowait(self, method, params=None):
        """Fire-and-forget, for turn/interrupt — which the UI thread sends. The write happens
        on a thread of its own: _write takes the write lock and flushes into a pipe, and if a
        big turn/start is mid-write or app-server has stopped reading, that blocks — which
        on the Tk thread would freeze the whole window on the Stop click meant to rescue it."""
        def send():
            try:
                ev, slot = self._send(method, params)
                if not ev.wait(_CALL_TIMEOUT):
                    with self._plock:
                        self._pending.pop(slot[2], None)
            except Exception:
                pass
        threading.Thread(target=send, name="codex-rpc-nowait", daemon=True).start()

    def _send(self, method, params):
        with self._plock:
            self._next_id += 1
            rid = self._next_id
            ev = threading.Event()
            slot = [ev, None, rid]
            self._pending[rid] = slot
        msg = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
        except Exception:
            with self._plock:
                self._pending.pop(rid, None)
            raise
        return ev, slot

    def notify(self, method, params=None):
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def respond(self, rid, result=None, error=None):
        msg = {"id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self._write(msg)

    def close(self):
        self.alive = False
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=DISCONNECT_TIMEOUT)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class CodexWorker(threading.Thread):
    def __init__(self, ui_queue: "queue.Queue", permission_mode=None, effort=None):
        super().__init__(daemon=True)
        self.ui = _UIQueueTap(ui_queue) if DEBUG_LOG else ui_queue
        self.req = self._inbox = queue.Queue()  # `req` for parity with ClaudeWorker
        self._deferred = collections.deque()  # UI requests set aside while draining a turn
        self._running = True
        self._rpc = None
        self._gen = 0                          # bumped per process: a dead one's leftovers are ignored
        self._thread_id = None
        self._resume_id = None                 # set → the next _ensure_thread resumes it
        self._turn = None                      # the in-flight turn's state, or None
        self._turn_lock = threading.Lock()     # guards _turn's ids for interrupt() (UI thread)
        self._compact = None                   # {"pre", "t0", "done"} while compaction runs
        self._approvals = {}                   # pending server request id → method
        self._asked_perms = {}                 # permissions-request id → what it asked for
        self._usage = None                     # latest tokenUsage, emitted after turn_done
        self._ctx_tokens = None
        self._rl = {}                          # merged rate-limit snapshot (updates are sparse)
        self._allowed_sandboxes = None         # from configRequirements/read; None = unknown
        self._allowed_approvals = None
        self._announced_sandbox = None
        self._downgrade_told = False
        self._cli_version = None
        self._ws_ok = None                     # workspace-write sandbox works here? None = unknown
        self._ws_probing = False
        self._ws_told = False
        self._last_quota = None
        self._last_status = None               # the last ("quota", …) reading sent, for dedupe
        self._asking = False                   # an ask is being turned into a turn (see interrupt)
        self._cancel = False                   # Stop arrived before that turn had an id
        self._bg_busy = set()                  # tags of background reads in flight
        self._next_rate_poll = 0.0
        self._next_auth_poll = 0.0
        self._signed_in = None                 # last known Codex login state
        self.models = []                       # [{id, label, efforts, default_effort, is_default}]
        self._model = CODEX_MODEL or None      # explicit model override; None = Codex's default
        self._served_model = None              # what the thread actually runs
        self._read_only = (permission_mode == "plan")
        self._effort = CODEX_EFFORT if effort is None else (effort or "")

    # ── the UI-facing surface (any thread) ──
    def ask(self, text: str, image_paths=None):
        self._inbox.put(("ask", (text, list(image_paths or []))))
    def reset(self):                  self._inbox.put(("reset", None))
    def compact(self):                self._inbox.put(("compact", None))
    def resume(self, session_id):     self._inbox.put(("resume", str(session_id)))
    def set_model(self, model):       self._inbox.put(("set_model", model))
    def set_permission_mode(self, mode): self._inbox.put(("set_permission_mode", mode))
    def set_effort(self, level):      self._inbox.put(("set_effort", level))
    def respond_approval(self, rid, decision):
        self._inbox.put(("approve", (rid, decision)))
    def list_sessions(self):          self._inbox.put(("list_sessions", None))
    def archive(self, thread_id):     self._inbox.put(("archive", str(thread_id)))

    def shutdown(self):
        self._running = False
        self._inbox.put(("stop", None))

    def kill_server(self):
        """Last resort for a CLI update: end app-server now, even mid-request (it holds
        codex.exe open, and Windows won't let npm replace a running binary)."""
        rpc = self._rpc
        if rpc is not None:
            try:
                rpc.proc.kill()
            except Exception:
                pass

    def interrupt(self):
        """Stop the in-flight turn. Called on the UI thread, so it only writes the request:
        the turn's own turn/completed (status "interrupted") ends it through the normal path."""
        with self._turn_lock:
            t = self._turn if self._turn is not None else self._compact
            ids = (self._thread_id, t.get("id")) if t else None
            if self._asking and not (ids and ids[1]):
                # Stop clicked before the turn has an id (still starting app-server, reading
                # attachments, or waiting on turn/start): remember it. _start_turn checks it
                # before sending, and interrupts the moment the id arrives.
                self._cancel = True
                return
        rpc = self._rpc
        if ids and ids[1] and rpc is not None and rpc.alive:
            rpc.request_nowait("turn/interrupt", {"threadId": ids[0], "turnId": ids[1]})

    # ── main loop ──
    def run(self):
        dbg("codex_worker_start")
        try:
            self._ensure_thread()
        except Exception as e:
            self.ui.put(("error", f"Codex couldn't start: {type(e).__name__}: {e}"))
        while self._running:
            if self._deferred:
                item = self._deferred.popleft()
            else:
                try:
                    item = self._inbox.get(timeout=1.0)
                except queue.Empty:
                    self._watchdog()
                    continue
            try:
                if not self._step(item):
                    break
            except Exception as e:
                dbg("codex_step_err", f"{type(e).__name__}: {e}")
                self.ui.put(("error", f"{type(e).__name__}: {e}"))
                if self._turn is not None:
                    self._finish_turn({"status": "failed", "error": {"message": str(e)}})
        self._teardown()

    def _step(self, item):
        kind, payload = item
        if kind == "stop":
            return False
        if kind == "srv":
            gen, msg = payload
            if gen == self._gen:
                self._on_server(msg)
        elif kind == "exit":
            if payload == self._gen:
                self._on_exit()
        elif kind == "ask":
            if self._turn is not None or self._compact is not None:
                self._deferred.append(item)        # one turn at a time, in order
                self._park_until_idle()
            else:
                self._start_turn(*payload)
        elif kind == "reset":
            self._do_reset()
        elif kind == "compact":
            self._do_compact()
        elif kind == "resume":
            self._do_resume(payload)
        elif kind == "set_model":
            self._model = str(payload) if payload else None
            cur = self._model or self._served_model
            levels = next((m.get("efforts") for m in self.models if m["id"] == cur), None)
            if levels and self._effort not in ("", "auto") and self._effort not in levels:
                # The new model can't run the pinned level (Ultra → a model without it); every
                # turn would fail. Fall back to its own default and say so via the ⚡ label.
                self._effort = "auto"
                self.ui.put(("effort", "auto"))
            self.ui.put(("model", self._model or self._served_model or "codex"))
            self.ui.put(("status", ""))
        elif kind == "set_permission_mode":
            to_plan = (payload == "plan")
            if to_plan and (self._turn is not None or self._compact is not None):
                # Sandbox and approvals are per TURN: the running one keeps the policy it
                # started with. Claiming Read-only while it can still write would be a lie
                # about a safety state, so it is stopped first and the lock confirmed after.
                self.ui.put(("system", "⏹ Stopping the running reply so Read-only applies "
                                       "right away."))
                self._stop_turn_and_wait()
            self._read_only = to_plan
            if not to_plan and self._ws_ok is None:
                self._probe_workspace_write()      # launched Read-only: never probed yet
            self.ui.put(("codex_sandbox", self._sandbox_mode()))
            self.ui.put(("permission_mode", "plan" if self._read_only else str(payload)))
            self.ui.put(("status", ""))
            self._announce_sandbox()
        elif kind == "set_effort":
            self._effort = payload or ""
            self.ui.put(("effort", self._effort))
            self.ui.put(("status", ""))
        elif kind == "approve":
            self._answer_approval(*payload)
        elif kind == "ws_probe":
            self._ws_probe_done(*payload)
        elif kind == "bg":
            self._bg_done(*payload)
        elif kind == "list_sessions":
            if self._ensure_server():
                self._bg_call("threads", "thread/list", {
                    "cwd": WORKING_DIR, "limit": 40, "sortKey": "updated_at"})
            else:
                self.ui.put(("sessions_failed", "Codex isn't running"))
        elif kind == "archive":
            if self._rpc is not None and self._rpc.alive:
                self._bg_call("archive:" + payload, "thread/archive", {"threadId": payload})
        return True

    def _park_until_idle(self):
        """An ask arrived mid-turn (the UI's own line-up normally prevents this). It waits in
        _deferred; keep serving server traffic until the turn ends so it can go out."""
        self._drain_until(lambda: self._turn is None and self._compact is None, None)

    def _drain_until(self, done, timeout):
        """Process server traffic (and only server traffic) until done() or the timeout.
        UI requests that arrive meanwhile are set aside, in order, for the main loop."""
        end = None if timeout is None else time.monotonic() + timeout
        while self._running and not done():
            left = 1.0 if end is None else min(1.0, end - time.monotonic())
            if left <= 0:
                return False
            try:
                item = self._inbox.get(timeout=left)
            except queue.Empty:
                self._watchdog()
                continue
            if item[0] in ("srv", "exit", "ws_probe", "bg"):
                self._step(item)
            elif item[0] == "stop":
                self._running = False
            elif item[0] == "approve":
                self._step(item)               # an answer can be what the turn is waiting on
            else:
                self._deferred.append(item)
        return done()

    def _teardown(self):
        rpc, self._rpc = self._rpc, None
        if rpc is not None:
            rpc.close()

    # ── server lifecycle ──
    def _ensure_server(self):
        if self._rpc is not None and self._rpc.alive:
            return True
        if self._rpc is not None and self._thread_id and not self._resume_id:
            # The old process died and its "exit" hasn't been handled yet (it will be ignored
            # as stale once the generation moves on) — the thread must still be RESUMED in the
            # new process, or every turn would name a thread the new server never loaded.
            self._resume_id = self._thread_id
        argv = codex_command()
        if not argv:
            self.ui.put(("error", "The Codex CLI isn't installed. Install it with  "
                                  "npm install -g @openai/codex  then run  codex login  "
                                  "in a terminal, and send your message again."))
            return False
        self._gen += 1
        self.ui.put(("status", "starting Codex…"))
        try:
            rpc = _Rpc(argv + _mcp_flags(), self._inbox, self._gen)
        except Exception as e:
            self.ui.put(("error", f"Couldn't start the Codex CLI ({' '.join(argv)}): "
                                  f"{type(e).__name__}: {e}"))
            self.ui.put(("status", ""))
            return False
        try:
            init = rpc.request("initialize", {"clientInfo": {
                "name": CLIENT_NAME, "title": "Claude Overlay", "version": __version__}},
                timeout=_INIT_TIMEOUT) or {}
            rpc.notify("initialized")
            m = re.search(r"/(\d+\.\d+\.\d+)", str(init.get("userAgent") or ""))
            self._cli_version = m.group(1) if m else None
        except Exception as e:
            tail = "\n".join(list(rpc.stderr_tail)[-6:])
            rpc.close()
            self.ui.put(("error", f"Codex app-server didn't start: {type(e).__name__}: {e}"
                                  + (f"\nCodex said:\n{tail}" if tail else "")
                                  + "\nCheck  codex --version  and  codex login  in a terminal."))
            self.ui.put(("status", ""))
            return False
        self._rpc = rpc
        self._read_requirements()          # synchronous: the sandbox choice depends on it
        self._probe_workspace_write()
        # Menus, the allowance and the login state are not prerequisites of a first turn —
        # serial reads here held every queued message back by however slow they were.
        now = time.monotonic()
        self._next_rate_poll = now + _RATE_POLL_S
        self._next_auth_poll = now + AUTH_CHECK_INTERVAL
        self._bg_call("models", "model/list", {})
        self._bg_call("rl", "account/rateLimits/read")
        self._bg_call("acct", "account/read", {})
        return True

    # ── background reads: allowance, login, past conversations ──
    def _periodic(self):
        """Called on every idle second. Re-reads the allowance (so the ring is right while the
        overlay sits unused, as Claude's is) and the login (so a sign-out is announced before
        a message is lost to it) — each off this thread, one at a time."""
        rpc = self._rpc
        if rpc is None or not rpc.alive:
            return
        now = time.monotonic()
        if now >= self._next_rate_poll:
            self._next_rate_poll = now + _RATE_POLL_S
            self._bg_call("rl", "account/rateLimits/read")
        if now >= self._next_auth_poll:
            self._next_auth_poll = now + AUTH_CHECK_INTERVAL
            self._bg_call("acct", "account/read", {})

    def recheck_auth(self):
        """Read the login on the next idle second instead of at the next poll — the UI calls
        this after its "Set up Codex" console signed in, so held sends free up right away."""
        self._next_auth_poll = 0.0

    def _bg_call(self, tag, method, params=None):
        if tag in self._bg_busy:
            return
        rpc, gen = self._rpc, self._gen
        if rpc is None:
            return
        self._bg_busy.add(tag)

        def work():
            try:
                res = rpc.request(method, params)
            except Exception as e:
                res = e
            self._inbox.put(("bg", (gen, tag, res)))
        threading.Thread(target=work, name=f"codex-{tag}", daemon=True).start()

    def _bg_done(self, gen, tag, res):
        self._bg_busy.discard(tag)
        if gen != self._gen:
            return
        failed = isinstance(res, Exception)
        if tag == "models" and not failed:
            self._adopt_models((res or {}).get("data") or [])
        elif tag == "rl" and not failed:
            self._merge_rate_limits((res or {}).get("rateLimits"), full=True)
        elif tag == "acct" and not failed:
            self._account(res or {})
        elif tag == "threads":
            if failed:
                self.ui.put(("sessions_failed", f"{type(res).__name__}: {res}"))
            else:
                self.ui.put(("codex_sessions", _session_rows((res or {}).get("data") or [])))
        elif tag.startswith("archive:") and failed:
            self.ui.put(("error", f"Couldn't archive that conversation: {res}"))

    def _read_account(self):
        self._next_auth_poll = time.monotonic() + AUTH_CHECK_INTERVAL
        try:
            self._account(self._rpc.request("account/read", {}) or {})
        except Exception as e:
            dbg("codex_account", f"{type(e).__name__}: {e}")

    def _account(self, res):
        """Tell the UI when the Codex login comes or goes (it holds sends while it's gone).
        No account while OpenAI auth is required = signed out; an API-key or a custom
        provider setup that needs no OpenAI login counts as signed in."""
        ok = bool(res.get("account")) or not res.get("requiresOpenaiAuth", True)
        if ok != self._signed_in:
            self._signed_in = ok
            self.ui.put(("codex_auth", {"ok": ok}))

    def _wants_workspace_write(self):
        return self._sandbox_mode(probe_aware=False) == "workspace-write"

    def _probe_workspace_write(self):
        """Find out whether workspace-write can run anything at all, before a turn needs it.

        On Windows that sandbox is a helper that has to set itself up, and it can fail in a
        way nothing reports: measured 2026-10-28 on a managed laptop, every command under
        workspace-write hung until the model gave up ("setup helper exited with status 143"
        via `codex exec`; through app-server not even an item appeared), while read-only ran
        the same command in 3s. A turn in that state just stalls. So run one `echo` under the
        policy with command/exec — local, no model call — and remember the answer per Codex
        version, so only the first launch after an upgrade pays for it."""
        if sys.platform != "win32" or not self._wants_workspace_write():
            return
        cached = _load_probe()
        if cached.get("version") == self._cli_version and isinstance(cached.get("ok"), bool):
            self._ws_ok = cached["ok"]
            return
        if self._ws_probing:
            return
        self._ws_probing = True
        rpc, gen = self._rpc, self._gen

        def work():
            ok = False
            try:
                res = rpc.request("command/exec", {
                    "command": ["cmd.exe", "/d", "/c", "echo overlay-sandbox-ok"],
                    "sandboxPolicy": _SANDBOX_POLICY["workspace-write"],
                    "timeoutMs": _PROBE_TIMEOUT * 1000}, timeout=_PROBE_TIMEOUT + 3) or {}
                ok = res.get("exitCode") == 0 and "overlay-sandbox-ok" in str(res.get("stdout"))
            except Exception as e:
                dbg("codex_ws_probe", f"{type(e).__name__}: {e}")
            self._inbox.put(("ws_probe", (gen, ok)))
        threading.Thread(target=work, name="codex-ws-probe", daemon=True).start()

    def _ws_probe_done(self, gen, ok):
        self._ws_probing = False
        if gen != self._gen:
            return
        self._ws_ok = bool(ok)
        _save_probe({"version": self._cli_version, "ok": self._ws_ok})
        dbg("codex_ws_probe", f"workspace-write ok={self._ws_ok}")
        self._announce_sandbox()

    def _read_requirements(self):
        """What a managed machine allows. app-server downgrades a forbidden sandbox or
        approval policy WITHOUT failing the request, so asking for what isn't allowed would
        quietly run something else — read-only, in the case that prompted this."""
        try:
            req = (self._rpc.request("configRequirements/read") or {}).get("requirements") or {}
        except Exception as e:
            dbg("codex_requirements", f"{type(e).__name__}: {e}")
            return
        sb, ap = req.get("allowedSandboxModes"), req.get("allowedApprovalPolicies")
        self._allowed_sandboxes = list(sb) if isinstance(sb, list) and sb else None
        self._allowed_approvals = list(ap) if isinstance(ap, list) and ap else None

    def _adopt_models(self, data):
        out = []
        for m in data:
            if not isinstance(m, dict) or m.get("hidden") or not m.get("id"):
                continue
            efforts = [e.get("reasoningEffort") for e in (m.get("supportedReasoningEfforts") or [])
                       if isinstance(e, dict) and e.get("reasoningEffort")]
            out.append({"id": str(m["id"]), "label": str(m.get("displayName") or m["id"]),
                        "efforts": efforts, "default_effort": m.get("defaultReasoningEffort"),
                        "is_default": bool(m.get("isDefault"))})
        if out:
            self.models = out
            self.ui.put(("codex_models", list(out)))

    def _read_rate_limits(self):
        self._next_rate_poll = time.monotonic() + _RATE_POLL_S
        try:
            res = self._rpc.request("account/rateLimits/read") or {}
        except Exception as e:
            dbg("codex_ratelimits", f"{type(e).__name__}: {e}")
            return
        self._merge_rate_limits(res.get("rateLimits"), full=True)

    def _sandbox_mode(self, probe_aware=True):
        """The sandbox to ask for: Read-only, or the strongest mode the machine allows at or
        below CODEX_SANDBOX — skipping workspace-write where the probe found it can't run
        commands. Full access then means read-only WITH approvals: Codex asks to step
        outside the sandbox, and the reviewer (or the in-chat card) says yes or no."""
        if self._read_only:
            return "read-only"
        want = CODEX_SANDBOX if CODEX_SANDBOX in _SANDBOX_ORDER else "workspace-write"
        allowed = self._allowed_sandboxes
        for mode in _SANDBOX_ORDER[_SANDBOX_ORDER.index(want):]:
            if allowed is not None and mode not in allowed:
                continue
            if mode == "workspace-write" and probe_aware and self._ws_ok is False:
                continue
            return mode
        return "read-only"

    def _approval_policy(self):
        """No prompts when the sandbox is off and the machine allows it — the Codex reading
        of bypassPermissions. Otherwise "on-request": Codex asks only when it wants to step
        outside the sandbox, and the reviewer the user configured (or the card) answers."""
        allowed = self._allowed_approvals
        if self._sandbox_mode() == "danger-full-access" and (allowed is None or "never" in allowed):
            return "never"
        for p in ("on-request", "untrusted"):
            if allowed is None or p in allowed:
                return p
        return allowed[0] if allowed else "on-request"

    def _announce_sandbox(self):
        mode = self._sandbox_mode()
        if mode == self._announced_sandbox:
            return
        self.ui.put(("codex_sandbox", mode))
        if self._read_only:
            self._announced_sandbox = mode
            return
        if self._ws_probing:
            return                  # the verdict is seconds away; say it once, with the result
        self._announced_sandbox = mode
        policy_mode = self._sandbox_mode(probe_aware=False)
        why = []
        if policy_mode != CODEX_SANDBOX and not self._downgrade_told:
            self._downgrade_told = True
            why.append(f"this machine's Codex policy doesn't allow {CODEX_SANDBOX}")
        if self._ws_ok is False and policy_mode == "workspace-write" and not self._ws_told:
            self._ws_told = True
            why.append("its Windows sandbox can't run commands in workspace-write here "
                       "(the setup helper fails)")
        if why:
            self.ui.put(("system", f"🛡 Full access runs in {mode}: " + ", and ".join(why)
                                   + ". Codex asks before anything that writes or runs "
                                   "outside the sandbox."))

    def _thread_params(self):
        p = {"cwd": WORKING_DIR, "sandbox": self._sandbox_mode(),
             "approvalPolicy": self._approval_policy(),
             "developerInstructions": CODEX_SYSTEM_APPEND}
        if self._model:
            p["model"] = self._model
        return p

    def _ensure_thread(self):
        if not self._ensure_server():
            return False
        if self._thread_id and not self._resume_id:
            return True
        resuming = self._resume_id
        try:
            if resuming:
                res = self._rpc.request("thread/resume",
                                        dict(self._thread_params(), threadId=resuming),
                                        timeout=_INIT_TIMEOUT)
            else:
                res = self._rpc.request("thread/start", self._thread_params(),
                                        timeout=_INIT_TIMEOUT)
        except Exception as e:
            if resuming:
                self._resume_id = None
                raise
            self.ui.put(("error", f"Codex couldn't open a conversation: {type(e).__name__}: {e}"))
            self.ui.put(("status", ""))
            return False
        self._resume_id = None
        self._adopt_thread(res or {})
        return True

    def _adopt_thread(self, res):
        th = res.get("thread") or {}
        self._thread_id = th.get("id") or self._thread_id
        self._served_model = res.get("model") or th.get("model") or self._served_model
        sb = _SANDBOX_NAMES.get(((res.get("sandbox") or {}).get("type")))
        if sb and sb != self._sandbox_mode():
            dbg("codex_sandbox", f"asked {self._sandbox_mode()} got {sb}")
        if self._thread_id:
            self.ui.put(("session", self._thread_id))
        self.ui.put(("model", self._model or self._served_model or "codex"))
        self.ui.put(("ready", None))
        self.ui.put(("status", ""))
        self._announce_sandbox()

    def _on_exit(self):
        rpc, self._rpc = self._rpc, None
        tail = "\n".join(list(rpc.stderr_tail)[-4:]) if rpc else ""
        if not self._running:
            return
        # Keep the conversation: the next ask restarts the server and resumes this thread.
        if self._thread_id:
            self._resume_id = self._thread_id
        if self._turn is not None or self._compact is not None:
            self.ui.put(("error", "Codex stopped unexpectedly — the next message restarts it "
                                  "and picks the conversation back up."
                                  + (f"\nCodex said:\n{tail}" if tail else "")))
            self._finish_turn({"status": "failed"}, quiet=True)
        self._approvals.clear()

    def _watchdog(self):
        self._periodic()
        t = self._turn
        if t is None and self._compact is None:
            return
        if self._approvals:
            return                              # waiting on the user is not a dead transport
        last = (t or self._compact).get("last", time.monotonic())
        budget = (COMPACT_IDLE_TIMEOUT if self._compact is not None
                  else TOOL_IDLE_TIMEOUT if (t or {}).get("tools") else RECV_IDLE_TIMEOUT)
        if time.monotonic() - last <= budget:
            return
        self.ui.put(("error", "Codex stopped responding — restarting it (the conversation "
                              "is kept)."))
        rpc, self._rpc = self._rpc, None
        self._gen += 1                          # its leftovers, if any, are now stale
        if rpc is not None:
            rpc.close()
        if self._thread_id:
            self._resume_id = self._thread_id
        self._finish_turn({"status": "failed"}, quiet=True)

    # ── requests ──
    def _start_turn(self, text, image_paths):
        with self._turn_lock:
            self._asking, self._cancel = True, False
        try:
            self._start_turn_inner(text, image_paths)
        finally:
            with self._turn_lock:
                self._asking = False

    def _cancelled_early(self):
        with self._turn_lock:
            hit, self._cancel = self._cancel, False
        if hit:
            self.ui.put(("system", "⏹ stopped."))
            self.ui.put(("turn_done", None))
        return hit

    def _start_turn_inner(self, text, image_paths):
        try:
            ok = self._ensure_thread()
        except Exception as e:
            ok = False
            self.ui.put(("system", "⚠ Couldn't pick the previous Codex conversation back up "
                                   f"({type(e).__name__}) — starting fresh."))
            self._thread_id = None
            self.ui.put(("session_replaced", None))
            try:
                ok = self._ensure_thread()
            except Exception:
                ok = False
        if not ok:
            self.ui.put(("turn_done", None))
            return
        if self._ws_probing and self._wants_workspace_write():
            self.ui.put(("status", "checking Codex's sandbox…"))
            self._drain_until(lambda: not self._ws_probing, _PROBE_TIMEOUT + 5)
            self.ui.put(("status", "thinking…"))
        if self._effort == "auto" and not self.models:
            # Auto resolves through the model list, which now loads in the background; it is
            # one fast local read, so give it a moment rather than fall back to config.toml's
            # (often slow, xhigh) level for this first turn.
            self._drain_until(lambda: bool(self.models) or "models" not in self._bg_busy, 5)
        items = self._build_input(text, image_paths)
        if self._cancelled_early():
            return
        params = {"threadId": self._thread_id, "input": items, "summary": "auto",
                  "sandboxPolicy": _SANDBOX_POLICY[self._sandbox_mode()],
                  "approvalPolicy": self._approval_policy()}
        if self._model:
            params["model"] = self._model
        effort = self._effort
        if effort == "auto":
            # Codex has no per-request "model decides" level; the closest thing is the level
            # the model ships with — what a fresh Codex install would use for it.
            cur = self._model or self._served_model
            effort = next((m.get("default_effort") for m in self.models if m["id"] == cur), None)
        if effort:
            params["effort"] = effort
        with self._turn_lock:
            self._turn = {"id": None, "last": time.monotonic(), "tools": 0, "texted": False,
                          "streamed": set(), "think": False, "err": None, "files": {}}
        try:
            res = self._rpc.request("turn/start", params, timeout=QUERY_TIMEOUT)
        except Exception as e:
            self.ui.put(("error", self._error_text(e)))
            self._finish_turn({"status": "failed"}, quiet=True)
            return
        tid = ((res or {}).get("turn") or {}).get("id")
        with self._turn_lock:
            if self._turn is not None and not self._turn["id"]:
                self._turn["id"] = tid
            late_stop, self._cancel = self._cancel, False
        if late_stop and tid:
            self._rpc.request_nowait("turn/interrupt", {"threadId": self._thread_id, "turnId": tid})

    @staticmethod
    def _error_text(e):
        if isinstance(e, RpcError):
            msg = e.error.get("message") or str(e)
            if "auth" in msg.lower() or "login" in msg.lower():
                return f"Codex can't authenticate: {msg}\nSign in again in a terminal:  codex login"
            return f"Codex refused the request: {msg}"
        return f"{type(e).__name__}: {e}"

    def _build_input(self, text, image_paths):
        """The turn's UserInput items: the text, then one item per attachment. Images go by
        PATH (localImage) — app-server reads and encodes them itself, so nothing here holds
        a 4MB base64 string. Word/PowerPoint are extracted to text exactly the way the
        Claude worker does it; a PDF is handed over by path, since Codex reads files
        itself and the Responses API has no inline document block to put it in."""
        items = [{"type": "text", "text": text}] if text else []
        why, seen, n, total, doc_chars = [], set(), 0, 0, 0
        for p in image_paths:
            if p in seen:
                continue
            seen.add(p)
            name = Path(p).name
            if n >= MAX_INLINE_IMAGES:
                why.append(f"{name} — over the {MAX_INLINE_IMAGES}-attachment limit for one message")
                continue
            ext = Path(p).suffix.lower()
            try:
                size = Path(p).stat().st_size
            except Exception as e:
                why.append(f"{name} — {_stat_reason(e)}")
                continue
            if ext in IMAGE_EXTS:
                if size > MAX_INLINE_IMAGE_BYTES:
                    why.append(f"{name} — {size / 1048576:.0f}MB, over the "
                               f"{MAX_INLINE_IMAGE_BYTES // 1048576}MB limit for one image")
                    continue
                if total + size > MAX_INLINE_TOTAL_BYTES:
                    why.append(f"{name} — no room left in this message "
                               f"({MAX_INLINE_TOTAL_BYTES // 1048576}MB of attachments total)")
                    continue
                total += size
                items.append({"type": "localImage", "path": str(p)})
            elif ext == ".pdf":
                # Claude takes a PDF natively; the Responses API Codex uses has no inline
                # document block. So the text comes along extracted (pypdf, the same caps as
                # Word/PowerPoint), with the path for anything the text can't carry — a chart,
                # a scanned page — which Codex can open itself.
                text, chars = _pdf_text(p, doc_chars)
                doc_chars += chars
                note = (f"[Attached PDF: {p}]" + (f"\n{text}" if text else
                        " — its text couldn't be extracted here; open it from disk if you "
                        "need what it says."))
                items.append({"type": "text", "text": note})
            elif ext in (".docx", ".pptx"):
                block, chars, reason = _doc_text(p, ext, doc_chars)
                if block is None:
                    why.append(f"{name} — {reason}")
                    continue
                doc_chars += chars
                items.append({"type": "text", "text": block})
            else:
                why.append(f"{name} — {ext or 'no extension'} isn't a supported type")
                continue
            n += 1
        if why:
            self.ui.put(("error", "Not sent:\n  " + "\n  ".join(why[:6])
                                  + (f"\n  …and {len(why) - 6} more" if len(why) > 6 else "")))
        if not items:
            items = [{"type": "text", "text": text or ""}]
        return items

    def _do_reset(self):
        self._stop_turn_and_wait()
        self._thread_id = None
        self._resume_id = None
        self._usage = None
        self._ctx_tokens = None
        ok = False
        try:
            ok = self._ensure_server()
            if ok:
                res = self._rpc.request("thread/start", self._thread_params(),
                                        timeout=_INIT_TIMEOUT)
        except Exception as e:
            ok = False
            self.ui.put(("error", f"Codex couldn't open a new conversation: {type(e).__name__}: {e}"))
        # reset_done BEFORE the new thread's ("session", id): the UI ignores session ids
        # until it has seen reset_done (they could belong to the discarded conversation).
        self.ui.put(("reset_done", None))
        if ok:
            self._adopt_thread(res or {})

    def _stop_turn_and_wait(self):
        if self._turn is None and self._compact is None:
            return
        self.interrupt()
        if not self._drain_until(lambda: self._turn is None and self._compact is None, 8):
            self._finish_turn({"status": "interrupted"}, quiet=True)

    def _do_compact(self):
        if self._turn is not None:
            self._deferred.append(("compact", None))
            self._park_until_idle()
            return
        try:
            ok = self._ensure_thread()
        except Exception:
            ok = False
        if not ok or not self._thread_id:
            self.ui.put(("compact_done", {"status": "error", "meta": None,
                                          "detail": "not connected (check `codex --version`)"}))
            return
        self.ui.put(("compacting", {"pre_tokens": self._ctx_tokens}))
        self._compact = {"id": None, "pre": self._ctx_tokens, "t0": time.monotonic(),
                         "last": time.monotonic(), "done": False}
        try:
            self._rpc.request("thread/compact/start", {"threadId": self._thread_id})
        except Exception as e:
            self._compact = None
            self.ui.put(("compact_done", {"status": "error", "meta": None,
                                          "detail": f"{type(e).__name__}: {e}"}))

    def _do_resume(self, sid):
        self._stop_turn_and_wait()
        prev = self._thread_id
        self._resume_id = sid
        try:
            ok = self._ensure_thread()
        except Exception as e:
            dbg("codex_resume", f"{type(e).__name__}: {e}")
            ok = False
        if ok and self._thread_id == sid:
            self.ui.put(("resumed", None))
        else:
            self._thread_id = prev
            self.ui.put(("resume_failed", None))
        self.ui.put(("status", ""))

    # ── server traffic ──
    def _on_server(self, msg):
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if "id" in msg:
            self._on_server_request(msg["id"], method, params)
            return
        t = self._turn
        if t is not None:
            t["last"] = time.monotonic()
        if self._compact is not None:
            self._compact["last"] = time.monotonic()
        tid = params.get("threadId")
        if tid and self._thread_id and tid != self._thread_id and method != "account/rateLimits/updated":
            return                              # a sub-agent's thread: its own business
        if method == "item/agentMessage/delta":
            self._text(t, params.get("itemId"), params.get("delta") or "")
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            self._think(t, params.get("delta") or "")
        elif method == "item/reasoning/summaryPartAdded":
            if t is not None and t.get("think"):
                t["star"] = ""
                self.ui.put(("think", "\n\n"))
        elif method == "item/started":
            self._item_started(t, params.get("item") or {})
        elif method == "item/completed":
            self._item_completed(t, params.get("item") or {})
        elif method == "turn/started":
            with self._turn_lock:
                cur = self._turn if self._turn is not None else self._compact
                if cur is not None and not cur.get("id"):
                    cur["id"] = (params.get("turn") or {}).get("id")
        elif method == "turn/completed":
            self._finish_turn(params.get("turn") or {})
        elif method == "thread/tokenUsage/updated":
            self._usage = params.get("tokenUsage") or None
            if self._turn is None and self._compact is None:
                self._emit_usage()
        elif method == "account/rateLimits/updated":
            self._merge_rate_limits(params.get("rateLimits"))
        elif method in ("account/updated", "account/login/completed"):
            self._next_auth_poll = 0.0             # re-read on the next idle tick
        elif method == "error":
            err = params.get("error") or {}
            text = err.get("message") or "unknown error"
            if params.get("willRetry"):
                self.ui.put(("status", f"retrying… ({text[:60]})"))
            elif t is not None:
                t["err"] = err
            else:
                self.ui.put(("error", f"Codex: {text}"))
        elif method == "model/rerouted":
            to = params.get("toModel")
            if to:
                self._served_model = to
                self.ui.put(("model", to))
                self.ui.put(("system", f"↪ Codex routed this turn to {to} "
                                       f"({params.get('reason') or 'by the server'})."))
        elif method == "serverRequest/resolved":
            rid = params.get("requestId")
            if self._approvals.pop(rid, None) is not None:
                self.ui.put(("approval_resolved", rid))
        elif method in ("item/autoApprovalReview/started",):
            self.ui.put(("status", "auto-review…"))
        elif method in ("item/autoApprovalReview/completed",):
            self.ui.put(("status", "thinking…" if self._turn is not None else ""))
        elif method in ("warning", "configWarning", "deprecationNotice", "guardianWarning"):
            dbg("codex_" + method, str(params.get("message") or params.get("summary"))[:300])

    def _think(self, t, delta):
        """A reasoning-summary delta, minus its Markdown bold. Codex titles each summary part
        `**Like this**`, and the thinking block is plain text, so the markers would show as
        literal asterisks. A `**` can arrive split across two deltas, so a trailing lone `*`
        waits for the next one."""
        carry = (t or {}).get("star") or ""
        d = carry + (delta or "")
        hold = ""
        if d.endswith("*") and not d.endswith("**"):
            d, hold = d[:-1], "*"
        d = d.replace("**", "")
        if t is not None:
            t["star"] = hold
            if d:
                t["think"] = True
        if d:
            self.ui.put(("think", d))

    def _text(self, t, item_id, delta):
        if not delta:
            return
        if t is not None:
            if item_id and item_id not in t["streamed"]:
                if t["texted"] and not t.get("chip"):
                    self.ui.put(("delta", "\n\n"))   # a new message in the same turn — unless
                                                     # a tool line already sits between them
                t["streamed"].add(item_id)
            t["texted"] = True
            t["chip"] = False
        self.ui.put(("delta", delta))

    @staticmethod
    def _command_tool(item):
        """A commandExecution as the tool line the chat draws. Codex runs everything through a
        shell wrapper ("C:\\…\\powershell.exe" -Command '…'), which is all the raw `command`
        shows; its commandActions carry the parsed intent, so a file read reads as ▤ Read
        name.py and a search as ⌕, exactly like the Claude side's own tools."""
        acts = [a for a in (item.get("commandActions") or []) if isinstance(a, dict)]
        if len(acts) == 1:
            a = acts[0]
            if a.get("type") == "read" and a.get("path"):
                return ("Read", {"file_path": str(a["path"])})
            if a.get("type") == "search":
                return ("Grep", {"pattern": a.get("query") or a.get("command") or ""})
            if a.get("type") == "listFiles":
                return ("Glob", {"pattern": a.get("path") or a.get("command") or ""})
        cmd = " && ".join(str(a["command"]) for a in acts if a.get("command"))
        return ("Bash", {"command": cmd or _unwrap_shell(item.get("command") or "")})

    def _item_started(self, t, item):
        typ = item.get("type")
        tool = None
        if typ == "commandExecution":
            tool = self._command_tool(item)
        elif typ == "fileChange":
            paths = [c.get("path") for c in (item.get("changes") or [])
                     if isinstance(c, dict) and c.get("path")]
            if t is not None:
                t["files"][item.get("id")] = paths
            tool = ("Edit", {"file_path": paths[0]} if paths else {})
        elif typ == "mcpToolCall":
            args = item.get("arguments")
            tool = (f"{item.get('server')}.{item.get('tool')}",
                    args if isinstance(args, dict) else {})
        elif typ == "dynamicToolCall":
            args = item.get("arguments")
            tool = (str(item.get("tool") or "tool"), args if isinstance(args, dict) else {})
        elif typ == "webSearch":
            tool = ("WebSearch", {"query": item.get("query") or ""})
        elif typ == "imageView":
            tool = ("Read", {"file_path": item.get("path") or ""})
        elif typ == "collabAgentToolCall":
            tool = ("Task", {"prompt": item.get("prompt") or ""})
        elif typ == "imageGeneration":
            tool = ("ImageGen", {"prompt": item.get("revisedPrompt") or ""})
        if tool is not None:
            if t is not None:
                t["chip"] = True
                if typ in ("commandExecution", "fileChange", "mcpToolCall",
                           "dynamicToolCall", "collabAgentToolCall"):
                    t["tools"] += 1
            self.ui.put(("tool", tool))

    def _item_completed(self, t, item):
        typ = item.get("type")
        if typ == "agentMessage" and t is not None and item.get("id") not in t["streamed"]:
            self._text(t, item.get("id"), item.get("text") or "")   # never streamed: say it whole
        elif typ in ("commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
                     "collabAgentToolCall") and t is not None:
            t["tools"] = max(0, t["tools"] - 1)
        elif typ == "contextCompaction":
            if self._compact is not None:
                self._compact["done"] = True
            else:
                self.ui.put(("auto_compacted", None))

    def _finish_turn(self, turn, quiet=False):
        """End the in-flight turn (or compaction) with the events the UI keys on: a result
        (or a stop line), then turn_done, then the context reading that includes it."""
        status = turn.get("status")
        if self._compact is not None:
            c, self._compact = self._compact, None
            post = self._usage_tokens()
            ok = status == "completed" and c["done"]
            self.ui.put(("compact_done", {
                "status": ("ok" if ok else "cancelled" if status == "interrupted"
                           else "unconfirmed" if status == "completed" else "error"),
                "meta": {"pre_tokens": c["pre"], "post_tokens": post,
                         "duration_ms": int((time.monotonic() - c["t0"]) * 1000),
                         "trigger": "manual"},
                "detail": (None if ok else ((turn.get("error") or {}).get("message")
                                            or "no compaction item seen"))}))
            self._emit_usage()
            return
        t = self._turn
        if t is None:
            return
        with self._turn_lock:
            self._turn = None
        self._approvals.clear()
        if not quiet:
            if status == "interrupted":
                self.ui.put(("system", "⏹ stopped."))
            else:
                err = turn.get("error") or t.get("err")
                failed = status == "failed" or (status != "completed" and err)
                info = (err or {}).get("codexErrorInfo")
                sub = (next(iter(info)) if isinstance(info, dict) and info
                       else info if isinstance(info, str) else None)
                self.ui.put(("result", {
                    "cost": None, "is_error": bool(failed),
                    "subtype": (_codex_subtype(sub) if failed else "success"),
                    "result": (err or {}).get("message") if failed else None,
                    "stop_reason": None}))
        self.ui.put(("turn_done", None))
        self._emit_usage()

    def _usage_tokens(self):
        last = (self._usage or {}).get("last") or {}
        n = last.get("totalTokens")
        return int(n) if isinstance(n, (int, float)) and n >= 0 else None

    def _emit_usage(self):
        u = self._usage or {}
        n = self._usage_tokens()
        win = u.get("modelContextWindow")
        if n is None:
            return
        self._ctx_tokens = n
        self.ui.put(("ctx_tokens", n))
        if isinstance(win, (int, float)) and win > 0:
            self.ui.put(("ctx", min(100.0, 100.0 * n / win)))

    def _merge_rate_limits(self, snap, full=False):
        """Fold a rate-limit snapshot into what we know and hand the UI one reading per window,
        in the shape usage.py's poll already uses: {"windows": {name: {...}}}.

        `full` (an account/rateLimits/read answer) REPLACES what we know, so a cleared
        rejection really clears. A rolling account/rateLimits/updated is sparse, and the
        protocol says its nulls mean "not sent", not "cleared" — those merge. The periodic
        full read is what lifts a stale rejection within a minute."""
        if not isinstance(snap, dict):
            return
        if full:
            self._rl = {k: v for k, v in snap.items() if v is not None}
        else:
            for k, v in snap.items():
                if v is not None:
                    self._rl[k] = v
        wins = {}
        for key in ("primary", "secondary"):
            w = self._rl.get(key)
            if not isinstance(w, dict) or not isinstance(w.get("usedPercent"), (int, float)):
                continue
            mins = w.get("windowDurationMins")
            name = ("five_hour" if not isinstance(mins, (int, float)) and key == "primary"
                    else "five_hour" if isinstance(mins, (int, float)) and mins <= 24 * 60
                    else "seven_day")
            wins.setdefault(name, {"utilization": max(0.0, w["usedPercent"] / 100.0),
                                   "resets_at": w.get("resetsAt")})
        lim = self._rl.get("individualLimit")
        if isinstance(lim, dict) and isinstance(lim.get("remainingPercent"), (int, float)):
            wins["codex_limit"] = {"utilization": max(0.0, 1 - lim["remainingPercent"] / 100.0),
                                   "resets_at": lim.get("resetsAt")}
        status = ("rejected" if self._rl.get("rateLimitReachedType")
                  or self._rl.get("spendControlReached") else None)
        payload = {"windows": wins, "status": status}
        if payload != self._last_quota:        # app-server repeats it after every request
            self._last_quota = payload
            self.ui.put(("codex_quota", payload))
        # The same reading as the CLI-style status the UI's notice and auto-retry key on:
        # the binding window (the one furthest along), and a status Codex itself doesn't send.
        name, w = max(wins.items(), key=lambda kv: kv[1]["utilization"], default=(None, None))
        if w is None and status is None:
            return
        u = (w or {}).get("utilization") or 0.0
        st = ("rejected" if status == "rejected" or u >= 1.0
              else "allowed_warning" if u >= _QUOTA_WARN_AT else "allowed")
        reading = {"status": st, "utilization": u, "resets_at": (w or {}).get("resets_at"),
                   "window": name}
        if (reading["status"], reading["window"], round(u, 2)) != self._last_status:
            self._last_status = (reading["status"], reading["window"], round(u, 2))
            self.ui.put(("quota", reading))

    # ── server → client requests ──
    def _on_server_request(self, rid, method, params):
        rpc = self._rpc
        if rpc is None:
            return
        kind = _APPROVAL_METHODS.get(method)
        try:
            if kind:
                self._approvals[rid] = method
                if kind == "permissions":
                    self._asked_perms[rid] = params.get("permissions") or {}
                files = []
                if kind == "files" and self._turn is not None:
                    files = self._turn["files"].get(params.get("itemId")) or []
                cmd = params.get("command")
                if isinstance(cmd, list):
                    cmd = " ".join(str(c) for c in cmd)
                self.ui.put(("approval", {
                    "rid": rid, "kind": kind, "command": cmd or "",
                    "cwd": params.get("cwd") or "", "reason": params.get("reason") or "",
                    "files": [f for f in files if f], "grant_root": params.get("grantRoot"),
                    "permissions": _describe_perms(params.get("permissions"))}))
                return
            if method == "item/tool/requestUserInput":
                rpc.respond(rid, {"answers": {}})
                self.ui.put(("system", "❓ Codex tried to open a form the overlay can't show — "
                                       "it will ask in the chat instead."))
            elif method == "mcpServer/elicitation/request":
                rpc.respond(rid, {"action": "decline"})
            elif method == "item/tool/call":
                rpc.respond(rid, {"contentItems": [], "success": False})
            else:
                rpc.respond(rid, error={"code": -32601,
                                        "message": f"{CLIENT_NAME} does not handle {method}"})
        except Exception as e:
            dbg("codex_server_request", f"{method}: {type(e).__name__}: {e}")

    def _answer_approval(self, rid, decision):
        """decision ∈ {"accept", "session", "decline"} → the method's own vocabulary."""
        method = self._approvals.pop(rid, None)
        rpc = self._rpc
        if method is None or rpc is None:
            return
        if method in ("execCommandApproval", "applyPatchApproval"):
            res = {"decision": {"accept": "approved", "session": "approved_for_session"}
                   .get(decision, "denied")}
        elif method == "item/permissions/requestApproval":
            asked = self._asked_perms.pop(rid, {})
            res = {"permissions": {} if decision == "decline" else
                   {k: asked.get(k) for k in ("fileSystem", "network") if asked.get(k)},
                   "scope": "session" if decision == "session" else "turn"}
        else:
            res = {"decision": {"accept": "accept", "session": "acceptForSession"}
                   .get(decision, "decline")}
        try:
            rpc.respond(rid, res)
        except Exception as e:
            self.ui.put(("error", f"Couldn't answer Codex: {type(e).__name__}: {e}"))


def _codex_subtype(info):
    """Codex's error class as the "subtype" the UI already explains ("rate_limit…" puts the
    message back in the box and offers a retry, exactly like a refused Claude turn)."""
    s = str(info or "error")
    low = s.lower()
    if "usagelimit" in low or "ratelimit" in low or "quota" in low:
        return "rate_limit_error"
    if "unauthorized" in low or "auth" in low:
        return "authentication_error"
    return s


def _stat_reason(exc):
    if isinstance(exc, FileNotFoundError):
        return "is no longer there (moved or deleted since you picked it)"
    if isinstance(exc, PermissionError):
        return "couldn't be opened (permission denied, or another program has it locked)"
    return f"couldn't be read ({exc.__class__.__name__})"


def _doc_text(p, ext, already):
    """A .docx/.pptx as one text input, through the Claude worker's own extractor (same
    caps, same truncation note, same wording on failure). Imported here, not at module
    load: worker.py pulls in claude_agent_sdk, which a Codex-only turn otherwise never needs."""
    try:
        from worker import ClaudeWorker
    except Exception as e:
        return None, 0, f"couldn't be parsed ({e.__class__.__name__})"
    block, chars, reason = ClaudeWorker._doc_text_block(ClaudeWorker, p, ext, already)
    if block is None:
        return None, 0, reason
    return block.get("text") or "", chars, None


def _load_probe():
    try:
        d = json.loads(_PROBE_FILE.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_probe(d):
    try:
        _PROBE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROBE_FILE.write_text(json.dumps(d), "utf-8")
    except Exception:
        pass


_SHELL_WRAP = re.compile(
    r"""^\s*"?[^"]*?\b(?:powershell|pwsh|cmd)(?:\.exe)?"?\s+"""
    r"""(?:-NoLogo\s+|-NoProfile\s+|-NonInteractive\s+|/d\s+)*(?:-Command|/c)\s+(.+?)\s*$""",
    re.I | re.S)


def _unwrap_shell(cmd):
    r"""`"C:\…\powershell.exe" -Command 'Get-Process'` → `Get-Process`: the fallback when a
    command item carries no parsed commandActions."""
    m = _SHELL_WRAP.match(cmd or "")
    if not m:
        return cmd
    inner = m.group(1)
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        inner = inner[1:-1]
    return inner


def _toml(v):
    """A JSON-ish value as inline TOML, for -c overrides."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_toml_key(k)} = {_toml(x)}" for k, x in v.items()) + "}"
    return json.dumps(str(v))              # a JSON string literal is a valid TOML basic string


def _toml_key(k):
    k = str(k)
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def _codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _user_mcp_names():
    """The MCP servers ~/.codex/config.toml declares — names only; nothing else is read."""
    try:
        text = (_codex_home() / "config.toml").read_text("utf-8")
    except Exception:
        return []
    try:
        import tomllib                     # 3.11+
    except ImportError:                    # 3.10: the table headers are all that's needed
        names = []
        for m in re.finditer(r'(?m)^\s*\[\s*mcp_servers\.("(?:[^"\\]|\\.)*"|[A-Za-z0-9_-]+)\s*\]', text):
            n = m.group(1)
            n = json.loads(n) if n.startswith('"') else n
            if n not in names:
                names.append(n)
        return names
    try:
        servers = tomllib.loads(text).get("mcp_servers")
        return list(servers) if isinstance(servers, dict) else []
    except Exception:
        return []


def _mcp_flags():
    """-c overrides that give Codex the overlay's MCP setup, exactly as the Claude side gets it.

    STRICT_MCP_CONFIG (lean, the default): the servers the user configured for Codex itself
    stay off — each one by name, since app-server MERGES `-c mcp_servers={}` instead of
    replacing the table (measured 2026-10-28). Codex's own built-ins aren't in that table
    and stay, like Claude Code's core tools. MCP_SERVERS (declared FOR the overlay) are added
    in Codex's spelling — http → url (+ headers), stdio → command/args/env — under either
    setting. Remote servers need their own sign-in on the Codex side: `codex mcp login <name>`."""
    flags = []
    mine = MCP_SERVERS if isinstance(MCP_SERVERS, dict) else {}
    if STRICT_MCP_CONFIG:
        for name in _user_mcp_names():
            if name not in mine:
                flags += ["-c", f"mcp_servers.{_toml_key(name)}.enabled=false"]
    for name, spec in mine.items():
        if not isinstance(spec, dict):
            continue
        out = {}
        if spec.get("url"):
            out["url"] = spec["url"]
            if isinstance(spec.get("headers"), dict):
                out["http_headers"] = spec["headers"]
        elif spec.get("command"):
            out["command"] = spec["command"]
            if isinstance(spec.get("args"), list):
                out["args"] = spec["args"]
            if isinstance(spec.get("env"), dict):
                out["env"] = spec["env"]
        if out:
            flags += ["-c", f"mcp_servers.{_toml_key(name)}={_toml(out)}"]
    return flags


def _session_rows(threads):
    """thread/list entries as plain rows for the UI's past-conversation cards."""
    rows = []
    for th in threads:
        if not isinstance(th, dict) or not th.get("id") or th.get("ephemeral"):
            continue
        title = (th.get("name") or th.get("preview") or "").strip().replace("\n", " ")
        ts = th.get("updatedAt") or th.get("recencyAt") or th.get("createdAt") or 0
        rows.append({"id": str(th["id"]), "title": title[:120] or "(untitled)",
                     "subtitle": str(th.get("model") or ""),
                     "mtime": float(ts) if isinstance(ts, (int, float)) else 0.0})
    return rows


def _pdf_text(p, already):
    """(text, chars charged) for a PDF, within the same per-file / per-message budgets as
    Word and PowerPoint; ("", 0) if pypdf isn't installed or the file has no text layer."""
    room = min(MAX_INLINE_DOC_CHARS, max(0, MAX_INLINE_DOC_TOTAL_CHARS - already))
    if room <= 0:
        return "", 0
    try:
        from pypdf import PdfReader      # optional, from requirements-docs.txt
        pages = []
        total = 0
        for i, page in enumerate(PdfReader(p).pages, 1):
            t = (page.extract_text() or "").strip()
            if t:
                pages.append(f"--- Page {i} ---\n{t}")
                total += len(t)
            if total > room:
                break
        text = "\n\n".join(pages)
    except Exception:
        return "", 0
    if not text.strip():
        return "", 0
    if len(text) > room:
        text = text[:room] + f"\n[... truncated at {room} characters — this file is longer]"
    return text, min(len(text), room)


def _describe_perms(perms):
    """A permissions request as one line for the approval card."""
    if not isinstance(perms, dict):
        return ""
    bits = []
    fs = perms.get("fileSystem")
    if isinstance(fs, dict):
        for key in ("write", "writableRoots", "read", "readableRoots"):
            v = fs.get(key)
            if v:
                bits.append(f"{key}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
    elif fs:
        bits.append(f"files: {fs}")
    net = perms.get("network")
    if net:
        bits.append("network access" if net is True else f"network: {net}")
    return "; ".join(bits)
