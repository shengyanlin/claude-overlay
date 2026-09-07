"""Tests for modelresolve — mapping a family alias ("opus") to the concrete latest id.

Everything is monkeypatched so no real `claude` CLI is ever spawned; these assert the
pure decision/parsing/caching logic (the streaming-alias-lag workaround, see the module
docstring). Cross-platform: the module is stdlib-only and the Windows console flag is 0
off-Windows."""
import json
import time

import pytest

import modelresolve as mr


# ── pass-through: only bare family aliases get resolved; everything else is untouched ──

def test_concrete_id_passes_through_without_touching_cli(monkeypatch):
    # A concrete id must be returned verbatim AND must not trigger any CLI work.
    called = []
    monkeypatch.setattr(mr, "_find_cli", lambda: called.append("find") or "claude")
    monkeypatch.setattr(mr, "_probe_concrete", lambda *a: called.append("probe") or "X")
    assert mr.resolve_model("claude-opus-4-8") == "claude-opus-4-8"
    assert called == []          # neither _find_cli nor _probe_concrete ran


def test_inherit_and_unknown_and_blank_pass_through():
    assert mr.resolve_model("inherit") == "inherit"
    assert mr.resolve_model("some-future-provider/model-x") == "some-future-provider/model-x"
    assert mr.resolve_model("") == ""
    assert mr.resolve_model("   ") == "   "
    assert mr.resolve_model(None) is None
    assert mr.resolve_model(123) == 123


# ── happy path: alias -> concrete latest id ─────────────────────────────────────────

def _patch_ok(monkeypatch, concrete="claude-opus-4-8", sig="claude|100|111"):
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: sig)
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: concrete)


def test_alias_resolves_to_concrete(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    _patch_ok(monkeypatch)
    assert mr.resolve_model("opus") == "claude-opus-4-8"


def test_alias_1m_suffix_reattached(monkeypatch, tmp_path):
    # The "[1m]" is a context-window modifier, not part of the model id — strip it for the
    # probe, then re-attach it so the streaming session still gets the 1M context.
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    _patch_ok(monkeypatch)
    assert mr.resolve_model("opus[1m]") == "claude-opus-4-8[1m]"


def test_alias_is_case_and_whitespace_tolerant(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    _patch_ok(monkeypatch)
    assert mr.resolve_model("  OPUS  ") == "claude-opus-4-8"


def test_fable_alias_is_resolved_like_the_others(monkeypatch, tmp_path):
    # "fable" joined config.MODELS so the overlay can run the same model a user's CLI is
    # pinned to. It must go through the same resolve path (NOT pass through as an unknown
    # spec, which would hand the streaming session a bare alias — the version-lag bug this
    # module exists to prevent), and the [1m] suffix must compose with it.
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    _patch_ok(monkeypatch, concrete="claude-fable-5")
    assert mr.resolve_model("fable") == "claude-fable-5"
    assert mr.resolve_model("fable[1m]") == "claude-fable-5[1m]"


# ── graceful degradation: any failure returns the ORIGINAL spec (never breaks startup) ──

def test_probe_failure_returns_original_alias(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: None)
    assert mr.resolve_model("opus") == "opus"
    assert mr.resolve_model("opus[1m]") == "opus[1m]"


def test_no_cli_returns_original_alias(monkeypatch):
    monkeypatch.setattr(mr, "_find_cli", lambda: None)
    assert mr.resolve_model("opus") == "opus"


# ── cache: keyed by the CLI launcher's file signature + a probe timestamp; a fresh hit
#    (matching signature, within the TTL) skips the probe, while a changed CLI, an aged-out
#    entry, or a pre-TTL entry (no timestamp) re-probes ──

def test_cache_hit_skips_probe(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"signature": "claude|100|111", "probed_at": time.time(),
                                 "aliases": {"opus": "claude-opus-4-8"}}), "utf-8")
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    probed = []
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: probed.append(base) or "SHOULD-NOT-HAPPEN")
    assert mr.resolve_model("opus") == "claude-opus-4-8"
    assert mr.resolve_model("opus[1m]") == "claude-opus-4-8[1m]"   # suffix reattached from cache
    assert probed == []                                            # cache served both, no probe


def test_cache_signature_change_reprobes_and_rewrites(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"signature": "claude|90|100",     # old CLI
                                 "aliases": {"opus": "claude-opus-4-6"}}), "utf-8")
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|222")   # CLI upgraded
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: "claude-opus-4-8")
    assert mr.resolve_model("opus") == "claude-opus-4-8"
    # the stale-signature cache was replaced with the fresh probe under the new signature
    saved = json.loads(cache.read_text("utf-8"))
    assert saved["signature"] == "claude|100|222"
    assert saved["aliases"]["opus"] == "claude-opus-4-8"


def test_cache_miss_writes_result(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: "claude-sonnet-4-6")
    before = time.time()
    assert mr.resolve_model("sonnet") == "claude-sonnet-4-6"
    saved = json.loads(cache.read_text("utf-8"))
    assert saved["signature"] == "claude|100|111"
    assert saved["aliases"] == {"sonnet": "claude-sonnet-4-6"}
    assert before <= saved["probed_at"] <= time.time()   # stamped now for the TTL


def test_expired_cache_reprobes_and_restamps(monkeypatch, tmp_path):
    # Same CLI signature, but the probe is older than the TTL -> re-probe. This is the
    # "org enabled a newer Sonnet with no CLI upgrade" case: the signature is unchanged, so
    # only the TTL rescues us. The classic symptom was 'sonnet' stuck on the previous id.
    cache = tmp_path / "c.json"
    stale = time.time() - (mr._CACHE_TTL_S + 100)
    cache.write_text(json.dumps({"signature": "claude|100|111", "probed_at": stale,
                                 "aliases": {"sonnet": "claude-sonnet-4-6"}}), "utf-8")
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    probed = []
    monkeypatch.setattr(mr, "_probe_concrete",
                        lambda cli, base: probed.append(base) or "claude-sonnet-5")
    assert mr.resolve_model("sonnet") == "claude-sonnet-5"   # fresh id, not the stale one
    assert probed == ["sonnet"]                              # the TTL forced a re-probe
    saved = json.loads(cache.read_text("utf-8"))
    assert saved["aliases"]["sonnet"] == "claude-sonnet-5"
    assert saved["probed_at"] > stale                        # restamped


def test_pre_ttl_cache_without_timestamp_reprobes(monkeypatch, tmp_path):
    # A cache written by a pre-TTL overlay has a matching signature but NO 'probed_at'.
    # It must be treated as stale so upgrading the overlay self-heals a stuck alias.
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"signature": "claude|100|111",
                                 "aliases": {"sonnet": "claude-sonnet-4-6"}}), "utf-8")
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    probed = []
    monkeypatch.setattr(mr, "_probe_concrete",
                        lambda cli, base: probed.append(base) or "claude-sonnet-5")
    assert mr.resolve_model("sonnet") == "claude-sonnet-5"
    assert probed == ["sonnet"]


def test_new_alias_in_fresh_generation_keeps_timestamp(monkeypatch, tmp_path):
    # Resolving a second alias within a still-fresh generation must add it WITHOUT resetting
    # 'probed_at' — otherwise switching models on every launch would extend the TTL forever.
    cache = tmp_path / "c.json"
    stamp = time.time() - 60
    cache.write_text(json.dumps({"signature": "claude|100|111", "probed_at": stamp,
                                 "aliases": {"opus": "claude-opus-4-8"}}), "utf-8")
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: "claude-sonnet-5")
    assert mr.resolve_model("sonnet") == "claude-sonnet-5"   # not cached yet -> probed
    saved = json.loads(cache.read_text("utf-8"))
    assert saved["aliases"] == {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-5"}
    assert saved["probed_at"] == stamp                       # generation timestamp preserved


def test_use_cache_false_always_probes_and_never_writes(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    probed = []
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: probed.append(base) or "claude-opus-4-8")
    assert mr.resolve_model("opus", use_cache=False) == "claude-opus-4-8"
    assert probed == ["opus"]              # probed despite a would-be cache
    assert not cache.exists()              # and wrote nothing


def test_cli_signature_stats_the_file(monkeypatch, tmp_path):
    f = tmp_path / "claude.CMD"
    f.write_text("shim", "utf-8")
    sig = mr.cli_signature(str(f))
    assert sig is not None and str(f) in sig
    assert mr.cli_signature(str(tmp_path / "does-not-exist")) is None


# ── _probe_concrete: parse the CLI's -p JSON, prefer the family we asked for ──

def test_probe_concrete_single_model_key(monkeypatch):
    monkeypatch.setattr(mr, "_run_cli",
                        lambda cli, args, timeout: json.dumps({"modelUsage": {"claude-opus-4-8": {}}}))
    assert mr._probe_concrete("claude", "opus") == "claude-opus-4-8"


def test_probe_concrete_prefers_matching_family_over_helper(monkeypatch):
    # A turn can bill a small helper model alongside the main one; pick the family we asked for.
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: json.dumps(
        {"modelUsage": {"claude-haiku-4-5": {}, "claude-opus-4-8": {}}}))
    assert mr._probe_concrete("claude", "opus") == "claude-opus-4-8"


def test_probe_concrete_falls_back_to_top_level_model(monkeypatch):
    monkeypatch.setattr(mr, "_run_cli",
                        lambda cli, args, timeout: json.dumps({"modelUsage": {}, "model": "claude-opus-4-8"}))
    assert mr._probe_concrete("claude", "opus") == "claude-opus-4-8"


def test_probe_concrete_none_on_bad_json_or_no_output(monkeypatch):
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: "not json {{{")
    assert mr._probe_concrete("claude", "opus") is None
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: None)
    assert mr._probe_concrete("claude", "opus") is None


# ── cli_version parsing ─────────────────────────────────────────────────────────────

def test_cli_version_parses_semver(monkeypatch):
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: "2.1.156 (Claude Code)\n")
    assert mr.cli_version() == "2.1.156"


def test_cli_version_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: None)
    assert mr.cli_version() is None


# ── entitlement: which families this login may pick (menu filtering) ─────────────────

MODELS = [("Opus", "opus"), ("Opus (1M)", "opus[1m]"),
          ("Fable", "fable"), ("Fable (1M)", "fable[1m]"),
          ("Sonnet", "sonnet"), ("Haiku", "haiku")]


def _claude_json(tmp_path, entries, name=".claude.json"):
    """Write a .claude.json carrying a modelAccessCache and return its path."""
    p = tmp_path / name
    p.write_text(json.dumps({"modelAccessCache": entries, "junk": {"other": 1}}), "utf-8")
    return p


def _ent(api, yes=True):
    return {"apiName": api, "entitled": yes}


@pytest.fixture(autouse=True)
def _clear_ent_memo():
    """The entitlement memo is process-global; a leaked entry would make the next test read
    another test's file."""
    mr._ENT_MEMO["key"] = mr._ENT_MEMO["families"] = None
    yield
    mr._ENT_MEMO["key"] = mr._ENT_MEMO["families"] = None


def test_family_of_handles_both_naming_eras():
    # 'claude-3-opus-...' puts the family third, 'claude-opus-4-8' second — both appear in
    # a real modelAccessCache, so the family is matched per segment, not by position.
    assert mr._family_of("claude-3-opus-20240229") == "opus"
    assert mr._family_of("claude-opus-4-1-20250805-claude-ai") == "opus"
    assert mr._family_of("claude-haiku-4-5-20251001") == "haiku"
    assert mr._family_of("claude-fable-5") == "fable"
    assert mr._family_of("claude-sonnet-5") == "sonnet"
    assert mr._family_of("gpt-4o") is None
    assert mr._family_of(None) is None


def test_entitled_families_reads_the_cache(tmp_path):
    p = _claude_json(tmp_path, [_ent("claude-opus-5"), _ent("claude-sonnet-5"),
                                _ent("claude-fable-5", False)])
    assert mr.entitled_families(p) == frozenset({"opus", "sonnet"})


def test_only_explicit_true_entitles(tmp_path):
    # A missing / non-boolean flag is not a grant.
    p = _claude_json(tmp_path, [_ent("claude-opus-5"), {"apiName": "claude-fable-5"},
                                {"apiName": "claude-haiku-4-5", "entitled": "true"}])
    assert mr.entitled_families(p) == frozenset({"opus"})


def test_unknown_when_file_or_cache_is_missing_or_odd(tmp_path):
    assert mr.entitled_families(tmp_path / "nope.json") is None          # no file
    (tmp_path / "a.json").write_text("not json {{{", "utf-8")
    assert mr.entitled_families(tmp_path / "a.json") is None             # unparseable
    (tmp_path / "b.json").write_text(json.dumps({"other": 1}), "utf-8")
    assert mr.entitled_families(tmp_path / "b.json") is None             # no cache key
    assert mr.entitled_families(_claude_json(tmp_path, [], "c.json")) is None   # empty list
    (tmp_path / "d.json").write_text(json.dumps({"modelAccessCache": "x"}), "utf-8")
    assert mr.entitled_families(tmp_path / "d.json") is None             # wrong type
    # Entries present but none entitled and none recognisable -> still 'unknown', never
    # 'nothing available' (the caller would otherwise render an empty menu).
    p = _claude_json(tmp_path, [_ent("claude-opus-5", False)], "e.json")
    assert mr.entitled_families(p) is None


def test_memo_is_keyed_on_file_identity_and_mtime(tmp_path):
    p = _claude_json(tmp_path, [_ent("claude-opus-5")])
    assert mr.entitled_families(p) == frozenset({"opus"})
    # Rewriting the file (the CLI does this constantly) must be picked up, not memoized past.
    p.write_text(json.dumps({"modelAccessCache": [_ent("claude-fable-5")]}), "utf-8")
    os_stat_bump(p)
    assert mr.entitled_families(p) == frozenset({"fable"})


def os_stat_bump(p):
    """Force a distinguishable mtime even on a coarse filesystem clock."""
    import os as _os
    st = p.stat()
    _os.utime(p, ns=(st.st_atime_ns + 10 ** 9, st.st_mtime_ns + 10 ** 9))


def test_available_models_hides_unentitled_families(tmp_path):
    # The reported bug: a login with no Fable was still offered Fable (both entries).
    p = _claude_json(tmp_path, [_ent("claude-opus-5"), _ent("claude-sonnet-5"),
                                _ent("claude-haiku-4-5-20251001"),
                                _ent("claude-fable-5", False)])
    got = mr.available_models(MODELS, p)
    assert [lbl for lbl, _ in got] == ["Opus", "Opus (1M)", "Sonnet", "Haiku"]


def test_available_models_keeps_everything_when_entitlement_unknown(tmp_path):
    assert mr.available_models(MODELS, tmp_path / "nope.json") == MODELS


def test_available_models_never_returns_empty(tmp_path):
    # Our reading contradicting the whole menu means the reading is wrong; locking the user
    # out of the switcher entirely is the worse failure, so show everything.
    p = _claude_json(tmp_path, [_ent("claude-penguin-9")])
    assert mr.available_models(MODELS, p) == MODELS


def test_available_models_keeps_unclassifiable_specs(tmp_path):
    p = _claude_json(tmp_path, [_ent("claude-opus-5")])
    models = [("Opus", "opus"), ("Pinned", "claude-sonnet-4-6"), ("Inherit", "inherit"),
              ("Weird", None), ("Short",)]
    assert mr.available_models(models, p) == models


def test_probe_rejects_a_silent_fallback_to_another_family(monkeypatch):
    # `claude --model fable` on a login without Fable exits 0 having run something else.
    # Pinning that id would run a model the user never chose, so it must not resolve.
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: json.dumps(
        {"modelUsage": {"claude-sonnet-5": {}}, "model": "claude-sonnet-5"}))
    assert mr._probe_concrete("claude", "fable") is None


def test_unentitled_alias_passes_through_unpinned(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|1|2")
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: json.dumps(
        {"modelUsage": {"claude-sonnet-5": {}}}))
    assert mr.resolve_model("fable[1m]") == "fable[1m]"      # the original spec, unchanged
