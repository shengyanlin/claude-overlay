"""Tests for cliupdate — detecting an out-of-date `claude` CLI and running the update.

Everything is monkeypatched so no real npm / CLI is ever spawned; these assert the pure
decision / parsing / caching / degradation logic. Cross-platform: the module is stdlib-only
and the Windows console flag is 0 off-Windows."""
import json
import os
import sys
import time

import pytest

import cliupdate as cu

_REAL_NPM_PREFIX = cu.npm_prefix


@pytest.fixture(autouse=True)
def _no_real_npm_folder(monkeypatch):
    """run_update asks npm_prefix where the running CLI lives: on a developer's machine that
    is their real %APPDATA%\\npm. The prefix tests below use _REAL_NPM_PREFIX on a fake one."""
    monkeypatch.setattr(cu, "npm_prefix", lambda cli="claude": None)


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


# ── the same check and one-click update for the Codex CLI ──

def _codex_run(calls, latest="0.161.0", installed="0.160.1", install_rc=0):
    def run(args, timeout):
        calls.append(args)
        if args[:2] == ["codex", "--version"]:
            return (0, f"codex-cli {installed}\n", "")
        if args[:2] == ["npm", "view"]:
            return (0, latest + "\n", "")
        if args[:2] == ["npm", "install"]:
            return (install_rc, "", "")
        return None
    return run


def test_codex_check_uses_its_own_package_and_cache(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "_run", _codex_run(calls))
    monkeypatch.setattr(cu, "_CACHE_PATH", tmp_path / "claude.json")
    monkeypatch.setattr(cu, "_CODEX_CACHE_PATH", tmp_path / "codex.json")
    assert cu.check_update(cli="codex") == {"installed": "0.160.1", "latest": "0.161.0",
                                            "behind": True}
    assert ["npm", "view", "@openai/codex", "version"] in calls
    assert (tmp_path / "codex.json").exists() and not (tmp_path / "claude.json").exists()


def test_codex_run_update_installs_the_codex_package(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "_run", _codex_run(calls, installed="0.161.0"))
    monkeypatch.setattr(cu, "_CODEX_CACHE_PATH", tmp_path / "codex.json")
    assert cu.run_update(cli="codex") == (True, "0.161.0")
    assert ["npm", "install", "-g", "@openai/codex@latest"] in calls


# ── npm on a network that inspects HTTPS (UNABLE_TO_GET_ISSUER_CERT_LOCALLY, 1.25.2) ──

def test_npm_runs_with_the_certificates_windows_trusts(monkeypatch):
    seen = {}
    monkeypatch.setattr(cu, "_windows_ca_pem", lambda: r"C:\state\windows-ca.pem")
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    monkeypatch.delenv("NODE_USE_SYSTEM_CA", raising=False)

    class R:
        returncode, stdout, stderr = 0, b"", b""

    def fake_run(args, **kw):
        seen.update(kw)
        return R()
    monkeypatch.setattr(cu.subprocess, "run", fake_run)
    cu._run(["npm", "view", "x", "version"], 5)
    assert seen["env"]["NODE_USE_SYSTEM_CA"] == "1"
    assert seen["env"]["NODE_EXTRA_CA_CERTS"] == r"C:\state\windows-ca.pem"


def test_certificate_settings_someone_already_made_are_kept(monkeypatch):
    monkeypatch.setattr(cu, "_windows_ca_pem", lambda: r"C:\state\windows-ca.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", r"C:\it\proxy-root.pem")
    monkeypatch.setenv("NODE_USE_SYSTEM_CA", "0")
    env = cu._npm_env()
    assert env["NODE_EXTRA_CA_CERTS"] == r"C:\it\proxy-root.pem"
    assert env["NODE_USE_SYSTEM_CA"] == "0"


def test_no_certificate_file_still_runs_npm(monkeypatch):
    monkeypatch.setattr(cu, "_windows_ca_pem", lambda: None)
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    env = cu._npm_env()
    assert "NODE_EXTRA_CA_CERTS" not in env and env["NODE_USE_SYSTEM_CA"] == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows certificate store")
def test_the_windows_roots_file_loads_and_is_written_once(monkeypatch, tmp_path):
    from test_windowsca import openssl_loads
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(cu, "_ca_pem", None)
    pem = cu._windows_ca_pem()
    assert pem == str(tmp_path / "claude-overlay" / "windows-ca.pem")
    blocks = open(pem, encoding="ascii").read().count("-----BEGIN CERTIFICATE-----")
    assert blocks > 0 and openssl_loads(pem) == blocks
    stamp = os.stat(pem).st_mtime_ns
    assert cu._windows_ca_pem() == pem and os.stat(pem).st_mtime_ns == stamp   # once a run
    os.remove(pem)
    assert cu._windows_ca_pem() == pem and os.path.isfile(pem)   # gone: written again
    assert not [p for p in os.listdir(tmp_path / "claude-overlay") if p.endswith(".partial")]


# ── which copy an update replaces: the one that runs ──

def _npm_folder(tmp_path, pkg_parts, shim):
    folder = tmp_path / "npm"
    (folder.joinpath("node_modules", *pkg_parts)).mkdir(parents=True)
    (folder / shim).write_text("@echo off")
    return folder


def test_npm_prefix_is_the_folder_of_the_running_shim(monkeypatch, tmp_path):
    folder = _npm_folder(tmp_path, ["@openai", "codex"], "codex.CMD")   # PATHEXT's own case
    monkeypatch.setattr(cu.shutil, "which", lambda name: str(folder / "codex.CMD"))
    assert _REAL_NPM_PREFIX("codex") == str(folder)


def test_a_native_or_foreign_install_is_not_npms(monkeypatch, tmp_path):
    native = tmp_path / "claude.exe"
    native.write_text("")
    monkeypatch.setattr(cu.shutil, "which", lambda name: str(native))
    assert _REAL_NPM_PREFIX("claude") is None                  # ~/.local/bin/claude.exe
    lone = tmp_path / "bin"
    lone.mkdir()
    (lone / "claude.cmd").write_text("")                       # a shim with no package by it
    monkeypatch.setattr(cu.shutil, "which", lambda name: str(lone / "claude.cmd"))
    assert _REAL_NPM_PREFIX("claude") is None
    monkeypatch.setattr(cu.shutil, "which", lambda name: None)
    assert _REAL_NPM_PREFIX("claude") is None and not cu.npm_managed("claude")


def test_cmd_reads_an_argument_with_an_ampersand_as_one_argument(monkeypatch, tmp_path):
    """Windows' list quoting quotes only for spaces: an & went to cmd bare and ended the
    command there."""
    assert cu._cmdline(["echo", r"D:\Tools&Apps\npm"]) == 'echo "D:\\Tools&Apps\\npm"'
    assert cu._cmdline(["npm", "view", "x", "version"]) == "npm view x version"
    if sys.platform == "win32":
        monkeypatch.setattr(cu, "_windows_ca_pem", lambda: None)   # no real state file
        rc, out, err = cu._run(["echo", str(tmp_path / "a&b (c)")], 30)
        assert rc == 0 and not err.strip()              # nothing ran as a second command
        assert "a&b (c)" in out


def test_the_update_goes_into_the_folder_that_runs(monkeypatch, tmp_path):
    """An .npmrc prefix moved since the install must not get a second copy that never runs."""
    calls = []
    monkeypatch.setattr(cu, "npm_prefix", lambda cli="claude": r"D:\%USERNAME% & co\npm")
    monkeypatch.setattr(cu, "_find_npm", lambda: "npm")
    monkeypatch.setattr(cu, "_run", lambda args, timeout, env_extra=None: (
        calls.append((args, env_extra)), (0, "", ""))[1])
    monkeypatch.setattr(cu, "installed_version", lambda: "2.1.204")
    monkeypatch.setattr(cu, "_CACHE_PATH", tmp_path / "c.json")
    assert cu.run_update() == (True, "2.1.204")
    assert calls[-1] == (["npm", "install", "-g", "@anthropic-ai/claude-code@latest"],
                         {"npm_config_prefix": r"D:\%USERNAME% & co\npm"})


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_a_folder_in_the_environment_reaches_the_child_exactly(monkeypatch):
    """cmd expands %NAME% even inside quotes; it never touches the environment it hands on."""
    monkeypatch.setattr(cu, "_windows_ca_pem", lambda: None)
    monkeypatch.setenv("OV_SEGMENT", "expanded")
    odd = r"C:\%OV_SEGMENT% & (x)\npm"
    rc, out, _err = cu._run(["set", "npm_config_prefix"], 30,
                            env_extra={"npm_config_prefix": odd})
    assert rc == 0 and ("npm_config_prefix=" + odd) in out
