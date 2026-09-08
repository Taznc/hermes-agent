"""An explicit operator reset must survive the disk/newer-state merge guard.

``hermes auth reset <provider>`` clears cooldown metadata in memory and
persists.  ``_merge_disk_cooldown_state`` exists to stop a stale in-memory
snapshot from resurrecting a credential another process just benched — but the
reset blanks ``last_status_at`` to ``None`` (epoch 0.0), so the guard read every
reset as the stalest possible write and restored the very cooldown the operator
asked to clear.  Observed 2026-09-07: ``Reset status on 2 openai-codex
credentials`` printed while both rows stayed exhausted on disk.

The reset is authoritative for cooldowns recorded up to the moment it ran, and
only for those: a 429 stamped AFTER the reset boundary is genuinely newer
information and must still win.
"""

from __future__ import annotations

import json
import time

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


def _read_pool(tmp_path, provider: str) -> list[dict]:
    payload = json.loads(_store_path(tmp_path).read_text(encoding="utf-8"))
    return payload["credential_pool"][provider]


def _exhausted_row(cred_id: str, *, age_seconds: float, priority: int = 0) -> dict:
    """A 429-exhausted row still inside its cooldown TTL."""
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


def _load(provider: str = "openrouter"):
    from agent.credential_pool import load_pool

    return load_pool(provider)


def test_reset_clears_cooldown_that_is_newer_on_disk(pool_env):
    """AC1: cleared state must persist even though the disk row is 'newer'.

    Both rows are exhausted on disk with a recent ``last_status_at``.  After
    ``reset_statuses()`` the in-memory rows have ``last_status_at=None``, so the
    merge guard sees disk-newer-than-memory for every row — the exact shape that
    silently restored the cooldown.
    """
    _write_pool(
        pool_env,
        "openrouter",
        [
            _exhausted_row("cred-1", age_seconds=60, priority=0),
            _exhausted_row("cred-2", age_seconds=30, priority=1),
        ],
    )

    pool = _load()
    assert pool.has_available() is False, "precondition: both rows benched"

    assert pool.reset_statuses() == 2

    on_disk = {row["id"]: row for row in _read_pool(pool_env, "openrouter")}
    assert set(on_disk) == {"cred-1", "cred-2"}
    for cred_id, row in on_disk.items():
        assert row["last_status"] is None, f"{cred_id} cooldown restored on disk"
        assert row["last_status_at"] is None
        assert row["last_error_code"] is None

    reloaded = _load()
    assert reloaded.has_available() is True
    assert [e.last_status for e in reloaded.entries()] == [None, None]


def test_reset_does_not_erase_a_cooldown_recorded_after_the_reset_boundary(pool_env, monkeypatch):
    """AC2: the reset is authoritative only up to the instant it ran.

    Simulates the real race: another process benches a credential with a 429
    while this process is between clearing its rows and writing them out.  That
    cooldown is genuinely newer than the operator's intent and must survive.
    """
    _write_pool(pool_env, "openrouter", [_exhausted_row("cred-1", age_seconds=600)])
    pool = _load()

    from agent import credential_pool as cp

    real_persist = cp.persist_pool_entries

    def _persist_with_concurrent_429(provider, payloads, **kwargs):
        # A second process stamps a fresh 429 AFTER our reset boundary but
        # BEFORE our write lands.
        _write_pool(pool_env, provider, [_exhausted_row("cred-1", age_seconds=0)])
        return real_persist(provider, payloads, **kwargs)

    monkeypatch.setattr(cp, "persist_pool_entries", _persist_with_concurrent_429)

    report = pool.reset_statuses_report()
    # The operator asked to clear one row; the newer 429 legitimately kept it
    # benched, and the report says so rather than claiming success.
    assert (report.requested, report.cleared) == (1, 0)
    assert report.ok is False

    row = _read_pool(pool_env, "openrouter")[0]
    assert row["last_status"] == "exhausted", "a post-reset 429 was erased"
    assert row["last_error_code"] == 429
    assert _load().has_available() is False


def test_ordinary_write_still_loses_to_a_newer_disk_cooldown(pool_env, monkeypatch):
    """AC2: non-reset writes keep the pre-existing newer-disk-wins guard.

    ``reset_at=None`` is the ordinary path; a concurrent process's fresh
    cooldown must still beat this process's stale in-memory snapshot.
    """
    _write_pool(pool_env, "openrouter", [_exhausted_row("cred-1", age_seconds=600)])
    pool = _load()
    pool.reset_statuses()  # clear, so the in-memory row is status-free

    # Another process benches the credential; then an ORDINARY persist runs.
    _write_pool(pool_env, "openrouter", [_exhausted_row("cred-1", age_seconds=0)])
    pool._persist()

    row = _read_pool(pool_env, "openrouter")[0]
    assert row["last_status"] == "exhausted"
    assert row["last_error_code"] == 429


def test_reset_clears_a_borrowed_root_grant_cooldown(tmp_path, monkeypatch):
    """The incident provider's real path: a profile borrowing the root's rows.

    ``openai-codex`` is a single-use-refresh provider, so a named profile with no
    local rows persists through ``_update_root_pool_rows`` against the ROOT store,
    not ``write_credential_pool``. The reset boundary has to reach that path too.
    """
    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    (tmp_path / "fakehome").mkdir()
    # Keep the host's real ~/.hermes out of the picture; the write-through's
    # pytest seat belt compares the resolved global path against $HOME/.hermes.
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    (root / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    # Sole credential: a 429 gets the short cooldown cap, so keep
                    # this well inside it or the precondition ages out on its own.
                    "openai-codex": [_exhausted_row("codex-1", age_seconds=5)]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (profile_home / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {}}, indent=2), encoding="utf-8"
    )

    from agent import credential_pool as cp

    assert cp._profile_owns_pool_provider("openai-codex") is False, (
        "precondition: the profile must be BORROWING the root's rows"
    )
    pool = cp.load_pool("openai-codex")
    assert cp._borrowed_single_use_pool_root() is not None, (
        "precondition: writes must route to the root store"
    )
    assert pool.has_available() is False

    report = pool.reset_statuses_report()
    assert (report.requested, report.cleared) == (1, 1)
    assert report.ok is True

    root_rows = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    row = root_rows["credential_pool"]["openai-codex"][0]
    assert row["last_status"] is None, "root-grant cooldown was restored"
    assert row["last_error_code"] is None
    assert cp.load_pool("openai-codex").has_available() is True
