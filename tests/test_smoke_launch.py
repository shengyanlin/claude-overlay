"""The launch smoke test's own safeguards (.github/scripts/smoke_launch.py).

That script starts `python claude_overlay.py` on a CI runner's real desktop session and
fails if the process exits, writes a crash log or reports a Tk callback error — the
workflow runs it as a step of its own. It opens a real window, so it is not run from here;
these tests cover what can be checked without one: it refuses to open a window unless it is
on CI or told to, and the environment it gives the app holds no login and reaches nothing.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "smoke_launch.py")


def test_the_smoke_script_refuses_to_open_a_window_unasked():
    env = {k: v for k, v in os.environ.items() if k != "CI"}
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True, timeout=60,
                       env=env, stdin=subprocess.DEVNULL)
    assert r.returncode == 2 and "--force" in r.stdout


def test_the_smoke_environment_carries_no_login_and_reaches_nothing(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("smoke_launch", SCRIPT)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "AWS_SECRET_ACCESS_KEY", "CODEX_API_KEY"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("https_proxy", "http://proxy.example:8080")
    env = smoke._isolated_env(str(tmp_path), str(tmp_path / "config.json"))
    assert not [k for k in env if k.upper().startswith(smoke.SECRET_PREFIXES)
                and k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")]
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path))
    assert env["CODEX_HOME"].startswith(str(tmp_path))
    assert env["LOCALAPPDATA"] == str(tmp_path)
    proxies = {k.upper(): v for k, v in env.items() if k.upper().endswith("_PROXY")}
    assert proxies == {"HTTP_PROXY": smoke.NOWHERE, "HTTPS_PROXY": smoke.NOWHERE,
                       "ALL_PROXY": smoke.NOWHERE}                 # and no NO_PROXY left
