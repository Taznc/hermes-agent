"""Preset settings through the registered RPC and real profile/config stores."""

from copy import deepcopy
import os
from pathlib import Path
import threading

import pytest
import yaml

from hermes_cli.config import load_config, read_user_config_raw
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
import tui_gateway.server as srv


KEY = "model_recommendation.preset"
PRESETS = ("balanced", "save_codex", "best_quality")


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    # Profile enumeration is HOME-anchored, not HERMES_HOME-anchored. Both must
    # be sandboxed, including any dispatcher pins inherited by this process.
    for name in list(os.environ):
        if name.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(name)
    root = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / ".config" / "gh"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setattr(srv, "_hermes_home", root)
    homes = {"default": root, **{name: root / "profiles" / name for name in ("alpha", "beta")}}
    for name, home in homes.items():
        home.mkdir(parents=True, exist_ok=True)
        raw = {
            "model": {"provider": "openai", "default": f"{name}-selected"},
            "agent": {"reasoning_effort": "high"},
            "display": {"skin": "default"},
            "auxiliary": {
                "vision": {"provider": "openai", "model": "untouched"},
                "model_recommendation": {"provider": "auto", "model": "", "api_key": "${ROUTER_TEST_KEY}"},
            },
        }
        (home / "config.yaml").write_text("# Keep this comment\n" + yaml.safe_dump(raw), encoding="utf-8")
        (home / "state.db").write_bytes(b"session-store-not-to-be-opened")
    return homes


def rpc(method, **params):
    class Transport:
        def __init__(self):
            self.done = threading.Event()
            self.responses = []

        def write(self, response):
            self.responses.append(response)
            self.done.set()
            return True

        def close(self):
            pass

    transport = Transport()
    immediate = srv.dispatch({"id": "preset-test", "method": method, "params": params}, transport=transport)
    if immediate is not None:
        return immediate
    assert transport.done.wait(10), "RPC worker did not reply"
    assert len(transport.responses) == 1
    return transport.responses[0]


def loaded(home):
    token = set_hermes_home_override(home)
    try:
        return load_config()
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("preset", PRESETS)
def test_preset_roundtrip_changes_only_the_selected_profile_setting(profiles, monkeypatch, preset):
    original = {name: read_user_config_raw(home / "config.yaml") for name, home in profiles.items()}
    original_bytes = {name: (home / "config.yaml").read_bytes() for name, home in profiles.items()}
    session = {"model_override": {"provider": "openai", "model": "draft-choice"},
               "create_reasoning_override": {"effort": "low"}, "history": [{"content": "private history"}]}
    before_session = deepcopy(session)
    monkeypatch.setitem(srv._sessions, "draft-session", session)
    # Prime the production loader cache BEFORE writing, so reload proves invalidation too.
    loaded(profiles["alpha"])
    before_files = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}
    response = rpc("config.set", key=KEY, value=preset, profile="alpha", session_id="draft-session",
                   draft="PRIVATE-DRAFT", recommendations=[{"reason": "PRIVATE-ADVICE"}])
    assert response.get("result") == {"key": KEY, "value": preset, "profile": "alpha"}, response
    assert rpc("config.get", key=KEY, profile="alpha")["result"] == {"value": preset, "profile": "alpha"}
    assert loaded(profiles["alpha"])["model_recommendation"]["preset"] == preset
    assert read_user_config_raw(profiles["alpha"] / "config.yaml") == {
        **original["alpha"], "model_recommendation": {"preset": preset}}
    assert "# Keep this comment" in (profiles["alpha"] / "config.yaml").read_text()
    for other in ("default", "beta"):
        assert (profiles[other] / "config.yaml").read_bytes() == original_bytes[other]
    assert session == before_session
    after_files = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}
    changed = {p for p in before_files.keys() | after_files.keys() if before_files.get(p) != after_files.get(p)}
    assert changed == {profiles["alpha"] / "config.yaml"}


@pytest.mark.parametrize("value", [None, "", "Balanced", " balanced ", "other", False, 1, [], {}, {"preset": "balanced"}])
def test_invalid_preset_write_rejects_without_mutating_any_file(profiles, value):
    assert "result" in rpc("config.set", key=KEY, value="save_codex", profile="alpha")
    before = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}
    response = rpc("config.set", key=KEY, value=value, profile="alpha")
    assert response.get("error", {}).get("code") == 4002, response
    assert {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("profile_params", [{}, {"profile": None}, {"profile": ""}, {"profile": "missing"},
                                            {"profile": "../outside"}, {"profile": True}, {"profile": ["alpha"]}])
def test_settings_require_an_explicit_existing_profile_without_launch_fallback(profiles, profile_params):
    before = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}
    for method in ("config.get", "config.set"):
        response = rpc(method, key=KEY, value="best_quality", **profile_params)
        assert "error" in response, response
        assert {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()} == before


def test_explicit_default_and_sibling_profiles_are_independent_of_launch_profile(profiles, monkeypatch):
    monkeypatch.setattr(srv, "_hermes_home", profiles["beta"])
    monkeypatch.setenv("HERMES_HOME", str(profiles["beta"]))
    for profile, preset in zip(("default", "alpha", "beta"), PRESETS):
        assert "result" in rpc("config.set", key=KEY, profile=profile, value=preset)
    for profile, preset in zip(("default", "alpha", "beta"), PRESETS):
        assert rpc("config.get", key=KEY, profile=profile)["result"] == {"value": preset, "profile": profile}
        assert loaded(profiles[profile])["model_recommendation"]["preset"] == preset


@pytest.mark.parametrize("section", [{}, {"preset": "obsolete", "api_key": "PRIVATE-ROUTER-KEY"},
                                      {"preset": None}, {"preset": []}, None, "obsolete"])
def test_reads_default_safely_without_repairing_or_seeding_profile_files(profiles, section):
    path = profiles["alpha"] / "config.yaml"
    raw = read_user_config_raw(path)
    raw["model_recommendation"] = section
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    before = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}
    assert rpc("config.get", key=KEY, profile="alpha").get("result") == {"value": "balanced", "profile": "alpha"}
    assert {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("text", ["model: [PRIVATE-BROKEN", "- PRIVATE-NON-MAPPING", "model_recommendation: PRIVATE-SCALAR\n"])
def test_unwritable_config_is_not_replaced_and_errors_do_not_echo_contents(profiles, text):
    path = profiles["alpha"] / "config.yaml"
    path.write_text(text, encoding="utf-8")
    response = rpc("config.set", key=KEY, profile="alpha", value="balanced")
    assert response.get("error") == {"code": 5001, "message": "Could not save model recommendation preset"}, response
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("section,effective", [
    ({"preset": "best_quality"}, "best_quality"),
    (None, "save_codex"),
    ("PRIVATE-MANAGED-SECTION", "balanced"),
    ({}, "save_codex"),
    # A managed subtree pins ``model_recommendation.preset.future``: the user's scalar preset
    # cannot become effective, so acknowledging a write would be an ineffective write.
    ({"preset": {"future": "pinned"}}, "balanced"),
    ({"preset": {"future": {"deeper": "pinned"}}, "other": "sibling"}, "balanced"),
])
def test_managed_preset_or_parent_rejects_write_without_changing_effective_read(
    profiles, tmp_path, monkeypatch, section, effective,
):
    assert "result" in rpc("config.set", key=KEY, profile="alpha", value="save_codex")
    managed = tmp_path / "managed"
    managed.mkdir()
    managed_path = managed / "config.yaml"
    managed_path.write_text(yaml.safe_dump({"model_recommendation": section}), encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    assert rpc("config.get", key=KEY, profile="alpha")["result"] == {"value": effective, "profile": "alpha"}
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    response = rpc("config.set", key=KEY, profile="alpha", value="balanced")
    readback = rpc("config.get", key=KEY, profile="alpha")
    stored = read_user_config_raw(profiles["alpha"] / "config.yaml")["model_recommendation"]
    assert response.get("error") == {"code": 5001, "message": "Could not save model recommendation preset"}, (
        response, readback, stored)
    assert readback["result"] == {"value": effective, "profile": "alpha"}
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("lock_source", ["environment", "profile_marker"])
def test_package_managed_preset_write_is_rejected_without_mutation(profiles, monkeypatch, lock_source):
    if lock_source == "environment":
        monkeypatch.setenv("HERMES_MANAGED", "true")
    else:
        (profiles["alpha"] / ".managed").write_text("nixos\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}

    response = rpc("config.set", key=KEY, profile="alpha", value="best_quality")
    assert response.get("error") == {"code": 5001, "message": "Could not save model recommendation preset"}, response
    assert rpc("config.get", key=KEY, profile="alpha")["result"] == {"value": "balanced", "profile": "alpha"}
    assert {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("nested_preset", [None, "save_codex"])
def test_literal_dotted_preset_key_rejects_write_without_reinterpreting_config(profiles, nested_preset):
    path = profiles["alpha"] / "config.yaml"
    raw = read_user_config_raw(path)
    raw[KEY] = "PRIVATE-LITERAL-VALUE"
    if nested_preset is not None:
        raw["model_recommendation"] = {"preset": nested_preset}
    path.write_text("# Keep this comment\n" + yaml.safe_dump(raw), encoding="utf-8")
    expected = {"value": nested_preset or "balanced", "profile": "alpha"}
    assert rpc("config.get", key=KEY, profile="alpha")["result"] == expected
    before = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}

    response = rpc("config.set", key=KEY, profile="alpha", value="best_quality")
    readback = rpc("config.get", key=KEY, profile="alpha")
    stored_literal = read_user_config_raw(path)[KEY]
    assert response.get("error") == {"code": 5001, "message": "Could not save model recommendation preset"}, (
        response, readback, stored_literal)
    assert readback["result"] == expected
    assert {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("text", [
    pytest.param(
        "model_recommendation: &shared {preset: balanced}\nunrelated: *shared\n",
        id="sibling-alias-to-target",
    ),
    pytest.param(
        "unrelated: &shared {preset: balanced}\nmodel_recommendation: *shared\n",
        id="target-alias-to-sibling",
    ),
    pytest.param(
        "model_recommendation: &shared {preset: balanced}\nunrelated:\n  nested: *shared\n",
        id="nested-alias",
    ),
    pytest.param(
        "model_recommendation: &shared {preset: balanced}\nunrelated:\n  <<: *shared\n  enabled: true\n",
        id="merge-reference-to-target",
    ),
    pytest.param(
        "unrelated: &defaults\n  model_recommendation: {preset: balanced}\n<<: *defaults\n",
        id="root-merge-supplies-unanchored-target",
    ),
    pytest.param(
        "unrelated: &defaults\n  model_recommendation: {preset: balanced}\n<<: [*defaults]\n",
        id="root-merge-sequence-supplies-unanchored-target",
    ),
    pytest.param(
        "model_recommendation: &unused {preset: balanced}\nunrelated: {enabled: true}\n",
        id="unreferenced-target-anchor",
    ),
    pytest.param(
        "&root\nmodel_recommendation: {preset: balanced}\nunrelated: *root\n",
        id="root-anchor-sibling-alias",
    ),
    pytest.param(
        "&root\nmodel_recommendation: {preset: balanced}\nunrelated:\n  nested: *root\n",
        id="root-anchor-nested-alias",
    ),
    pytest.param(
        "&root\nmodel_recommendation: {preset: balanced}\nunrelated: [*root]\n",
        id="root-anchor-sequence-alias",
    ),
    pytest.param(
        "&root\nmodel_recommendation: {preset: balanced}\nunrelated:\n  <<: *root\n  enabled: true\n",
        id="root-anchor-merge-alias",
    ),
    pytest.param(
        "&root\nmodel_recommendation: {preset: balanced}\nunrelated: {enabled: true}\n",
        id="unreferenced-root-anchor",
    ),
])
def test_shared_or_anchored_preset_target_rejects_save_without_mutating_config(profiles, text):
    path = profiles["alpha"] / "config.yaml"
    path.write_text("# Preserve shared settings\n" + text, encoding="utf-8")
    expected = {"value": "balanced", "profile": "alpha"}
    assert rpc("config.get", key=KEY, profile="alpha")["result"] == expected
    # Aliases to the root make the loaded document self-referential, so compare
    # the serialized effective document rather than recursing through dict equality.
    unrelated = yaml.safe_dump(read_user_config_raw(path)["unrelated"])
    before = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}

    response = rpc("config.set", key=KEY, profile="alpha", value="best_quality")

    # Reload effective YAML: merge references need not retain object identity
    # in PyYAML, but mutating their ruamel source still changes unrelated values.
    assert yaml.safe_dump(read_user_config_raw(path)["unrelated"]) == unrelated, response
    assert response.get("error") == {"code": 5001, "message": "Could not save model recommendation preset"}, response
    assert rpc("config.get", key=KEY, profile="alpha")["result"] == expected
    assert {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("target", [
    pytest.param("model_recommendation: {preset: balanced}\n", id="plain-target"),
    pytest.param("model_recommendation: {preset: balanced}\n<<: *shared\n", id="root-merges-unrelated-anchor"),
    pytest.param("model_recommendation:\n  <<: *shared\n  enabled: true\n", id="target-merges-unrelated-anchor"),
])
def test_unrelated_yaml_aliases_do_not_prevent_an_independent_preset_save(profiles, target):
    path = profiles["alpha"] / "config.yaml"
    text = (
        "# Preserve unrelated aliases\n"
        "unrelated: &shared {preset: save_codex, api_key: '${ROUTER_TEST_KEY}'}\n"
        "sibling: *shared\n"
        "nested:\n  copy: *shared\n"
        "merged:\n  <<: *shared\n  enabled: true\n"
    ) + target
    path.write_text(text, encoding="utf-8")
    original = read_user_config_raw(path)
    before = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}

    response = rpc("config.set", key=KEY, profile="alpha", value="best_quality")

    assert response.get("result") == {"key": KEY, "value": "best_quality", "profile": "alpha"}, response
    assert rpc("config.get", key=KEY, profile="alpha")["result"] == {"value": "best_quality", "profile": "alpha"}
    assert read_user_config_raw(path) == {
        **original, "model_recommendation": {**original["model_recommendation"], "preset": "best_quality"}}
    saved = path.read_text(encoding="utf-8")
    assert "# Preserve unrelated aliases" in saved
    assert "&shared" in saved and "*shared" in saved and "<<:" in saved
    assert "${ROUTER_TEST_KEY}" in saved
    after = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}
    assert {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)} == {path}


def test_missing_preset_and_file_read_as_balanced_without_creating_config(profiles):
    path = profiles["alpha"] / "config.yaml"
    path.unlink()
    before = set(profiles["alpha"].iterdir())
    assert rpc("config.get", key=KEY, profile="alpha")["result"] == {"value": "balanced", "profile": "alpha"}
    assert set(profiles["alpha"].iterdir()) == before
    assert loaded(profiles["alpha"])["model_recommendation"]["preset"] == "balanced"
    assert rpc("model_recommendation.get", profile="alpha", draft="A draft")["result"]["status"] == "unavailable"
    assert not path.exists()


def test_recommendation_keeps_request_policy_and_metadata_privacy_after_preset_save(profiles, monkeypatch):
    import json
    from hermes_fork.model_recommendation import service

    path = profiles["alpha"] / "config.yaml"
    raw = read_user_config_raw(path)
    raw["providers"] = {"anthropic": {"models": ["profile-only-model"]}}
    raw["auxiliary"]["model_recommendation"] = {"provider": "anthropic", "model": "test-router"}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-key")
    # Only outbound boundaries are substituted; discovery, profile binding,
    # request validation, ranking, config reads/writes and dispatch are real.
    monkeypatch.setattr("hermes_fork.account_limits.service.fetch_account_limits", lambda providers: ())
    calls = []

    def router_response(router, messages):
        calls.append((router, messages))
        request = json.loads(messages[1]["content"])
        selected = {}
        for candidate in request["candidates"]:
            selected.setdefault(candidate["provider"], candidate)
        return json.dumps({"task_risk": "low", "ambiguous": False, "recommendations": [
            {"provider": candidate["provider"], "model": candidate["model"], "effort": "none",
             "reason": "Adequate", "quality": 50, "materially_advantageous": False}
            for candidate in selected.values()]})

    monkeypatch.setattr(service, "_run_router_once", router_response)
    assert "result" in rpc("config.set", key=KEY, profile="alpha", value="best_quality")
    loaded(profiles["alpha"])
    before = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}
    draft = " \nUnsent draft 😀\n "
    for policy_params, expected in [*(({"policy": p}, p) for p in PRESETS), ({}, "balanced"),
                                    ({"policy": None}, "balanced"), ({"policy": " SAVE_CODEX "}, "save_codex")]:
        response = rpc("model_recommendation.get", profile="alpha", draft=draft,
                       attachments=[{"name": "brief.pdf", "kind": "file", "path": "/PRIVATE-PATH",
                                     "detail": "PRIVATE-DETAIL", "refText": "PRIVATE-REF", "previewUrl": "PRIVATE-PREVIEW",
                                     "thumbnailUrl": "PRIVATE-THUMB", "bytes": "PRIVATE-BYTES"}, {},
                                    {"name": "a" * 257, "mime_type": None, "size": -1}],
                       transcript="PRIVATE-HISTORY", session_id="PRIVATE-SESSION", hidden_context="PRIVATE-HIDDEN",
                       **policy_params)
        assert response.get("result", {}).get("policy") == expected, response
        router, messages = calls[-1]
        assert router["model"] == "test-router"
        request = json.loads(messages[1]["content"])
        assert request["draft"] == draft
        assert request["attachments"] == [{"name": "brief.pdf", "kind": "file"}, {}, {}]
        assert "PRIVATE-" not in json.dumps(messages)
        assert {candidate["provider"] for candidate in request["candidates"]} == {"anthropic"}
    assert len(calls) == 6
    after = {p: p.read_bytes() for p in profiles["default"].rglob("*") if p.is_file()}
    assert [str(p) for p, content in before.items() if after.get(p) != content] == []
    # Existing inventory discovery may cache provider catalog/auth metadata;
    # it must never persist the unsent draft or recommendation input/output.
    for p, content in after.items():
        assert b"PRIVATE-" not in content and draft.encode() not in content, str(p)
        assert b'"reason": "Adequate"' not in content, str(p)
    assert rpc("config.get", key=KEY, profile="alpha")["result"]["value"] == "best_quality"
    assert rpc("model_recommendation.get", profile="alpha", draft=draft, policy="invalid")["error"]["code"] == 4000
    assert len(calls) == 6
