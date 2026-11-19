"""Start the real app, the way a user does, as part of the suite — on CI only.

Everything else in tests/ builds the Overlay class inside pytest. This runs
.github/scripts/smoke_launch.py, which starts `python claude_overlay.py` through its
`__main__` block on the runner's real desktop session and fails if the process exits,
writes a crash log or reports a Tk callback error. It is the check for "every test passes
and the app doesn't open" on the platform a change was NOT written on, and it lives in the
suite so that every workflow that runs `pytest` runs it.

Skipped off CI: it opens a real window for a quarter of a minute. To run it by hand:
    python .github/scripts/smoke_launch.py --force
"""
import os
import subprocess
import sys

import pytest

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


@pytest.mark.skipif(not os.environ.get("CI"), reason="opens the real app window: CI only")
def test_the_app_starts_and_stays_up():
    r = subprocess.run([sys.executable, SCRIPT, "12"], capture_output=True, text=True,
                       timeout=180, stdin=subprocess.DEVNULL, encoding="utf-8",
                       errors="replace")
    assert r.returncode == 0, f"{r.stdout[-6000:]}\n{r.stderr[-2000:]}"
    assert "OK: the app started and ran" in r.stdout
