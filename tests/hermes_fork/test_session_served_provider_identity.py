"""Configured-vs-served provider identity on compact session rows (Phase 2.13).

``list_sessions_rich`` (and its compression-tip-projection helper,
``_get_session_rich_rows_batch``) stamp each row with ``configured_provider``
(what the NEXT turn/resume would use, via ``session_gateway_runtime``) and
``served_model``/``served_provider`` (what actually served the session's most
RECENT completed turn, from ``session_model_usage`` — not the legacy
first-call-only ``sessions.billing_provider`` column). These three fields are
the Desktop sidebar's whole data contract for Phase 2.13; this file locks in
the query-branch coverage (the CTE ``order_by_last_active`` path, the plain
path, and the pinned back-fill path all wire the same columns) plus the core
"latest wins, not first" behavior that motivated the ``session_model_usage``
join in the first place.
"""

from __future__ import annotations

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _row(rows, session_id):
    matches = [r for r in rows if r["id"] == session_id]
    assert len(matches) == 1, f"expected exactly one row for {session_id!r}, got {matches}"
    return matches[0]


def test_configured_provider_resolves_from_gateway_runtime(db):
    """A session with an explicit /model override reports that as configured,
    regardless of what actually served earlier turns."""
    db.create_session(session_id="s1", source="cli", model="anthropic/claude-opus-4.8")
    db.update_session_billing_route(
        "s1", provider="anthropic", base_url=None, billing_mode="oauth"
    )

    rows = db.list_sessions_rich(compact_rows=True)
    row = _row(rows, "s1")
    assert row["configured_provider"] == "anthropic"


def test_served_provider_reflects_latest_call_not_first(db):
    """The core gap this feature closes: sessions.billing_provider is
    first-call-only, so a mid-conversation fallback must be read from
    session_model_usage instead — the LATEST route, not the first."""
    db.create_session(session_id="s1", source="cli", model="anthropic/claude-opus-4.8")
    db.update_session_billing_route(
        "s1", provider="anthropic", base_url=None, billing_mode="oauth"
    )
    # First accounted call — establishes the (stale) sessions-row route.
    db.update_token_counts(
        "s1", input_tokens=100, output_tokens=50,
        model="anthropic/claude-opus-4.8", billing_provider="anthropic",
        api_call_count=1,
    )
    db.flush_token_counts()

    legacy_row = db.get_session("s1")
    assert legacy_row["billing_provider"] == "anthropic"

    # A silent mid-conversation fallback: a later accounted call routes
    # through a different provider/model but never touches the sessions
    # columns directly (COALESCE-backfill only fires on NULL).
    db.update_token_counts(
        "s1", input_tokens=80, output_tokens=40,
        model="openai/gpt-5.5", billing_provider="openai-codex",
        api_call_count=1,
    )
    db.flush_token_counts()

    # The legacy column is unchanged — this is the documented gap.
    stale_row = db.get_session("s1")
    assert stale_row["billing_provider"] == "anthropic"

    # But the compact rich row surfaces the LATEST route via session_model_usage.
    rows = db.list_sessions_rich(compact_rows=True)
    row = _row(rows, "s1")
    assert row["served_provider"] == "openai-codex"
    assert row["served_model"] == "openai/gpt-5.5"


def test_served_falls_back_to_sessions_columns_for_legacy_session(db):
    """A session with no session_model_usage rows (pre-v20, or one that never
    recorded a delta) falls back to the sessions columns — which also
    naturally reads as 'no mismatch' against the configured route."""
    db.create_session(session_id="s1", source="cli", model="anthropic/claude-opus-4.8")
    db.update_session_billing_route(
        "s1", provider="anthropic", base_url=None, billing_mode="oauth"
    )

    rows = db.list_sessions_rich(compact_rows=True)
    row = _row(rows, "s1")
    assert row["served_provider"] == row["configured_provider"] == "anthropic"


def test_order_by_last_active_branch_carries_the_same_columns(db):
    """The recursive-CTE 'recent' ordering path must expose the identical
    served/configured columns as the plain path — the sidebar's default
    sort order is 'recent'."""
    db.create_session(session_id="s1", source="cli", model="anthropic/claude-opus-4.8")
    db.update_session_billing_route(
        "s1", provider="anthropic", base_url=None, billing_mode="oauth"
    )
    db.update_token_counts(
        "s1", input_tokens=10, output_tokens=5,
        model="anthropic/claude-opus-4.8", billing_provider="anthropic",
        api_call_count=1,
    )
    db.flush_token_counts()
    db.update_token_counts(
        "s1", input_tokens=10, output_tokens=5,
        model="openai/gpt-5.5", billing_provider="openai-codex",
        api_call_count=1,
    )
    db.flush_token_counts()

    rows = db.list_sessions_rich(compact_rows=True, order_by_last_active=True)
    row = _row(rows, "s1")
    assert row["configured_provider"] == "anthropic"
    assert row["served_provider"] == "openai-codex"


def test_pinned_backfill_branch_carries_the_same_columns(db):
    """A pinned conversation the page's LIMIT/OFFSET window missed is
    back-filled by a second query — it must carry the same identity columns
    as a normally-paged row, not silently drop them."""
    db.create_session(session_id="old-pinned", source="cli", model="anthropic/claude-opus-4.8")
    db.update_session_billing_route(
        "old-pinned", provider="anthropic", base_url=None, billing_mode="oauth"
    )
    # First accounted call establishes the (matching) authoritative route —
    # see test_served_provider_reflects_latest_call_not_first for why a lone
    # call would instead overwrite the sessions row unconditionally.
    db.update_token_counts(
        "old-pinned", input_tokens=5, output_tokens=2,
        model="anthropic/claude-opus-4.8", billing_provider="anthropic",
        api_call_count=1,
    )
    db.flush_token_counts()
    # Mid-conversation fallback: only session_model_usage sees this route.
    db.update_token_counts(
        "old-pinned", input_tokens=10, output_tokens=5,
        model="openai/gpt-5.5", billing_provider="openai-codex",
        api_call_count=1,
    )
    db.flush_token_counts()
    db.set_session_pinned("old-pinned", True)

    # Push "old-pinned" off the first page with fresher sessions.
    for i in range(3):
        db.create_session(session_id=f"newer-{i}", source="cli")

    rows = db.list_sessions_rich(compact_rows=True, limit=3, include_pinned=True)
    ids = {r["id"] for r in rows}
    assert "old-pinned" in ids, "pinned session must be back-filled onto the page"

    row = _row(rows, "old-pinned")
    assert row["configured_provider"] == "anthropic"
    assert row["served_provider"] == "openai-codex"


def test_legacy_session_with_no_model_config_has_no_configured_provider(db):
    """A session that never ran /model and never recorded a billing route
    must not report a guessed configured_provider."""
    db.create_session(session_id="s1", source="cli")

    rows = db.list_sessions_rich(compact_rows=True)
    row = _row(rows, "s1")
    assert row["configured_provider"] is None


def test_bare_billing_bucket_is_not_a_routable_configured_identity(db):
    """'auto'/'custom' billing buckets are not routable provider identities —
    session_gateway_runtime filters them, and configured_provider must too."""
    db.create_session(session_id="s1", source="cli")
    db.update_token_counts(
        "s1", input_tokens=1, output_tokens=1,
        model="some-model", billing_provider="auto",
    )
    db.flush_token_counts()

    rows = db.list_sessions_rich(compact_rows=True)
    row = _row(rows, "s1")
    assert row["configured_provider"] is None
