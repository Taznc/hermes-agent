"""``hermes auth reset`` must report the DURABLE outcome, not the intent.

The 2026-09-07 incident printed ``Reset status on 2 openai-codex credentials``
while both rows stayed exhausted on disk: the count came from the in-memory
clear, which said nothing about what survived persistence.  The command's
report is only meaningful if it is read back from the store.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest


def _store_path(tmp_path):
    return tmp_path / "hermes" / "auth.json"


def _write_pool(tmp_path, provider: str, entries: list[dict]) -> None:
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    _store_path(tmp_path).write_text(
        json.dumps({"version": 1, "credential_pool": {provider: entries}}, indent=2),
        encoding="utf-8",
    )


def _exhausted_row(cred_id: str, *, age_seconds: float, priority: int = 0) -> dict:
    return {
        "id": cred_id,
        "label": cred_id,
        "auth_type": "api_key",
        "priority": priority,
        "source": "manual",
        "access_token": f"sk-{cred_id}",
        "base_url": "https://openrouter.ai/api/v1",
        "last_status": "exhausted",
        "last_status_at": time.time() - age_seconds,
        "last_error_code": 429,
    }


@pytest.fixture
def pool_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return tmp_path


def _run_reset(provider: str = "openrouter"):
    from hermes_cli.auth_commands import auth_reset_command

    auth_reset_command(SimpleNamespace(provider=provider))


def test_reset_reports_the_durable_count(pool_env, capsys):
    _write_pool(
        pool_env,
        "openrouter",
        [
            _exhausted_row("cred-1", age_seconds=60, priority=0),
            _exhausted_row("cred-2", age_seconds=30, priority=1),
        ],
    )

    _run_reset()

    out = capsys.readouterr().out
    assert "Reset status on 2 openrouter credentials" in out


def test_reset_does_not_claim_success_when_persistence_failed(pool_env, monkeypatch, capsys):
    """A row that is still benched on disk must not be counted as reset."""
    _write_pool(pool_env, "openrouter", [_exhausted_row("cred-1", age_seconds=600)])

    from agent import credential_pool as cp

    def _persist_that_does_not_stick(provider, payloads, **kwargs):
        # Stand-in for every way the write fails to land: a concurrent writer
        # wins, the merge guard restores, or the store rejects the write.
        return None

    monkeypatch.setattr(cp, "persist_pool_entries", _persist_that_does_not_stick)

    with pytest.raises(SystemExit) as excinfo:
        _run_reset()

    assert excinfo.value.code != 0
    out = capsys.readouterr().out
    assert "Reset status on 1 openrouter credentials" not in out
    assert "0 of 1" in out


def test_reset_reports_failure_when_the_write_raises(pool_env, monkeypatch, capsys):
    _write_pool(pool_env, "openrouter", [_exhausted_row("cred-1", age_seconds=600)])

    from agent import credential_pool as cp

    def _boom(provider, payloads, **kwargs):
        raise OSError("auth.json is read-only")

    monkeypatch.setattr(cp, "persist_pool_entries", _boom)

    with pytest.raises(SystemExit) as excinfo:
        _run_reset()

    assert excinfo.value.code != 0
    out = capsys.readouterr().out
    assert "Reset status on 1 openrouter credentials" not in out
    assert "read-only" in out


def test_reset_with_nothing_to_clear_is_success(pool_env, capsys):
    _write_pool(
        pool_env,
        "openrouter",
        [
            {
                "id": "cred-1",
                "label": "cred-1",
                "auth_type": "api_key",
                "priority": 0,
                "source": "manual",
                "access_token": "sk-cred-1",
                "base_url": "https://openrouter.ai/api/v1",
            }
        ],
    )

    _run_reset()

    assert "Reset status on 0 openrouter credentials" in capsys.readouterr().out
