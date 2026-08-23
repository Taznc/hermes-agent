from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.proxy import cli as proxy_cli
from hermes_cli.proxy.adapters import ADAPTERS, get_adapter
from hermes_cli.proxy.adapters.base import UpstreamCredential
from hermes_cli.proxy.adapters.codex import OpenAICodexAdapter
from hermes_cli.proxy.cli import _read_client_auth_token, cmd_proxy_start
from hermes_cli.proxy.server import create_app, is_loopback_host, run_server


def _jwt_with_account(account_id: str = "acct-123") -> str:
    payload = {
        "exp": 4_102_444_800,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"header.{encoded}.signature"


def _entry(account_id: str = "acct-123"):
    return SimpleNamespace(
        runtime_api_key=_jwt_with_account(account_id),
        runtime_base_url="https://chatgpt.com/backend-api/codex",
        base_url="https://chatgpt.com/backend-api/codex",
        expires_at="2099-01-01T00:00:00Z",
    )


def test_registry_exposes_one_canonical_codex_adapter_and_alias():
    assert ADAPTERS["openai-codex"] is OpenAICodexAdapter
    assert "codex" not in ADAPTERS
    assert isinstance(get_adapter("openai-codex"), OpenAICodexAdapter)
    assert isinstance(get_adapter("codex"), OpenAICodexAdapter)


def test_codex_loopback_host_validation_and_cli_rejection(capsys):
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.5")
    assert not is_loopback_host("example.com")

    adapter = OpenAICodexAdapter()
    args = SimpleNamespace(provider="codex", host="0.0.0.0", port=8645)
    with (
        patch("hermes_cli.proxy.cli.get_adapter", return_value=adapter),
        patch.object(adapter, "is_authenticated", return_value=True),
        patch("hermes_cli.proxy.cli.run_server") as run,
    ):
        assert cmd_proxy_start(args) == 2
    run.assert_not_called()
    assert "loopback-only" in capsys.readouterr().err


def test_codex_cli_requires_owner_only_client_auth_file(tmp_path, capsys):
    adapter = OpenAICodexAdapter()
    args = SimpleNamespace(
        provider="codex",
        host="127.0.0.1",
        port=8645,
        auth_token_file=None,
    )
    with (
        patch("hermes_cli.proxy.cli.get_adapter", return_value=adapter),
        patch.object(adapter, "is_authenticated", return_value=True),
        patch("hermes_cli.proxy.cli.run_server") as run,
    ):
        assert cmd_proxy_start(args) == 2
    run.assert_not_called()
    assert "--auth-token-file" in capsys.readouterr().err

    token_file = tmp_path / "proxy-token"
    token_file.write_text("owner-client-secret\n", encoding="utf-8")
    if os.name == "nt":
        _set_windows_client_auth_acl(token_file, allow_everyone=False)
    else:
        token_file.chmod(0o644)
        with pytest.raises(ValueError, match="owner-only"):
            _read_client_auth_token(str(token_file))
        token_file.chmod(0o600)

    assert _read_client_auth_token(str(token_file)) == "owner-client-secret"


def test_windows_client_auth_acl_policy_rejects_other_owners_and_allowed_sids():
    allowed = {"S-1-5-18", "S-1-5-21-owner"}

    with pytest.raises(ValueError, match="wrong owner"):
        proxy_cli._validate_windows_owner_only_acl(
            owner_sid="S-1-5-21-other",
            allowed_sids=allowed,
            allowed_aces=[],
        )

    with pytest.raises(ValueError, match="permissive DACL"):
        proxy_cli._validate_windows_owner_only_acl(
            owner_sid="S-1-5-21-owner",
            allowed_sids=allowed,
            allowed_aces=[(0x1, "S-1-1-0")],
        )

    proxy_cli._validate_windows_owner_only_acl(
        owner_sid="S-1-5-21-owner",
        allowed_sids=allowed,
        allowed_aces=[(0x1, "S-1-5-21-owner"), (0x1, "S-1-5-18")],
    )


def _set_windows_client_auth_acl(path: Path, *, allow_everyone: bool) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows ACL fixtures require native Windows.")

    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    system = win32security.ConvertStringSidToSid("S-1-5-18")
    acl = win32security.ACL()
    for sid in (owner, system):
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            0,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    if allow_everyone:
        everyone = win32security.ConvertStringSidToSid("S-1-1-0")
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            0,
            ntsecuritycon.FILE_GENERIC_READ,
            everyone,
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(owner, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED,
        win32security.SE_DACL_PROTECTED,
    )
    win32security.SetFileSecurity(
        str(path),
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
        descriptor,
    )


@pytest.mark.windows_only
def test_windows_client_auth_file_rejects_broadly_readable_dacl(tmp_path):
    token_file = tmp_path / "proxy-token-broad"
    token_file.write_text("owner-client-secret\n", encoding="utf-8")
    _set_windows_client_auth_acl(token_file, allow_everyone=True)

    with pytest.raises(ValueError, match="permissive DACL"):
        _read_client_auth_token(str(token_file))


@pytest.mark.windows_only
def test_windows_client_auth_file_accepts_current_user_and_system_only(tmp_path):
    token_file = tmp_path / "proxy-token-private"
    token_file.write_text("owner-client-secret\n", encoding="utf-8")
    _set_windows_client_auth_acl(token_file, allow_everyone=False)

    assert _read_client_auth_token(str(token_file)) == "owner-client-secret"


def test_codex_cli_passes_client_authority_without_logging_it(tmp_path, capsys):
    token_file = tmp_path / "proxy-token"
    token_file.write_text("owner-client-secret\n", encoding="utf-8")
    if os.name == "nt":
        _set_windows_client_auth_acl(token_file, allow_everyone=False)
    else:
        token_file.chmod(0o600)

    adapter = OpenAICodexAdapter()
    captured = {}

    async def fake_run_server(adapter_arg, **kwargs):
        captured["adapter"] = adapter_arg
        captured.update(kwargs)

    args = SimpleNamespace(
        provider="codex",
        host="127.0.0.1",
        port=18645,
        auth_token_file=str(token_file),
    )
    with (
        patch("hermes_cli.proxy.cli.get_adapter", return_value=adapter),
        patch.object(adapter, "is_authenticated", return_value=True),
        patch("hermes_cli.proxy.cli.run_server", side_effect=fake_run_server),
    ):
        assert cmd_proxy_start(args) == 0

    assert captured == {
        "adapter": adapter,
        "host": "127.0.0.1",
        "port": 18645,
        "client_auth_token": "owner-client-secret",
    }
    assert "owner-client-secret" not in capsys.readouterr().err


def test_client_auth_file_rejects_symlink_and_empty_file(tmp_path):
    empty = tmp_path / "empty-token"
    empty.write_text("\n", encoding="utf-8")
    if os.name == "nt":
        _set_windows_client_auth_acl(empty, allow_everyone=False)
    else:
        empty.chmod(0o600)
    with pytest.raises(ValueError, match="empty"):
        _read_client_auth_token(str(empty))

    multiline = tmp_path / "multiline-token"
    multiline.write_text("first\nsecond\n", encoding="utf-8")
    if os.name == "nt":
        _set_windows_client_auth_acl(multiline, allow_everyone=False)
    else:
        multiline.chmod(0o600)
    with pytest.raises(ValueError, match="without whitespace"):
        _read_client_auth_token(str(multiline))

    target = tmp_path / "real-token"
    target.write_text("secret", encoding="utf-8")
    if os.name == "nt":
        _set_windows_client_auth_acl(target, allow_everyone=False)
    else:
        target.chmod(0o600)
    link = tmp_path / "token-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(ValueError, match="regular file"):
        _read_client_auth_token(str(link))


def test_codex_server_rejects_non_loopback_programmatic_bind():
    adapter = OpenAICodexAdapter()
    try:
        asyncio.run(run_server(adapter, host="0.0.0.0", port=0))
    except RuntimeError as exc:
        assert "loopback-only" in str(exc)
    else:
        raise AssertionError("programmatic non-loopback Codex bind was accepted")


def test_codex_server_requires_client_authority_programmatically():
    adapter = OpenAICodexAdapter()
    with pytest.raises(RuntimeError, match="client authentication"):
        create_app(adapter)


def test_codex_adapter_declares_client_auth_requirement():
    assert OpenAICodexAdapter().requires_client_auth is True


def test_codex_adapter_authentication_is_pool_backed():
    pool = MagicMock()
    pool.has_available.return_value = True
    with patch("hermes_cli.proxy.adapters.codex.load_pool", return_value=pool):
        assert OpenAICodexAdapter().is_authenticated() is True


def test_codex_adapter_selects_responses_credential_and_required_headers():
    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.select.return_value = _entry()
    adapter = OpenAICodexAdapter()
    with patch("hermes_cli.proxy.adapters.codex.load_pool", return_value=pool):
        credential = adapter.get_credential()

    assert credential.bearer == _jwt_with_account()
    assert credential.base_url == "https://chatgpt.com/backend-api/codex"
    assert adapter.allowed_paths == frozenset({"/responses", "/models"})
    assert adapter.get_upstream_headers(credential) == {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
        "originator": "codex_cli_rs",
        "ChatGPT-Account-ID": "acct-123",
    }


def test_codex_adapter_rejects_untrusted_upstream_before_returning_bearer():
    pool = MagicMock()
    pool.has_credentials.return_value = True
    bad = _entry()
    bad.runtime_base_url = "http://127.0.0.1:9999/steal"
    bad.base_url = "http://127.0.0.1:9999/steal"
    pool.select.return_value = bad
    adapter = OpenAICodexAdapter()
    with patch("hermes_cli.proxy.adapters.codex.load_pool", return_value=pool):
        try:
            adapter.get_credential()
        except RuntimeError as exc:
            assert "untrusted" in str(exc).lower()
        else:
            raise AssertionError("untrusted Codex upstream was accepted")


def test_codex_owned_account_header_survives_missing_jwt_claim_as_owned_only():
    adapter = OpenAICodexAdapter()
    credential = cast(
        UpstreamCredential,
        SimpleNamespace(bearer="malformed-token"),
    )
    headers = adapter.get_upstream_headers(credential)
    assert "ChatGPT-Account-ID" not in headers
    assert "ChatGPT-Account-ID" in adapter.get_owned_upstream_header_names()


def test_codex_adapter_401_refreshes_matching_credential():
    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.select.return_value = _entry("first")
    pool.try_refresh_matching.return_value = _entry("refreshed")
    adapter = OpenAICodexAdapter()
    with patch("hermes_cli.proxy.adapters.codex.load_pool", return_value=pool):
        failed = adapter.get_credential()
        retry = adapter.get_retry_credential(
            failed_credential=failed,
            status_code=401,
        )

    assert retry is not None
    assert retry.bearer == _jwt_with_account("refreshed")
    pool.try_refresh_matching.assert_called_once_with(api_key_hint=failed.bearer)
    pool.mark_exhausted_and_rotate.assert_not_called()


def test_codex_adapter_reuses_concurrently_refreshed_current_credential():
    pool = MagicMock()
    refreshed = _entry("new-token")
    pool.try_refresh_matching.side_effect = [refreshed, None]
    pool.select.return_value = refreshed
    adapter = OpenAICodexAdapter()
    adapter._pool = pool
    failed = cast(
        UpstreamCredential,
        SimpleNamespace(bearer=_jwt_with_account("old-token")),
    )

    first = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=401,
    )
    second = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=401,
    )

    assert first is not None and first.bearer == refreshed.runtime_api_key
    assert second is not None and second.bearer == refreshed.runtime_api_key
    pool.mark_exhausted_and_rotate.assert_not_called()


def test_codex_adapter_concurrent_401_refresh_is_serialized():
    pool = MagicMock()
    in_flight = threading.Event()
    overlap = threading.Event()
    counter = {"n": 0}

    def refresh(*, api_key_hint):
        _ = api_key_hint
        if in_flight.is_set():
            overlap.set()
        in_flight.set()
        try:
            time.sleep(0.03)
            counter["n"] += 1
            return _entry(f"refreshed-{counter['n']}")
        finally:
            in_flight.clear()

    pool.try_refresh_matching.side_effect = refresh
    adapter = OpenAICodexAdapter()
    adapter._pool = pool
    failed = cast(
        UpstreamCredential,
        SimpleNamespace(bearer=_jwt_with_account("failed")),
    )
    results = []

    def worker():
        results.append(
            adapter.get_retry_credential(
                failed_credential=failed,
                status_code=401,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 3
    assert not overlap.is_set()


def test_codex_adapter_429_marks_failed_credential_and_rotates():
    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.select.return_value = _entry("first")
    pool.mark_exhausted_and_rotate.return_value = _entry("second")
    adapter = OpenAICodexAdapter()
    with patch("hermes_cli.proxy.adapters.codex.load_pool", return_value=pool):
        failed = adapter.get_credential()
        retry = adapter.get_retry_credential(
            failed_credential=failed,
            status_code=429,
        )

    assert retry is not None
    assert retry.bearer == _jwt_with_account("second")
    pool.mark_exhausted_and_rotate.assert_called_once_with(
        status_code=429,
        api_key_hint=failed.bearer,
    )
    pool.try_refresh_matching.assert_not_called()
