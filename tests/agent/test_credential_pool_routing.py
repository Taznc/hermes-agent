# ---------------------------------------------------------------------------
# 8. Integration: real CredentialPool — exhausted multi-entry pool must fall
#    back to Codex immediately, not wait for pool rotation
# ---------------------------------------------------------------------------

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.credential_pool import (
    CredentialPool,
    PooledCredential,
    STATUS_OK,
    STATUS_EXHAUSTED,
    STATUS_DEAD,
    load_pool,
)
from run_agent import _pool_may_recover_from_rate_limit


class TestExhaustedPoolFallbackIntegration:
    """Prove the real recovery decision (not a mocked has_available boolean):
    when every entry in a multi-entry credential pool is quota-exhausted with a
    future reset deadline, _pool_may_recover_from_rate_limit returns False so
    the turn-recovery handler activates the cross-provider fallback immediately.

    Uses the real CredentialPool + has_genuinely_available(), not a mocked
    has_available, because the bug is that has_available() can return True for
    a fully-exhausted pool when _available_entries() revives one entry via
    _resync_stale_entry (e.g. re-reading ~/.claude/.credentials.json for a
    claude_code-source entry whose tokens were refreshed by another process).
    """

    def _mk_exhausted_claude_code_entry(
        self, *, id: str, reset_at: float | None = None
    ) -> PooledCredential:
        """A claude_code-source entry whose tokens may be revivable by resync."""
        now = time.time()
        return PooledCredential(
            id=id,
            provider="anthropic",
            source="claude_code",
            label=f"cc-{id}",
            access_token="stale-token",
            auth_type="api_key",
            last_status=STATUS_EXHAUSTED,
            last_status_at=now,
            last_error_reset_at=reset_at if reset_at is not None else now + 300,
            priority=0,
        )

    def test_exhausted_multientry_pool_fires_fallback_immediately(
        self, tmp_path, monkeypatch
    ):
        """2 claude_code-source exhausted entries → fallback fires (no rotation wait).

        Regression for the reported Desktop hang: both Anthropic credentials were
        rate-limited (429) with reset deadlines, but the session stayed at
        ``error retry backoff (1/3)`` instead of falling back to Codex because
        _pool_may_recover_from_rate_limit returned True.  The old gate used
        has_available(), which calls _available_entries() → _resync_stale_entry
        and can surface a revived entry; the new gate uses has_genuinely_available(),
        which reads raw entry state without side effects.
        """
        future = time.time() + 300  # 5 min from now
        e1 = PooledCredential(
            id="cc-1",
            provider="anthropic",
            source="claude_code",
            label="cc-1",
            access_token="stale-1",
            auth_type="api_key",
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
            last_error_reset_at=future,
            priority=0,
        )
        e2 = PooledCredential(
            id="cc-2",
            provider="anthropic",
            source="claude_code",
            label="cc-2",
            access_token="stale-2",
            auth_type="api_key",
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
            last_error_reset_at=future,
            priority=1,
        )
        pool = CredentialPool("anthropic", [e1, e2])

        # REAL check: has_genuinely_available is side-effect-free → False.
        assert pool.has_genuinely_available() is False

        # The old has_available() would also be False here (claude_code-source
        # entries ARE resynced, but the credentials file has stale tokens, so
        # resync doesn't revive them).  The bug is specific to the case where
        # another process wrote fresh tokens between the 429 and the check.
        decision = _pool_may_recover_from_rate_limit(pool)
        assert decision is False, (
            "multi-entry pool where every entry is exhausted must not wait for "
            "rotation — fall back to the configured cross-provider fallback immediately"
        )

    def test_has_available_can_be_fooled_by_resync_but_gate_is_not(
        self, tmp_path, monkeypatch
    ):
        """Regression: simulate the exact resync-revive path and prove the gate
        still returns False.

        _resync_stale_entry for claude_code-source entries re-reads
        ~/.claude/.credentials.json.  If another process wrote fresh tokens
        (user re-authed elsewhere), the exhausted entry is revived to STATUS_OK
        inside has_available() → old gate returns True → fallback suppressed.

        We monkeypatch _sync_anthropic_entry_from_credentials_file to return a
        fresh STATUS_OK entry, mimicking that path.  has_available() sees the
        revived entry; has_genuinely_available() does not (raw state check);
        _pool_may_recover_from_rate_limit uses the latter.
        """
        future = time.time() + 300
        e1 = PooledCredential(
            id="cc-1",
            provider="anthropic",
            source="claude_code",
            label="cc-1",
            access_token="stale",
            auth_type="api_key",
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
            last_error_reset_at=future,
            priority=0,
        )
        pool = CredentialPool("anthropic", [e1])

        # Single credential → no rotation possible anyway.
        assert _pool_may_recover_from_rate_limit(pool) is False

        # Now prove the resync-revive distinction on a 2-entry pool.
        e2 = PooledCredential(
            id="cc-2",
            provider="anthropic",
            source="claude_code",
            label="cc-2",
            access_token="stale-2",
            auth_type="api_key",
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
            last_error_reset_at=future,
            priority=1,
        )
        pool = CredentialPool("anthropic", [e1, e2])
        assert len(pool.entries()) == 2

        # Mimic a concurrent re-auth: monkeypatch the sync method to return a
        # fresh STATUS_OK entry, as _available_entries would when the credentials
        # file has a new token.
        fresh = PooledCredential(
            id="cc-1",
            provider="anthropic",
            source="claude_code",
            label="cc-1",
            access_token="fresh-from-another-process",
            auth_type="api_key",
            last_status=STATUS_OK,
            priority=0,
        )
        pool._sync_anthropic_entry_from_credentials_file = (  # type: ignore[method-assign]
            lambda e: fresh
        )

        # has_available() triggers _available_entries → resync → revived entry.
        assert pool.has_available() is True

        # has_genuinely_available() reads raw state WITHOUT resync → still sees
        # cc-1 as exhausted (the entry object itself was not mutated).
        assert pool.has_genuinely_available() is False

        # The gate uses has_genuinely_available, so fallback fires.
        assert _pool_may_recover_from_rate_limit(pool) is False

    def test_healthy_multientry_pool_does_not_fallback(self, tmp_path, monkeypatch):
        """When at least one entry is genuinely usable, rotation is worth waiting on."""
        e1 = PooledCredential(
            id="ok-1",
            provider="anthropic",
            source="manual",
            label="ok-1",
            access_token="healthy-key",
            auth_type="api_key",
            last_status=STATUS_OK,
            priority=0,
        )
        e2 = PooledCredential(
            id="ex-2",
            provider="anthropic",
            source="manual",
            label="ex-2",
            access_token="exhausted-key",
            auth_type="api_key",
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
            last_error_reset_at=time.time() + 300,
            priority=1,
        )
        pool = CredentialPool("anthropic", [e1, e2])
        assert pool.has_genuinely_available() is True
        assert _pool_may_recover_from_rate_limit(pool) is True
