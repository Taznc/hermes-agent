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


def _healthy_row(cred_id: str, *, priority: int = 0) -> dict:
    """A row with no cooldown/error metadata at all."""
    return {
        "id": cred_id,
        "label": cred_id,
        "auth_type": "api_key",
        "priority": priority,
        "source": "manual",
        "access_token": f"sk-{cred_id}",
        "base_url": "https://openrouter.ai/api/v1",
    }


def _exhausted_row(cred_id: str, *, age_seconds: float, priority: int = 0) -> dict:
    """A 429-exhausted row still inside its cooldown TTL."""
    return {
        **_healthy_row(cred_id, priority=priority),
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


def test_a_process_running_before_the_reset_cannot_resurrect_the_cooldown(pool_env):
    """The reset must hold against writers that were ALREADY running.

    This is the card's literal goal ("while other Hermes processes are
    running").  A long-lived process loaded the exhausted row before the
    operator reset; its next ordinary ``_persist()`` carries that pre-reset
    snapshot.  Nothing about that write is newer information — the operator
    superseded it — so it must not put the credential back in cooldown.
    """
    _write_pool(
        pool_env,
        "openrouter",
        [
            _exhausted_row("cred-1", age_seconds=60, priority=0),
            _exhausted_row("cred-2", age_seconds=30, priority=1),
        ],
    )

    already_running = _load()  # holds the pre-reset exhausted snapshot
    assert already_running.has_available() is False

    assert _load().reset_statuses_report().ok is True
    assert [r["last_status"] for r in _read_pool(pool_env, "openrouter")] == [None, None]

    already_running._persist()  # an ordinary write, later, from the stale process

    for row in _read_pool(pool_env, "openrouter"):
        assert row["last_status"] is None, "a pre-reset writer resurrected the cooldown"
        assert row["last_error_code"] is None
    assert _load().has_available() is True


def test_reset_sees_a_cooldown_that_landed_after_this_process_loaded(pool_env):
    """The reset must not decide what to clear from a stale in-memory snapshot.

    The operator's shell loads a healthy pool; before ``reset`` runs, a live
    Hermes benches the credential with a 429 and persists it.  Deciding
    "nothing to clear" from the in-memory rows would return success while the
    credential is still durably benched — the reset silently doing nothing,
    which is the whole complaint.
    """
    healthy = _healthy_row("cred-1")
    _write_pool(pool_env, "openrouter", [healthy])

    reset_pool = _load()  # loaded BEFORE the 429 lands
    assert reset_pool.has_available() is True

    _write_pool(pool_env, "openrouter", [_exhausted_row("cred-1", age_seconds=0)])

    report = reset_pool.reset_statuses_report()

    assert (report.requested, report.cleared) == (1, 1)
    assert report.ok is True
    row = _read_pool(pool_env, "openrouter")[0]
    assert row["last_status"] is None, "a cooldown durable at reset time was not cleared"
    assert row["last_error_code"] is None
    assert _load().has_available() is True


def test_a_bench_recorded_after_the_reset_still_sticks(pool_env):
    """The durable floor must not become a permanent do-not-bench flag.

    A reset floor that suppressed cooldowns forever would let a genuinely
    rate-limited credential be hammered indefinitely, which is exactly the
    failure the merge guard exists to prevent.  After a reset, the very next
    429 is newer than the floor and must persist normally.
    """
    _write_pool(
        pool_env,
        "openrouter",
        [
            _exhausted_row("cred-1", age_seconds=60, priority=0),
            _exhausted_row("cred-2", age_seconds=30, priority=1),
        ],
    )
    assert _load().reset_statuses_report().ok is True
    assert _load().has_available() is True

    # A live Hermes benches cred-1 again, AFTER the reset.
    working = _load()
    entry = next(e for e in working.entries() if e.id == "cred-1")
    working._mark_exhausted(entry, 429)

    row = next(r for r in _read_pool(pool_env, "openrouter") if r["id"] == "cred-1")
    assert row["last_status"] == "exhausted", "the reset floor suppressed a later 429"
    assert row["last_error_code"] == 429

    # And it survives an unrelated later write from another process.
    _load()._persist()
    row = next(r for r in _read_pool(pool_env, "openrouter") if r["id"] == "cred-1")
    assert row["last_status"] == "exhausted"


def test_reset_clears_a_credential_this_process_never_loaded(pool_env):
    """A reset covers every row of the provider, not just the ones in memory.

    Another Hermes added (or re-added) a credential after the operator's shell
    loaded its pool, and that credential is benched.  It is absent from
    ``self._entries``, so it rides the write as a disk-only row — and
    ``hermes auth reset <provider>`` promises the provider, not a snapshot.
    """
    _write_pool(pool_env, "openrouter", [_exhausted_row("cred-1", age_seconds=60)])
    reset_pool = _load()  # only ever sees cred-1

    _write_pool(
        pool_env,
        "openrouter",
        [
            _exhausted_row("cred-1", age_seconds=60, priority=0),
            _exhausted_row("cred-2", age_seconds=45, priority=1),  # added meanwhile
        ],
    )

    report = reset_pool.reset_statuses_report()

    on_disk = {row["id"]: row for row in _read_pool(pool_env, "openrouter")}
    assert set(on_disk) == {"cred-1", "cred-2"}, "the unknown row must not be dropped"
    assert on_disk["cred-2"]["last_status"] is None, "a row absent from memory stayed benched"
    assert on_disk["cred-1"]["last_status"] is None
    assert (report.requested, report.cleared) == (2, 2)
    assert _load().has_available() is True


def test_the_reset_floor_survives_unrelated_auth_store_writes(pool_env):
    """The floor is only durable if other auth.json writers preserve it.

    Every writer read-modify-writes the whole store, so an unrelated command
    (adding a credential for another provider, an OAuth token refresh) must
    carry the floor through — otherwise reset authority silently expires at
    the next unrelated write and the resurrection bug comes back.
    """
    _write_pool(
        pool_env,
        "openrouter",
        [
            _exhausted_row("cred-1", age_seconds=60, priority=0),
            _exhausted_row("cred-2", age_seconds=30, priority=1),
        ],
    )
    already_running = _load()
    assert _load().reset_statuses_report().ok is True

    from hermes_cli.auth import write_credential_pool

    # An unrelated provider's pool is written afterwards.
    write_credential_pool("anthropic", [_healthy_row("other-1")])

    already_running._persist()  # the pre-reset process finally writes

    for row in _read_pool(pool_env, "openrouter"):
        assert row["last_status"] is None, "an unrelated write dropped the reset floor"


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
