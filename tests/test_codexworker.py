"""CodexWorker against a fake `codex app-server` (tests/fake_codex_appserver.py).

The worker runs for real — its own process, reader thread and inbox — and only the server
on the other end of the pipe is fake, so what is tested is the protocol handling: which
events the UI gets for a turn, what each request carries, and how the sandbox, approval,
failure, interrupt, compaction, resume and crash paths come out."""
import json
import os
import queue
import sys
import time

import pytest

import codexworker as cw

FAKE = os.path.join(os.path.dirname(__file__), "fake_codex_appserver.py")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("CLAUDE_OVERLAY_CODEX_CMD", json.dumps([sys.executable, FAKE]))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("FAKE_CODEX_POLICY", "open")
    monkeypatch.setenv("FAKE_CODEX_WS", "ok")
    monkeypatch.setattr(cw, "_PROBE_FILE", tmp_path / "probe.json")
    workers = []

    class H:
        def start(self, permission_mode="bypassPermissions", effort="low", sandbox=None):
            if sandbox:
                monkeypatch.setattr(cw, "CODEX_SANDBOX", sandbox)
            self.q = queue.Queue()
            self.w = cw.CodexWorker(self.q, permission_mode=permission_mode, effort=effort)
            workers.append(self.w)
            self.w.start()
            self.seen = []
            self.wait("ready")
            return self.w

        def wait(self, kind, timeout=15, pred=None):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    k, p = self.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                self.seen.append((k, p))
                if k == kind and (pred is None or pred(p)):
                    return p
            raise AssertionError(f"no {kind!r} within {timeout}s; saw {[k for k, _ in self.seen]}")

        def event(self, kind, timeout=15):
            """The first `kind` seen so far, or the next one to arrive — for the reads that
            now happen in the background and may land on either side of "ready"."""
            for k, p in self.seen:
                if k == kind:
                    return p
            return self.wait(kind, timeout)

        def kinds(self):
            return [k for k, _ in self.seen]

        def requests(self, method):
            if not log.exists():
                return []
            out = []
            for line in log.read_text("utf-8").splitlines():
                e = json.loads(line)
                if e.get("method") == method:
                    out.append(e["params"])
            return out

        def responses(self):
            if not log.exists():
                return []
            return [json.loads(l)["response"] for l in log.read_text("utf-8").splitlines()
                    if "response" in json.loads(l)]

    h = H()
    yield h
    for w in workers:
        w.shutdown()
        w.join(5)


def test_a_turn_streams_text_tool_result_then_usage(harness):
    harness.start()
    harness.w.ask("hello")
    harness.wait("turn_done")
    ctx = harness.wait("ctx")
    seen = harness.seen
    deltas = [p for k, p in seen if k == "delta"]
    assert "".join(deltas) == "Hello"
    assert ("tool", ("Bash", {"command": "echo hi"})) in seen
    result = next(p for k, p in seen if k == "result")
    assert result["is_error"] is False
    # the context reading that includes the turn comes AFTER turn_done (the UI samples there)
    kinds = harness.kinds()
    assert kinds.index("turn_done") < kinds.index("ctx")
    assert ctx == pytest.approx(10.0)
    turn = harness.requests("turn/start")[0]
    assert turn["input"] == [{"type": "text", "text": "hello"}]
    assert turn["effort"] == "low"
    assert turn["summary"] == "auto"


def test_models_and_quota_reach_the_ui(harness):
    harness.start()
    models = harness.event("codex_models")
    assert [m["id"] for m in models] == ["gpt-x", "gpt-y"]          # hidden ones dropped
    assert models[0]["efforts"] == ["low", "high"]
    quota = harness.event("codex_quota")
    w = quota["windows"]
    assert w["five_hour"]["utilization"] == pytest.approx(0.40)
    assert w["seven_day"]["utilization"] == pytest.approx(0.10)
    assert w["codex_limit"]["utilization"] == pytest.approx(0.28)
    assert ("model", "gpt-x") in harness.seen


def test_unmanaged_full_access_means_no_sandbox_and_no_prompts(harness):
    harness.start(sandbox="danger-full-access")
    harness.w.ask("hi")
    harness.wait("turn_done")
    turn = harness.requests("turn/start")[0]
    assert turn["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert turn["approvalPolicy"] == "never"
    assert not harness.requests("command/exec")        # no sandbox, nothing to probe


def test_managed_policy_downgrades_and_says_so(harness, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_POLICY", "managed")
    harness.start(sandbox="danger-full-access")
    harness.w.ask("hi")
    harness.wait("turn_done")
    turn = harness.requests("turn/start")[0]
    assert turn["sandboxPolicy"] == {"type": "workspaceWrite"}
    assert turn["approvalPolicy"] == "on-request"      # "never" is not allowed by the policy
    notes = [p for k, p in harness.seen if k == "system"]
    assert any("doesn't allow danger-full-access" in n for n in notes)


def test_broken_workspace_sandbox_falls_back_to_read_only(harness, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_POLICY", "managed")
    monkeypatch.setenv("FAKE_CODEX_WS", "hang")
    monkeypatch.setattr(cw, "_PROBE_TIMEOUT", 1)
    harness.start(sandbox="danger-full-access")
    harness.w.ask("hi")                                # waits for the probe, then goes out
    harness.wait("turn_done", timeout=20)
    turn = harness.requests("turn/start")[0]
    assert turn["sandboxPolicy"] == {"type": "readOnly"}
    assert turn["approvalPolicy"] == "on-request"
    notes = [p for k, p in harness.seen if k == "system"]
    assert sum("Full access runs in read-only" in n for n in notes) == 1   # said once
    assert json.loads(cw._PROBE_FILE.read_text("utf-8")) == {"version": "9.9.9", "ok": False}


def test_probe_verdict_is_cached_per_cli_version(harness, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_POLICY", "managed")
    cw._PROBE_FILE.write_text(json.dumps({"version": "9.9.9", "ok": False}), "utf-8")
    harness.start(sandbox="workspace-write")
    harness.w.ask("hi")
    harness.wait("turn_done")
    assert not harness.requests("command/exec")
    assert harness.requests("turn/start")[0]["sandboxPolicy"] == {"type": "readOnly"}


def test_read_only_and_back(harness):
    harness.start(permission_mode="plan", sandbox="workspace-write")
    harness.w.ask("one")
    harness.wait("turn_done")
    harness.w.set_permission_mode("bypassPermissions")
    assert harness.wait("permission_mode") == "bypassPermissions"
    harness.w.ask("two")
    harness.wait("turn_done")
    first, second = harness.requests("turn/start")[:2]
    assert first["sandboxPolicy"] == {"type": "readOnly"}
    assert second["sandboxPolicy"] == {"type": "workspaceWrite"}
    sandbox_events = [p for k, p in harness.seen if k == "codex_sandbox"]
    assert sandbox_events[-1] == "workspace-write"


def test_model_and_effort_overrides_go_on_the_next_turn(harness):
    harness.start()
    harness.w.set_model("gpt-y")
    assert harness.wait("model") == "gpt-y"
    harness.w.set_effort("ultra")
    assert harness.wait("effort") == "ultra"
    harness.w.ask("hi")
    harness.wait("turn_done")
    turn = harness.requests("turn/start")[0]
    assert turn["model"] == "gpt-y" and turn["effort"] == "ultra"


def test_approval_is_asked_and_answered(harness):
    harness.start()
    harness.w.ask("APPROVE please")
    ask = harness.wait("approval")
    assert ask["rid"] == 900 and ask["kind"] == "command"
    assert ask["command"] == "Remove-Item build" and ask["reason"] == "needs write"
    harness.w.respond_approval(900, "accept")
    harness.wait("turn_done")
    resp = next(r for r in harness.responses() if r.get("id") == 900)
    assert resp["result"] == {"decision": "accept"}


@pytest.mark.parametrize("decision,wire", [("session", "acceptForSession"), ("decline", "decline")])
def test_approval_vocabulary(harness, decision, wire):
    harness.start()
    harness.w.ask("APPROVE")
    harness.wait("approval")
    harness.w.respond_approval(900, decision)
    harness.wait("turn_done")
    resp = next(r for r in harness.responses() if r.get("id") == 900)
    assert resp["result"] == {"decision": wire}


def test_usage_limit_failure_reads_as_a_rate_limit(harness):
    harness.start()
    harness.w.ask("FAIL")
    harness.wait("turn_done")
    result = next(p for k, p in harness.seen if k == "result")
    assert result["is_error"] is True
    assert result["subtype"] == "rate_limit_error"         # → the UI's give-it-back + retry path
    assert "usage limit" in result["result"]


def test_interrupt_stops_the_turn(harness):
    harness.start()
    harness.w.ask("SLOW")
    deadline = time.monotonic() + 10
    while harness.w._turn is None or not harness.w._turn.get("id"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    harness.w.interrupt()
    harness.wait("turn_done")
    assert ("system", "⏹ stopped.") in harness.seen
    assert harness.requests("turn/interrupt")[0]["turnId"] == "tu-1"


def test_compact(harness):
    harness.start()
    harness.w.ask("hi")
    harness.wait("turn_done")
    harness.wait("ctx")
    harness.w.compact()
    pre = harness.wait("compacting")
    assert pre == {"pre_tokens": 1000}
    done = harness.wait("compact_done")
    assert done["status"] == "ok"
    assert done["meta"]["pre_tokens"] == 1000 and done["meta"]["post_tokens"] == 300


def test_reset_confirms_before_the_new_session_id(harness):
    harness.start()
    harness.w.reset()
    harness.wait("session", pred=lambda p: p == "th-2")
    kinds = harness.kinds()
    assert kinds.index("reset_done") < len(kinds) - 1 - kinds[::-1].index("session")
    assert len(harness.requests("thread/start")) == 2


def test_resume(harness):
    harness.start()
    harness.w.resume("th-old")
    harness.wait("resumed")
    assert ("session", "th-old") in harness.seen
    assert harness.requests("thread/resume")[0]["threadId"] == "th-old"
    harness.w.resume("th-gone")
    harness.wait("resume_failed")


def test_server_death_mid_turn_is_reported_and_the_next_turn_resumes(harness):
    harness.start()
    harness.w.ask("DIE now")
    err = harness.wait("error")
    assert "stopped unexpectedly" in err
    harness.wait("turn_done")
    harness.w.ask("hello again")
    harness.wait("result")
    assert harness.requests("thread/resume")[-1]["threadId"] == "th-1"


def test_attachments_become_local_images_and_bad_files_are_named(harness, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    harness.start()
    harness.w.ask("look", [str(img), str(tmp_path / "gone.png")])
    err = harness.wait("error")
    assert "gone.png" in err and "no longer there" in err
    harness.wait("turn_done")
    items = harness.requests("turn/start")[0]["input"]
    assert items[0] == {"type": "text", "text": "look"}
    assert {"type": "localImage", "path": str(img)} in items


def test_no_codex_cli_says_how_to_install(monkeypatch):
    monkeypatch.setattr(cw, "codex_command", lambda: None)
    q = queue.Queue()
    w = cw.CodexWorker(q)
    w.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end:
            k, p = q.get(timeout=5)
            if k == "error":
                assert "npm install -g @openai/codex" in p and "codex login" in p
                break
        else:
            raise AssertionError("no error")
    finally:
        w.shutdown()
        w.join(5)


def test_npm_shim_resolves_to_the_native_binary(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    exe = (npm / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
           / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    (npm / "codex.cmd").write_text("@echo off\n")
    monkeypatch.delenv("CLAUDE_OVERLAY_CODEX_CMD", raising=False)
    monkeypatch.setattr(cw.shutil, "which", lambda name: str(npm / "codex.cmd"))
    monkeypatch.setattr(cw.platform, "machine", lambda: "AMD64")
    assert cw.codex_command() == [str(exe)]


def test_reasoning_bold_markers_are_stripped_even_when_split():
    """Codex titles each reasoning-summary part **like this**; the thinking block is plain
    text, so the markers must go — including a ** that arrives split over two deltas."""
    q = queue.Queue()
    w = cw.CodexWorker(q)                  # not started: _think needs no process
    t = {}
    for d in ("**Plan", "ning the summary*", "*\n\nRead the slide, then answer.", " 2 * 3"):
        w._think(t, d)
    out = ""
    while not q.empty():
        k, p = q.get_nowait()
        assert k == "think"
        out += p
    assert out == "Planning the summary\n\nRead the slide, then answer. 2 * 3"


def test_command_lines_show_the_real_command():
    """Codex wraps every command in a shell ("…powershell.exe" -Command '…'); the tool line
    should show what is being run, and a parsed file read should read like Claude's ▤ Read."""
    tool = cw.CodexWorker._command_tool
    ps = r'"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" -Command ' + "'echo hi'"
    assert tool({"command": ps}) == ("Bash", {"command": "echo hi"})
    assert tool({"command": "cmd.exe /d /c dir"}) == ("Bash", {"command": "dir"})
    assert tool({"command": "git status"}) == ("Bash", {"command": "git status"})
    assert tool({"command": ps, "commandActions": [
        {"type": "read", "command": "Get-Content a.py", "name": "a.py", "path": r"C:\w\a.py"}]}) \
        == ("Read", {"file_path": r"C:\w\a.py"})
    assert tool({"command": ps, "commandActions": [
        {"type": "search", "command": "rg foo", "query": "foo"}]}) == ("Grep", {"pattern": "foo"})
    assert tool({"command": ps, "commandActions": [
        {"type": "unknown", "command": "a"}, {"type": "unknown", "command": "b"}]}) \
        == ("Bash", {"command": "a && b"})


def test_messages_in_one_turn_are_spaced_unless_a_tool_line_sits_between():
    q = queue.Queue()
    w = cw.CodexWorker(q)
    t = {"streamed": set(), "texted": False, "tools": 0, "files": {}}
    w._text(t, "m1", "Running it.")
    w._item_started(t, {"type": "commandExecution", "id": "c1", "command": "echo hi"})
    w._text(t, "m2", "Done.")
    w._text(t, "m3", "Also.")
    deltas = []
    while not q.empty():
        k, p = q.get_nowait()
        if k == "delta":
            deltas.append(p)
    assert deltas == ["Running it.", "Done.", "\n\n", "Also."]


# ── parity with the Claude side: login, allowance, history, PDFs, Auto, MCP ──

def test_a_signed_out_codex_is_reported(harness, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    harness.start()
    assert harness.event("codex_auth") == {"ok": False}


def test_allowance_is_polled_while_idle_and_reads_as_a_quota_status(harness, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_USED", "85")
    monkeypatch.setattr(cw, "_RATE_POLL_S", 0.3)
    harness.start()
    q = harness.event("quota")
    assert q["status"] == "allowed_warning" and q["window"] == "five_hour"
    assert q["utilization"] == pytest.approx(0.85) and q["resets_at"] == 1800000000
    deadline = time.monotonic() + 8
    while len(harness.requests("account/rateLimits/read")) < 3:
        assert time.monotonic() < deadline, "the allowance was never re-read"
        time.sleep(0.2)


def test_past_threads_are_listed_and_archived(harness):
    harness.start()
    harness.w.list_sessions()
    rows = harness.wait("codex_sessions")
    assert [r["id"] for r in rows] == ["th-a", "th-b"]          # ephemeral ones left out
    assert rows[0]["title"] == "Summarise the deck" and rows[1]["title"] == "Excel macro"
    assert harness.requests("thread/list")[0]["cwd"] == cw.WORKING_DIR
    harness.w.archive("th-a")
    deadline = time.monotonic() + 5
    while not harness.requests("thread/archive"):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert harness.requests("thread/archive")[0] == {"threadId": "th-a"}


def test_auto_effort_sends_the_models_own_default(harness):
    harness.start(effort="auto")
    harness.w.ask("hi")
    harness.wait("turn_done")
    assert harness.requests("turn/start")[0]["effort"] == "medium"   # gpt-x's default


def _pdf_bytes(text):
    """A minimal but well-formed one-page PDF (real xref table) whose page shows `text`."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
            b"/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_a_pdfs_text_goes_along_with_its_path(harness, tmp_path):
    pytest.importorskip("pypdf")
    pdf = tmp_path / "q3.pdf"
    pdf.write_bytes(_pdf_bytes("Quarterly revenue up"))
    harness.start()
    harness.w.ask("summarise", [str(pdf)])
    harness.wait("turn_done")
    texts = [i["text"] for i in harness.requests("turn/start")[0]["input"] if i["type"] == "text"]
    doc = next(t for t in texts if t.startswith("[Attached PDF:"))
    assert str(pdf) in doc and "Quarterly revenue up" in doc


def test_mcp_flags_follow_the_overlays_settings(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('[mcp_servers.node_repl]\ncommand = "node"\n'
                                      '[mcp_servers."my docs"]\nurl = "https://x"\n', "utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(cw, "MCP_SERVERS", {
        "notion": {"type": "http", "url": "https://mcp.notion.com/mcp"},
        "tool": {"command": "npx", "args": ["-y", "x"], "env": {"K": "v"}}})
    monkeypatch.setattr(cw, "STRICT_MCP_CONFIG", True)
    added = ["-c", 'mcp_servers.notion={url = "https://mcp.notion.com/mcp"}',
             "-c", 'mcp_servers.tool={command = "npx", args = ["-y", "x"], env = {K = "v"}}']
    assert cw._mcp_flags() == ["-c", "mcp_servers.node_repl.enabled=false",
                               "-c", 'mcp_servers."my docs".enabled=false'] + added
    monkeypatch.setattr(cw, "STRICT_MCP_CONFIG", False)    # inherit the user's own servers
    assert cw._mcp_flags() == added


# ── regressions from the Codex code review (gpt-6.1-sol, 2026-10-28) ──

def test_stop_before_the_turn_has_an_id_is_remembered():
    q = queue.Queue()
    w = cw.CodexWorker(q)
    w._asking = True                       # an ask is still being turned into a turn
    w.interrupt()
    assert w._cancel is True
    assert w._cancelled_early() is True
    assert [k for k, _ in iter(lambda: q.get_nowait() if not q.empty() else (None, None), (None, None))] \
        == ["system", "turn_done"]


def test_read_only_mid_turn_stops_the_turn_before_confirming(harness):
    harness.start()
    harness.w.ask("SLOW")
    deadline = time.monotonic() + 10
    while harness.w._turn is None or not harness.w._turn.get("id"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    harness.w.set_permission_mode("plan")
    assert harness.wait("permission_mode") == "plan"
    kinds = harness.kinds()
    assert kinds.index("turn_done") < kinds.index("permission_mode")   # stopped, THEN locked
    assert harness.requests("turn/interrupt")


def test_a_model_without_the_pinned_level_falls_back_to_auto(harness):
    harness.start()
    harness.event("codex_models")
    harness.w.set_effort("ultra")                  # gpt-y has it, gpt-x does not
    harness.wait("effort")
    harness.w.set_model("gpt-x")
    assert harness.wait("effort") == "auto"
    harness.w.ask("hi")
    harness.wait("turn_done")
    assert harness.requests("turn/start")[0]["effort"] == "medium"   # gpt-x's own default


def test_a_full_allowance_read_clears_a_stale_rejection():
    q = queue.Queue()
    w = cw.CodexWorker(q)
    w._merge_rate_limits({"rateLimitReachedType": "usageLimit", "primary":
                          {"usedPercent": 100, "windowDurationMins": 300}}, full=True)
    w._merge_rate_limits({"primary": {"usedPercent": 12, "windowDurationMins": 300},
                          "rateLimitReachedType": None}, full=True)
    readings = [p for k, p in iter(lambda: q.get_nowait() if not q.empty() else (None, None),
                                   (None, None)) if k == "quota"]
    assert readings[0]["status"] == "rejected" and readings[-1]["status"] == "allowed"


def test_a_rolling_update_does_not_clear_with_nulls():
    q = queue.Queue()
    w = cw.CodexWorker(q)
    w._merge_rate_limits({"rateLimitReachedType": "usageLimit"}, full=True)
    w._merge_rate_limits({"rateLimitReachedType": None})           # sparse: "not sent"
    assert w._rl.get("rateLimitReachedType") == "usageLimit"


def test_allowing_a_permissions_request_grants_what_was_asked(harness):
    harness.start()
    harness.w.ask("PERMS")
    ask = harness.wait("approval")
    assert ask["kind"] == "permissions" and "network" in ask["permissions"]
    harness.w.respond_approval(901, "accept")
    harness.wait("turn_done")
    resp = next(r for r in harness.responses() if r.get("id") == 901)
    assert resp["result"] == {"permissions": {"network": {"enabled": True}}, "scope": "turn"}


def test_a_replaced_dead_server_resumes_the_thread(harness):
    harness.start()
    harness.w._rpc.alive = False           # died; its "exit" not handled yet
    harness.w.ask("hi")
    harness.wait("turn_done")
    assert harness.requests("thread/resume")[-1]["threadId"] == "th-1"


def test_mcp_names_on_python_310_without_tomllib(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "x"\n[mcp_servers.node_repl]\ncommand = "n"\n'
                                      '[mcp_servers."my docs"]\nurl = "u"\n'
                                      '[mcp_servers.node_repl.env]\nK = "v"\n', "utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setitem(sys.modules, "tomllib", None)              # import → ImportError
    assert cw._user_mcp_names() == ["node_repl", "my docs"]
