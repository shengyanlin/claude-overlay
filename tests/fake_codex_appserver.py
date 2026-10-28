"""A stand-in for `codex app-server`, for tests/test_codexworker.py.

Speaks the same newline-delimited JSON-RPC on stdio, with the message shapes recorded from
the real server (codex-cli 0.160.1, 2026-10-28). Started through CLAUDE_OVERLAY_CODEX_CMD,
so CodexWorker runs its real process/reader/inbox code against it. Every request it gets is
appended to $FAKE_CODEX_LOG as one JSON line, which is what the tests assert on.

Behaviour knobs (environment):
  FAKE_CODEX_POLICY  "managed" → allow only read-only/workspace-write + untrusted/on-request
                     (the policy measured on a managed laptop); anything else → no requirements
  FAKE_CODEX_WS      "ok" → the workspace-write probe succeeds; "hang" → it never answers
  FAKE_CODEX_SIGNED_OUT  "1" → account/read reports no login
  FAKE_CODEX_USED    the 5-hour window's used percent (default 40)
Turn behaviour is keyed off the prompt text:
  APPROVE → asks for command approval first; FAIL → the turn fails on a usage limit;
  SLOW → runs until turn/interrupt; DIE → the process exits mid-turn.
"""
import json
import os
import sys
import threading

LOG = os.environ.get("FAKE_CODEX_LOG")
_lock = threading.Lock()
_state = {"thread": 0, "turn": 0, "slow": None, "approval": None}


def log(entry):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def send(msg):
    with _lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def note(method, params):
    send({"method": method, "params": params})


def turn_events(tid, turn_id, status="completed", error=None, text=("Hel", "lo"), tool=True):
    note("turn/started", {"threadId": tid, "turn": {"id": turn_id, "items": [], "status": "inProgress"}})
    note("item/started", {"threadId": tid, "turnId": turn_id,
                          "item": {"type": "agentMessage", "id": "m1", "text": ""}})
    for d in text:
        note("item/agentMessage/delta", {"threadId": tid, "turnId": turn_id, "itemId": "m1", "delta": d})
    note("item/completed", {"threadId": tid, "turnId": turn_id,
                            "item": {"type": "agentMessage", "id": "m1", "text": "".join(text)}})
    if tool:
        note("item/started", {"threadId": tid, "turnId": turn_id,
                              "item": {"type": "commandExecution", "id": "c1", "command": "echo hi",
                                       "status": "inProgress"}})
        note("item/completed", {"threadId": tid, "turnId": turn_id,
                                "item": {"type": "commandExecution", "id": "c1", "command": "echo hi",
                                         "status": "completed", "exitCode": 0}})
    note("thread/tokenUsage/updated", {"threadId": tid, "turnId": turn_id, "tokenUsage": {
        "total": {"totalTokens": 1000}, "last": {"totalTokens": 1000}, "modelContextWindow": 10000}})
    finish(tid, turn_id, status, error)


def finish(tid, turn_id, status, error=None):
    note("turn/completed", {"threadId": tid, "turn": {"id": turn_id, "items": [], "status": status,
                                                      "error": error}})


def handle(msg):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None:                         # a response to one of OUR requests
        log({"response": msg})
        ap = _state["approval"]
        if ap and msg.get("id") == ap[0]:
            _state["approval"] = None
            turn_events(*ap[1:])
        return
    log({"method": method, "params": params})
    if rid is None:
        return                                 # a notification (initialized)
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "fake_client/9.9.9 (Windows; x86_64)"}})
    elif method == "configRequirements/read":
        req = ({"allowedSandboxModes": ["read-only", "workspace-write"],
                "allowedApprovalPolicies": ["untrusted", "on-request"]}
               if os.environ.get("FAKE_CODEX_POLICY") == "managed" else {})
        send({"id": rid, "result": {"requirements": req}})
    elif method == "command/exec":
        if os.environ.get("FAKE_CODEX_WS", "ok") == "ok":
            send({"id": rid, "result": {"exitCode": 0, "stdout": "overlay-sandbox-ok\r\n", "stderr": ""}})
        # "hang": never answer, like the real broken sandbox
    elif method == "model/list":
        send({"id": rid, "result": {"data": [
            {"id": "gpt-x", "displayName": "GPT-X", "isDefault": True, "hidden": False,
             "defaultReasoningEffort": "medium",
             "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
            {"id": "gpt-y", "displayName": "GPT-Y", "isDefault": False, "hidden": False,
             "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "ultra"}]},
            {"id": "gpt-hidden", "displayName": "Hidden", "hidden": True}]}})
    elif method == "account/read":
        out = os.environ.get("FAKE_CODEX_SIGNED_OUT") == "1"
        send({"id": rid, "result": {"account": None if out else {"type": "chatgpt"},
                                    "requiresOpenaiAuth": True}})
    elif method == "thread/list":
        send({"id": rid, "result": {"data": [
            {"id": "th-a", "name": None, "preview": "Summarise the deck", "updatedAt": 1790000000,
             "model": "gpt-x", "ephemeral": False},
            {"id": "th-b", "name": "Excel macro", "preview": "x", "updatedAt": 1790000500,
             "model": "gpt-y", "ephemeral": False},
            {"id": "th-c", "preview": "throwaway", "ephemeral": True}]}})
    elif method == "thread/archive":
        send({"id": rid, "result": {}})
    elif method == "account/rateLimits/read":
        used = int(os.environ.get("FAKE_CODEX_USED", "40"))
        send({"id": rid, "result": {"rateLimits": {
            "primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": 1800000000},
            "secondary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": 1800500000},
            "individualLimit": {"remainingPercent": 72, "resetsAt": 1800900000}}}})
    elif method in ("thread/start", "thread/resume"):
        if method == "thread/resume" and params.get("threadId") == "th-gone":
            send({"id": rid, "error": {"code": -32600, "message": "no rollout found for thread"}})
            return
        if method == "thread/start":
            _state["thread"] += 1
        tid = params.get("threadId") or f"th-{_state['thread']}"
        mode = {"read-only": "readOnly", "workspace-write": "workspaceWrite",
                "danger-full-access": "dangerFullAccess"}.get(params.get("sandbox"), "readOnly")
        send({"id": rid, "result": {"thread": {"id": tid}, "model": "gpt-x",
                                    "sandbox": {"type": mode}}})
    elif method == "turn/start":
        _state["turn"] += 1
        turn_id = f"tu-{_state['turn']}"
        tid = params.get("threadId")
        send({"id": rid, "result": {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
        text = " ".join(i.get("text", "") for i in params.get("input") or [] if i.get("type") == "text")
        if "DIE" in text:
            note("turn/started", {"threadId": tid, "turn": {"id": turn_id}})
            sys.stdout.flush()
            os._exit(3)
        if "APPROVE" in text:
            note("turn/started", {"threadId": tid, "turn": {"id": turn_id}})
            _state["approval"] = (900, tid, turn_id)
            send({"id": 900, "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": tid, "turnId": turn_id, "itemId": "c9", "startedAtMs": 0,
                             "command": "Remove-Item build", "cwd": "C:\\work", "reason": "needs write"}})
        elif "PERMS" in text:
            note("turn/started", {"threadId": tid, "turn": {"id": turn_id}})
            _state["approval"] = (901, tid, turn_id)
            send({"id": 901, "method": "item/permissions/requestApproval",
                  "params": {"threadId": tid, "turnId": turn_id, "itemId": "p1", "startedAtMs": 0,
                             "cwd": "C:\work", "reason": "needs the network",
                             "permissions": {"network": {"enabled": True}}}})
        elif "FAIL" in text:
            note("turn/started", {"threadId": tid, "turn": {"id": turn_id}})
            finish(tid, turn_id, "failed", {"message": "You've hit your usage limit.",
                                            "codexErrorInfo": "usageLimitExceeded"})
        elif "SLOW" in text:
            note("turn/started", {"threadId": tid, "turn": {"id": turn_id}})
            _state["slow"] = (tid, turn_id)
        else:
            turn_events(tid, turn_id)
    elif method == "turn/interrupt":
        send({"id": rid, "result": {}})
        if _state["slow"]:
            tid, turn_id = _state["slow"]
            _state["slow"] = None
            finish(tid, turn_id, "interrupted")
    elif method == "thread/compact/start":
        tid = params.get("threadId")
        send({"id": rid, "result": None})
        _state["turn"] += 1
        turn_id = f"tu-{_state['turn']}"
        note("turn/started", {"threadId": tid, "turn": {"id": turn_id}})
        note("item/started", {"threadId": tid, "turnId": turn_id, "item": {"type": "contextCompaction", "id": "k1"}})
        note("item/completed", {"threadId": tid, "turnId": turn_id, "item": {"type": "contextCompaction", "id": "k1"}})
        note("thread/tokenUsage/updated", {"threadId": tid, "turnId": turn_id, "tokenUsage": {
            "total": {"totalTokens": 300}, "last": {"totalTokens": 300}, "modelContextWindow": 10000}})
        finish(tid, turn_id, "completed")
    else:
        send({"id": rid, "error": {"code": -32601, "message": f"fake does not do {method}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if line:
            handle(json.loads(line))


if __name__ == "__main__":
    main()
