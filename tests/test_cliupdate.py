"""Tests for cliupdate — detecting an out-of-date `claude` CLI and running the update.

Everything is monkeypatched so no real npm / CLI is ever spawned; these assert the pure
decision / parsing / caching / degradation logic. Cross-platform: the module is stdlib-only
and the Windows console flag is 0 off-Windows."""
import json
import time

import cliupdate as cu


# ── version parsing (numeric, not lexical) ──────────────────────────────────────────

def test_parse_ver_numeric_compare():
    assert cu._parse_ver("2.1.198") == (2, 1, 198)
    assert cu._parse_ver("2.1.198") < cu._parse_ver("2.1.204")   # not a string compare
    assert cu._parse_ver("2.1.9") < cu._parse_ver("2.1.10")      # 9 < 10 numerically
    assert cu._parse_ver("") == (0,)                             # unreadable -> sorts oldest
    assert cu._parse_ver(None) == (0,)


# ── _npm_latest: parse STDOUT only (a stderr upgrade-nag must not be read as the version) ──

def test_npm_latest_reads_stdout_not_stderr_nag(monkeypatch):
    monkeypatch.setattr(cu, "_run", lambda args, timeout: (
        0, "2.1.204\n", "npm notice New major version of npm available! 10.8.2 -> 11.0.0\n"))
    assert cu._npm_latest() == "2.1.204"


def test_npm_latest_none_on_nonzero_or_nospawn(monkeypatch):
    monkeypatch.setattr(cu, "_run", lambda args, timeout: (1, "", "boom"))
    assert cu._npm_latest() is None
    monkeypatch.setattr(cu, "_run", lambda args, timeout: None)
    assert cu._npm_latest() is None


# ── check_update: behind / not-behind, writes a cache, degrades to None ─────────────

def _patch_versions(monkeypatch, installed, latest):
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "installed_version", lambda: installed)
    monkeypatch.setattr(cu, "_npm_latest", lambda: latest)


def test_check_update_behind_writes_cache(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    monkeypatch.setattr(cu, "_CACHE_PATH", cache)
    _patch_versions(monkeypatch, "2.1.198", "2.1.204")
    info = cu.check_update()
    assert info == {"installed": "2.1.198", "latest": "2.1.204", "behind": True}
    saved = json.loads(cache.read_text("utf-8"))
    assert saved["result"] == info and isinstance(saved["ts"], (int, float))


def test_check_update_not_behind_when_current(monkeypatch, tmp_path):
    monkeypatch.setattr(cu, "_CACHE_PATH", tmp_path / "c.json")
    _patch_versions(monkeypatch, "2.1.204", "2.1.204")
    assert cu.check_update()["behind"] is False


def test_check_update_none_without_npm(monkeypatch):
    calls = []
    monkeypatch.setattr(cu, "_find_npm", lambda: None)
    monkeypatch.setattr(cu, "installed_version", lambda: calls.append("v") or "2.1.198")
    assert cu.check_update() is None
    assert calls == []                       # short-circuits before any node/npm cold start


def test_check_update_none_on_version_probe_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(cu, "_CACHE_PATH", tmp_path / "c.json")
    _patch_versions(monkeypatch, None, "2.1.204")       # `claude -v` failed
    assert cu.check_update() is None
    _patch_versions(monkeypatch, "2.1.198", None)       # registry lookup failed
    assert cu.check_update() is None


# ── caching: a fresh cache skips the slow calls; a stale one re-queries ──────────────

def test_check_update_uses_fresh_cache(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    result = {"installed": "2.1.198", "latest": "2.1.204", "behind": True}
    cache.write_text(json.dumps({"ts": time.time(), "result": result}), "utf-8")
    monkeypatch.setattr(cu, "_CACHE_PATH", cache)
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    called = []
    monkeypatch.setattr(cu, "installed_version", lambda: called.append("v") or "x")
    monkeypatch.setattr(cu, "_npm_latest", lambda: called.append("l") or "x")
    assert cu.check_update() == result
    assert called == []                      # fresh cache served it, no cold starts


def test_check_update_ignores_stale_cache(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    stale = {"installed": "2.1.0", "latest": "2.1.1", "behind": True}
    cache.write_text(json.dumps({"ts": time.time() - cu._CHECK_INTERVAL_S - 100,
                                 "result": stale}), "utf-8")
    monkeypatch.setattr(cu, "_CACHE_PATH", cache)
    _patch_versions(monkeypatch, "2.1.198", "2.1.204")
    info = cu.check_update()
    assert info["latest"] == "2.1.204"       # re-queried, not the stale cached value


def test_force_bypasses_fresh_cache(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"ts": time.time(),
                                 "result": {"installed": "a", "latest": "b", "behind": True}}),
                     "utf-8")
    monkeypatch.setattr(cu, "_CACHE_PATH", cache)
    _patch_versions(monkeypatch, "2.1.204", "2.1.204")
    assert cu.check_update(force=True)["behind"] is False   # ignored the fresh cache


# ── run_update: success refreshes cache to not-behind; failure surfaces a reason ─────

def test_run_update_success_refreshes_cache(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    monkeypatch.setattr(cu, "_CACHE_PATH", cache)
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "_run", lambda args, timeout: (0, "changed 1 package\n", ""))
    monkeypatch.setattr(cu, "installed_version", lambda: "2.1.204")
    assert cu.run_update() == (True, "2.1.204")
    saved = json.loads(cache.read_text("utf-8"))["result"]
    assert saved == {"installed": "2.1.204", "latest": "2.1.204", "behind": False}


def test_run_update_busy_gives_actionable_reason(monkeypatch):
    # The common Windows failure: claude.exe is locked because a Claude process is running.
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "_run", lambda args, timeout: (
        1, "", "npm error code EBUSY\n"
               "npm error EBUSY: resource busy or locked, copyfile '...\\claude.exe'\n"
               "npm error A complete log of this run can be found in: C:\\x.log\n"))
    ok, reason = cu.run_update()
    assert ok is False
    assert "running Claude process" in reason and "try again" in reason
    assert "complete log" not in reason.lower()      # not the useless npm tail line


def test_run_update_permission_reason(monkeypatch):
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "_run", lambda args, timeout: (1, "", "npm error code EACCES\n"))
    ok, reason = cu.run_update()
    assert ok is False and "permission denied" in reason


def test_run_update_skips_complete_log_line(monkeypatch):
    # A non-lock failure surfaces the real error line, not npm's "A complete log…" pointer.
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "_run", lambda args, timeout: (
        1, "", "npm error network request failed\n"
               "npm error A complete log of this run can be found in: C:\\x.log\n"))
    ok, reason = cu.run_update()
    assert ok is False and reason == "network request failed"   # stripped "npm error " prefix too


def test_failure_reason_fallback_when_empty():
    assert cu._failure_reason(7, "", "") == "npm exited with code 7"


def test_run_update_none_when_npm_missing(monkeypatch):
    monkeypatch.setattr(cu, "_find_npm", lambda: None)
    ok, reason = cu.run_update()
    assert ok is False and "npm" in reason


def test_run_update_failure_when_cannot_spawn(monkeypatch):
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "_run", lambda args, timeout: None)
    ok, reason = cu.run_update()
    assert ok is False and "launch" in reason
