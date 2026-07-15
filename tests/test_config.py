# -*- coding: utf-8 -*-
"""Unit tests for config.py (repo root).

All tests are deterministic and require no network, display, or logged-in CLI.
Env-var mutations use monkeypatch so they never leak between tests.
"""
import os
import importlib

import pytest

import config


# ---------------------------------------------------------------------------
# 1. _env_int
# ---------------------------------------------------------------------------

class TestEnvInt:
    """Tests for config._env_int(name, default, min_value, max_value)."""

    _NAME = "TEST_ENV_INT_XYZ"

    def test_value_in_range_returned_as_is(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "75")
        assert config._env_int(self._NAME, 50, 10, 100) == 75

    def test_value_at_min_boundary(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "10")
        assert config._env_int(self._NAME, 50, 10, 100) == 10

    def test_value_at_max_boundary(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "100")
        assert config._env_int(self._NAME, 50, 10, 100) == 100

    def test_below_min_clamps_to_min(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "1")
        assert config._env_int(self._NAME, 50, 10, 100) == 10

    def test_above_max_clamps_to_max(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "999")
        assert config._env_int(self._NAME, 50, 10, 100) == 100

    def test_non_integer_string_returns_default(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "abc")
        assert config._env_int(self._NAME, 50, 10, 100) == 50

    def test_float_string_returns_default(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "3.14")
        assert config._env_int(self._NAME, 50, 10, 100) == 50

    def test_empty_string_returns_default(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "")
        assert config._env_int(self._NAME, 50, 10, 100) == 50

    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(self._NAME, raising=False)
        assert config._env_int(self._NAME, 42, 0, 1000) == 42

    def test_negative_value_clamps(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "-5")
        assert config._env_int(self._NAME, 50, 0, 100) == 0


# ---------------------------------------------------------------------------
# 2. _env_bool
# ---------------------------------------------------------------------------

class TestEnvBool:
    """Tests for config._env_bool(name, default)."""

    _NAME = "TEST_ENV_BOOL_XYZ"

    def test_unset_returns_default_true(self, monkeypatch):
        monkeypatch.delenv(self._NAME, raising=False)
        assert config._env_bool(self._NAME, True) is True

    def test_unset_returns_default_false(self, monkeypatch):
        monkeypatch.delenv(self._NAME, raising=False)
        assert config._env_bool(self._NAME, False) is False

    @pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "off"])
    def test_falsy_strings_return_false(self, monkeypatch, falsy):
        monkeypatch.setenv(self._NAME, falsy)
        assert config._env_bool(self._NAME, True) is False

    @pytest.mark.parametrize("blank", ["", " ", "   ", "\t"])
    def test_blank_value_returns_default(self, monkeypatch, blank):
        # set-but-blank (empty / whitespace-only) is treated as unset -> keep the
        # default, so a stray space in an env var can't silently flip the flag.
        monkeypatch.setenv(self._NAME, blank)
        assert config._env_bool(self._NAME, True) is True
        assert config._env_bool(self._NAME, False) is False

    def test_whitespace_zero_returns_false(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "  0  ")
        assert config._env_bool(self._NAME, True) is False

    def test_whitespace_false_returns_false(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "  false  ")
        assert config._env_bool(self._NAME, True) is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "whatever", "True", "YES"])
    def test_truthy_strings_return_true(self, monkeypatch, truthy):
        monkeypatch.setenv(self._NAME, truthy)
        assert config._env_bool(self._NAME, False) is True

    def test_case_insensitive_false(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "FALSE")
        assert config._env_bool(self._NAME, True) is False

    def test_case_insensitive_true(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "TRUE")
        assert config._env_bool(self._NAME, False) is True

    def test_no_returns_false(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "No")
        assert config._env_bool(self._NAME, True) is False

    def test_off_returns_false(self, monkeypatch):
        monkeypatch.setenv(self._NAME, "OFF")
        assert config._env_bool(self._NAME, True) is False


# ---------------------------------------------------------------------------
# 3. THEMES and T
# ---------------------------------------------------------------------------

class TestThemes:
    """Tests for config.THEMES and config.T."""

    def test_themes_has_light_and_dark(self):
        assert "light" in config.THEMES
        assert "dark" in config.THEMES

    def test_light_and_dark_have_identical_key_sets(self):
        assert set(config.THEMES["light"]) == set(config.THEMES["dark"])

    def test_required_keys_present_in_light(self):
        for key in ("bg", "text", "accent"):
            assert key in config.THEMES["light"], f"'light' theme missing key: {key}"

    def test_required_keys_present_in_dark(self):
        for key in ("bg", "text", "accent"):
            assert key in config.THEMES["dark"], f"'dark' theme missing key: {key}"

    def test_T_is_light_theme(self):
        # THEME defaults to "light", so T must be the light sub-dict
        assert config.T is config.THEMES["light"]

    def test_T_equals_light_theme_values(self):
        assert config.T == config.THEMES["light"]


# ---------------------------------------------------------------------------
# 4. MODEL and MODELS
# ---------------------------------------------------------------------------

class TestModels:
    """Tests for config.MODEL and config.MODELS."""

    def test_model_is_opus_alias(self):
        # A family alias, not a pinned version, so new Opus releases are adopted
        # automatically (no code change). See config.py's MODEL comment.
        assert config.MODEL == "opus"

    def test_models_is_non_empty_list(self):
        assert isinstance(config.MODELS, list)
        assert len(config.MODELS) > 0

    def test_models_contains_2_tuples(self):
        for item in config.MODELS:
            assert isinstance(item, tuple), f"Expected tuple, got {type(item)}: {item}"
            assert len(item) == 2, f"Expected 2-tuple, got length {len(item)}: {item}"
            label, model_id = item
            assert isinstance(label, str), f"Label must be str: {label!r}"
            assert isinstance(model_id, str), f"Model id must be str: {model_id!r}"

    def test_models_contains_1m_variant(self):
        ids = [model_id for _, model_id in config.MODELS]
        assert "opus[1m]" in ids, (
            f"Expected 'opus[1m]' in MODELS ids, got: {ids}"
        )

    def test_models_use_family_aliases_not_pinned_versions(self):
        # The whole point of the switcher: every entry tracks the LATEST model of its
        # family, so no id may hardcode a version (e.g. "claude-opus-4-8") — that would
        # freeze the overlay on an old model until someone edits this file. Allowed ids
        # are the bare family aliases opus/sonnet/haiku, optionally with a "[1m]" suffix.
        for _, model_id in config.MODELS:
            base = model_id.replace("[1m]", "")
            assert base in ("opus", "sonnet", "haiku"), (
                f"MODELS id {model_id!r} is not a bare family alias — it won't auto-update "
                f"to new model releases. Use 'opus'/'sonnet'/'haiku' (optionally '[1m]')."
            )


# ---------------------------------------------------------------------------
# 5. STRICT_MCP_CONFIG
# ---------------------------------------------------------------------------

class TestStrictMcpConfig:
    """Tests for config.STRICT_MCP_CONFIG.

    The value is computed at import time from _env_bool("CLAUDE_OVERLAY_STRICT_MCP", True).
    In a clean CI/dev environment CLAUDE_OVERLAY_STRICT_MCP is unset, so the default
    (True) is expected.  We assert the current module value is True — if the env var
    happens to be set in this environment the test is intentionally skipped.
    """

    def test_strict_mcp_config_is_true_when_unset(self):
        # If the env var is explicitly set, skip (we can't un-import the module).
        env_val = os.environ.get("CLAUDE_OVERLAY_STRICT_MCP")
        if env_val is not None:
            pytest.skip("CLAUDE_OVERLAY_STRICT_MCP is set in env; skipping default check")
        assert config.STRICT_MCP_CONFIG is True

    def test_strict_mcp_config_is_bool(self):
        assert isinstance(config.STRICT_MCP_CONFIG, bool)


# ---------------------------------------------------------------------------
# 6. SHOT_JPEG_QUALITY and SYSTEM_APPEND
# ---------------------------------------------------------------------------

class TestMiscConstants:
    """Tests for SHOT_JPEG_QUALITY and SYSTEM_APPEND."""

    def test_shot_jpeg_quality_is_int(self):
        assert isinstance(config.SHOT_JPEG_QUALITY, int)

    def test_shot_jpeg_quality_in_range(self):
        assert 50 <= config.SHOT_JPEG_QUALITY <= 95, (
            f"SHOT_JPEG_QUALITY={config.SHOT_JPEG_QUALITY} is outside [50, 95]"
        )

    def test_system_append_is_str(self):
        assert isinstance(config.SYSTEM_APPEND, str)

    def test_system_append_is_non_empty(self):
        assert len(config.SYSTEM_APPEND.strip()) > 0


# ---------------------------------------------------------------------------
# 7. SHOT_SCOPE
# ---------------------------------------------------------------------------

class TestShotScope:
    """Tests for SHOT_SCOPE (active-window vs. all-screens capture default)."""

    def test_default_is_screens(self):
        env_val = os.environ.get("CLAUDE_OVERLAY_SHOT_SCOPE")
        if env_val is not None:
            pytest.skip("CLAUDE_OVERLAY_SHOT_SCOPE is set in env; skipping default check")
        assert config.SHOT_SCOPE == "screens"

    def test_env_override_is_normalized(self, monkeypatch):
        # The env value is stripped + lowercased so "  Window " still means window scope.
        monkeypatch.setenv("CLAUDE_OVERLAY_SHOT_SCOPE", "  Window ")
        try:
            importlib.reload(config)
            assert config.SHOT_SCOPE == "window"
        finally:
            monkeypatch.delenv("CLAUDE_OVERLAY_SHOT_SCOPE", raising=False)
            importlib.reload(config)

    def test_shot_scope_is_str(self):
        assert isinstance(config.SHOT_SCOPE, str)
