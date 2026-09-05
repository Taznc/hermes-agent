"""Regression tests for #11314 — credential-pool rotation vs. fallback.

_pool_may_recover_from_rate_limit() is the hinge between credential-pool
rotation and fallback-provider activation.  Rotation is only worth waiting on
when the pool exists, has an available entry, and has more than one entry to
rotate to; otherwise we should fall back to the configured fallback provider
immediately.

The fix: _pool_may_recover_from_rate_limit must use has_genuinely_available()
(not has_available()) so that a pool whose every entry is quota-exhausted with
a future reset deadline does not report True just because _available_entries()
would revive one via _resync_stale_entry (e.g. re-reading ~/.claude/.credentials.json
for a claude_code-source entry whose tokens were refreshed by another process).
"""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

from agent.credential_pool import (
    CredentialPool,
    PooledCredential,
    STATUS_OK,
    STATUS_EXHAUSTED,
    STATUS_DEAD,
)
from run_agent import _pool_may_recover_from_rate_limit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _exhausted_entry(
    *,
    id: str,
    source: str = "manual",
    access_token: str = "sk-ant-...",
    reset_at: float | None = None,
    status_at: float | None = None,
) -> PooledCredential:
    """Build an exhausted PooledCredential for repellent tests."""
    now = _now()
    return PooledCredential(
        id=id,
        provider="anthropic",
        source=source,
        label=f"key-{id}",
        access_token=access_token,
        auth_type="api_key",
        last_status=STATUS_EXHAUSTED,
        last_status_at=status_at if status_at is not None else now,
        last_error_reset_at=reset_at,
        priority=0,
    )


def _ok_entry(id: str = "ok-1", source: str = "manual") -> PooledCredential:
    return PooledCredential(
        id=id,
        provider="anthropic",
        source=source,
        label=f"key-{id}",
        access_token="sk-ant-ok",
        auth_type="api_key",
        last_status=STATUS_OK,
        priority=0,
    )


# ---------------------------------------------------------------------------
# _pool_may_recover_from_rate_limit — mocked pool
# ---------------------------------------------------------------------------

def _mock_pool(has_genuinely_available: bool, entry_count: int = 2) -> MagicMock:
    p = MagicMock()
    p.has_genuinely_available.return_value = has_genuinely_available
    p.entries.return_value = list(range(entry_count))
    return p


def test_multi_entry_pool_recovers():
    """Multi-entry pool with a genuinely available entry → may recover."""
    assert _pool_may_recover_from_rate_limit(_mock_pool(True, entry_count=3)) is True


def test_exhausted_pool_skips_rotation():
    """Pool whose every entry is exhausted → no rotation, fall back."""
    assert _pool_may_recover_from_rate_limit(_mock_pool(False)) is False


def test_none_pool_skips_rotation():
    assert _pool_may_recover_from_rate_limit(None) is False


def test_single_entry_pool_skips_rotation():
    """Single-credential pool never rotates (same quota), always fall back."""
    assert _pool_may_recover_from_rate_limit(_mock_pool(True, entry_count=1)) is False


# ---------------------------------------------------------------------------
# has_genuinely_available — real CredentialPool, no resync side effects
# ---------------------------------------------------------------------------

def test_has_genuinely_available_empty_pool():
    pool = CredentialPool("anthropic", [])
    assert pool.has_genuinely_available() is False


def test_has_genuinely_available_one_ok_entry():
    pool = CredentialPool("anthropic", [_ok_entry()])
    assert pool.has_genuinely_available() is True


def test_has_genuinely_available_two_exhausted_with_future_reset():
    """Every entry exhausted with a future reset deadline → no availability.

    This is the exact bug scenario: a multi-entry pool where _available_entries()
    (called by the old has_available()) would revive one entry via _resync_stale_entry
    (claude_code source + fresh tokens on disk), but has_genuinely_available() correctly
    returns False because no entry is usable without a side-effect resync.
    """
    future = _now() + 300  # 5 min from now
    e1 = _exhausted_entry(id="e1", reset_at=future)
    e2 = _exhausted_entry(id="e2", reset_at=future)
    pool = CredentialPool("anthropic", [e1, e2])
    assert pool.has_genuinely_available() is False
    # The old gate would have checked has_available() here — which, for manual-source
    # entries, also returns False (no resync source). The bug is specific to resync-capable
    # sources; the regression is that _pool_may_recover_from_rate_limit now uses the
    # side-effect-free getter regardless of source.
    assert pool.has_available() is False  # manual source → no resync → same answer


def test_has_genuinely_available_exhausted_cooldown_elapsed():
    """Exhausted entry whose cooldown has elapsed → genuinely available now."""
    past = _now() - 60  # 1 min ago
    e1 = _exhausted_entry(id="e1", reset_at=past)
    pool = CredentialPool("anthropic", [e1])
    # Cooldown elapsed → entry is effectively available (select will clear it).
    assert pool.has_genuinely_available() is True


def test_has_genuinely_available_mixed_ok_and_exhausted():
    future = _now() + 300
    e1 = _ok_entry(id="ok")
    e2 = _exhausted_entry(id="ex", reset_at=future)
    pool = CredentialPool("anthropic", [e1, e2])
    assert pool.has_genuinely_available() is True


def test_has_genuinely_available_all_dead():
    e1 = PooledCredential(
        id="d1",
        provider="anthropic",
        source="manual",
        label="dead-1",
        access_token="sk-ant-dead",
        auth_type="api_key",
        last_status=STATUS_DEAD,
        priority=0,
    )
    e2 = PooledCredential(
        id="d2",
        provider="anthropic",
        source="manual",
        label="dead-2",
        access_token="sk-ant-dead2",
        auth_type="api_key",
        last_status=STATUS_DEAD,
        priority=0,
    )
    pool = CredentialPool("anthropic", [e1, e2])
    assert pool.has_genuinely_available() is False


def test_has_genuinely_available_dead_plus_exhausted():
    """DEAD entries never re-enter rotation; exhausted with future reset → False."""
    future = _now() + 300
    d1 = PooledCredential(
        id="d1",
        provider="anthropic",
        source="manual",
        label="dead",
        access_token="sk-ant-dead",
        auth_type="api_key",
        last_status=STATUS_DEAD,
        priority=0,
    )
    e1 = _exhausted_entry(id="e1", reset_at=future)
    pool = CredentialPool("anthropic", [d1, e1])
    assert pool.has_genuinely_available() is False


# ---------------------------------------------------------------------------
# Regression: resync-revivable entry fools has_available but not
# has_genuinely_available
# ---------------------------------------------------------------------------

def test_resync_fools_has_available_but_not_has_genuinely():
    """Regression test for the bug: a claude_code-source exhausted entry whose
    on-disk tokens were refreshed by another process would be revived by
    _available_entries() → has_available() returns True — but
    has_genuinely_available() returns False because the entry's in-memory
    state is still exhausted.

    This is the scenario that caused Desktop sessions to wait for pool rotation
    instead of falling back to Codex: both Anthropic credentials were exhausted,
    but has_available() (via _resync_stale_entry) surfaced one as "available"
    because the credentials file had fresh tokens, so _pool_may_recover_from_rate_limit
    returned True and suppressed the eager fallback.

    We simulate this by building a pool with a claude_code-source exhausted entry,
    then monkeypatching _resync_stale_entry to revive it (mimicking a fresh
    credentials file read).  has_available() sees the revived entry; the new
    has_genuinely_available() does not.
    """
    future = _now() + 300
    claude_entry = _exhausted_entry(
        id="cc-1",
        source="claude_code",
        access_token="stale-token",
        reset_at=future,
    )
    pool = CredentialPool("anthropic", [claude_entry])

    # has_genuinely_available reads raw state — exhausted → False.
    assert pool.has_genuinely_available() is False

    # Simulate what _available_entries does: resync revives the entry.
    synced = pool._sync_anthropic_entry_from_credentials_file(claude_entry)
    # If the credentials file had a DIFFERENT token, the entry would be revived.
    # For this test we force the "tokens changed" path by monkeypatching the
    # sync method to return a fresh STATUS_OK entry.
    fresh = PooledCredential(
        id="cc-1",
        provider="anthropic",
        source="claude_code",
        label="cc-1",
        access_token="fresh-token",
        auth_type="api_key",
        last_status=STATUS_OK,
        priority=0,
    )
    pool._sync_anthropic_entry_from_credentials_file = lambda e: fresh  # type: ignore[method-assign]

    # has_available() triggers _available_entries → resync → revived entry → True.
    assert pool.has_available() is True

    # But has_genuinely_available() checks in-memory state without resync,
    # so it still sees the exhausted entry → False.  _pool_may_recover_from_rate_limit
    # uses this path, so fallback fires immediately instead of waiting.
    assert pool.has_genuinely_available() is False
