# -*- coding: utf-8 -*-
"""Opt-in activity logging for the worker, and the queue tap that mirrors every
worker->UI event into the log. Enabled by the CLAUDE_OVERLAY_DEBUG_LOG env var;
inert (no writes) when unset. Leaf module - imports only the stdlib."""

import os
import time
import threading

# ── debug / activity log (monitoring) ──────────────────────────────────────
# Under pythonw the overlay has no console and exposes no IPC, so its work can't be
# watched from outside. Opt in by setting the CLAUDE_OVERLAY_DEBUG_LOG environment
# variable to a file path: you then get a timestamped, one-line-per-event trace of the
# worker (turn start, tool calls, results, errors, reconnects, a throttled streaming
# heartbeat) — enough to see what it's doing and whether a turn is stuck. Default is OFF
# (empty) so nothing is written. Privacy note: even when enabled, reply text is NEVER
# logged (deltas/thinking are logged only as a ~2s heartbeat + char count), but a
# turn-start prompt preview (≤120 chars) IS written — so only enable it on a trusted
# machine. Each PID tags its own lines so several overlays don't get confused.
DEBUG_LOG = os.environ.get("CLAUDE_OVERLAY_DEBUG_LOG", "")
DEBUG_LOG_MAX_BYTES = 2_000_000     # truncate (best-effort) once the log grows past this
_dbg_lock = threading.Lock()
_dbg_stream_last = [0.0]             # throttle high-frequency streaming deltas to a heartbeat
_dbg_think_last = [0.0]             # ditto for thinking deltas (separate, so a thinking heartbeat
                                    # can't suppress the first answer delta we use to time TTFT)

def dbg(kind, payload=None):
    """Append one best-effort line to DEBUG_LOG. Never raises into the caller."""
    if not DEBUG_LOG:
        return
    try:
        if kind in ("delta", "think"):   # streaming token text → heartbeat only, NEVER the content
            now = time.monotonic()       # (thinking is reply content too — must not hit disk verbatim)
            last = _dbg_stream_last if kind == "delta" else _dbg_think_last
            if now - last[0] < 2.0:
                return
            last[0] = now
            n = len(payload) if isinstance(payload, str) else 0
            payload = f"<{'streaming' if kind == 'delta' else 'thinking'} +{n} chars>"
        elif kind == "tool":        # (name, input_dict) → name + a short arg preview
            name, inp = payload if isinstance(payload, tuple) and len(payload) == 2 else (payload, None)
            arg = ""
            if isinstance(inp, dict):
                arg = " ".join(f"{k}={str(v)[:40]}" for k, v in list(inp.items())[:2])
            payload = f"{name} {arg}".strip()
        elif isinstance(payload, dict):
            payload = " ".join(f"{k}={v}" for k, v in payload.items())
        elif isinstance(payload, str):
            payload = payload[:200].replace("\n", " ")
        _now = time.time()
        _ts = time.strftime('%H:%M:%S', time.localtime(_now)) + f".{int((_now % 1) * 1000):03d}"
        line = f"{_ts} pid={os.getpid()} {kind} {payload if payload is not None else ''}".rstrip() + "\n"
        with _dbg_lock:
            try:
                if os.path.exists(DEBUG_LOG) and os.path.getsize(DEBUG_LOG) > DEBUG_LOG_MAX_BYTES:
                    open(DEBUG_LOG, "w", encoding="utf-8").close()
            except Exception:
                pass
            with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass

class _UIQueueTap:
    """Wraps the worker→UI queue so every event the worker emits is also written to the
    debug log. The worker's entire observable behavior already flows through this one
    queue (`ready`/`tool`/`result`/`error`/`system`/`turn_done`/`delta`/…), so a single
    tap here logs all of it without scattering calls through the worker. The UI keeps
    reading the underlying queue directly; only the worker's `put` side is wrapped."""
    def __init__(self, q):
        self._q = q

    def put(self, item, *a, **k):
        try:
            if isinstance(item, tuple) and len(item) == 2:
                dbg(item[0], item[1])
            else:
                dbg(item)
        except Exception:
            pass
        return self._q.put(item, *a, **k)

    def __getattr__(self, name):
        return getattr(self._q, name)
