import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _compression_pair(db: SessionDB):
    base = time.time() - 100
    db.create_session("root", source="cli")
    db.create_session("tip", source="cli", parent_session_id="root")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, end_reason = 'compression', message_count = 1 WHERE id = 'root'",
        (base, base + 10),
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, message_count = 1 WHERE id = 'tip'",
        (base + 20,),
    )
    db._conn.commit()


def test_archiving_compression_tip_archives_projected_root(db):
    _compression_pair(db)

    assert db.set_session_archived("tip", True) is True

    assert db.get_session("root")["archived"] == 1
    assert db.get_session("tip")["archived"] == 1
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True)] == []
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True, archived_only=True)] == ["tip"]


def test_unarchiving_compression_tip_unarchives_projected_root(db):
    _compression_pair(db)
    db.set_session_archived("tip", True)

    assert db.set_session_archived("tip", False) is True

    assert db.get_session("root")["archived"] == 0
    assert db.get_session("tip")["archived"] == 0
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True)] == ["tip"]


def _open_session(db: SessionDB, session_id: str, *, source: str = "desktop") -> None:
    """A session the user navigated away from: never ended, so ``ended_at IS NULL``."""
    db.create_session(session_id, source=source)
    db.append_message(session_id, "user", "hi")
    assert db.get_session(session_id)["ended_at"] is None


def test_archiving_by_id_reaches_an_open_session(db):
    """Per-ID archive is not gated on ended_at: an open session is the whole point (#85007)."""
    _open_session(db, "open_one")

    assert db.set_sessions_archived(["open_one"], True) == ["open_one"]

    assert db.get_session("open_one")["archived"] == 1
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True)] == []
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True, archived_only=True)] == ["open_one"]


def test_archiving_by_id_is_reversible_for_an_open_session(db):
    _open_session(db, "open_one")
    db.set_sessions_archived(["open_one"], True)

    assert db.set_sessions_archived(["open_one"], False) == ["open_one"]

    assert db.get_session("open_one")["archived"] == 0
    assert [s["id"] for s in db.list_sessions_rich(order_by_last_active=True)] == ["open_one"]


def test_resolve_session_ids_reports_unknown_and_dedupes(db):
    """Unknown ids come back for reporting instead of being silently dropped."""
    _open_session(db, "20260101_000000_abcdef")

    resolved, unknown = db.resolve_session_ids(
        ["20260101_000000_abcdef", "20260101_000000_abc", "  ", "no_such_session"]
    )

    # Both spellings name one session (exact + unique prefix), so it appears once.
    assert resolved == ["20260101_000000_abcdef"]
    assert unknown == ["no_such_session"]


def test_set_sessions_archived_skips_ids_that_no_longer_exist(db):
    """A racing delete must not fail the rest of the batch."""
    _open_session(db, "alive")

    assert db.set_sessions_archived(["alive", "already_gone"], True) == ["alive"]
    assert db.get_session("alive")["archived"] == 1


def test_include_open_matches_a_session_with_null_ended_at(db):
    """The bulk selector reaches never-ended sessions only when explicitly asked (#90360)."""
    _open_session(db, "open_one")

    assert db.list_prune_candidates(source="desktop") == []
    assert [row["id"] for row in db.list_prune_candidates(source="desktop", include_open=True)] == ["open_one"]
    assert db.count_prune_matches(source="desktop", include_open=True) == 1


def test_default_selection_still_excludes_open_sessions(db):
    """Without the flag the ended gate is unchanged: open in, ended out of the default match."""
    _open_session(db, "open_one")
    db.create_session("ended_one", source="desktop")
    db.append_message("ended_one", "user", "hi")
    db.end_session("ended_one", "user_exit")

    assert [row["id"] for row in db.list_prune_candidates(source="desktop")] == ["ended_one"]
    # The visibility counter reports what the DEFAULT selection skipped, flag or not.
    assert db.count_open_prune_matches(source="desktop") == 1
    assert db.count_open_prune_matches(source="desktop", include_open=True) == 1


def test_archive_sessions_include_open_archives_an_open_session(db):
    _open_session(db, "open_one")

    assert db.archive_sessions(source="desktop") == 0
    assert db.archive_sessions(source="desktop", include_open=True) == 1
    assert db.get_session("open_one")["archived"] == 1


def test_prune_sessions_include_open_deletes_an_open_session(db):
    _open_session(db, "open_one")

    assert db.prune_sessions(older_than_days=None, source="desktop") == 0
    assert db.get_session("open_one") is not None

    assert db.prune_sessions(older_than_days=None, source="desktop", include_open=True) == 1
    assert db.get_session("open_one") is None


def test_unknown_filter_kwarg_is_still_rejected(db):
    """include_open widened the allow-list; it must not have opened it up entirely."""
    with pytest.raises(TypeError):
        db.list_prune_candidates(source="desktop", not_a_filter=True)
