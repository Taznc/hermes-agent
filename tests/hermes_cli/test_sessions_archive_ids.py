"""`hermes sessions archive --ids` / `unarchive` / `archive|prune --include-open`.

These exercise the real argparse + dispatch path through `hermes_cli.main`, so a flag that is
declared but never threaded into the DB call fails here.
"""

import sys

import pytest


class _FakeDB:
    """Minimal SessionDB stand-in recording the calls the sessions CLI makes."""

    def __init__(self, sessions=None, unknown=()):
        # id -> row; anything not present resolves as unknown.
        self.sessions = sessions or {}
        self.unknown = set(unknown)
        self.archived_calls = []
        self.filter_calls = []
        self.deleted = []
        self.closed = False

    # -- id resolution --------------------------------------------------
    def resolve_session_id(self, raw):
        if raw in self.sessions:
            return raw
        matches = [sid for sid in self.sessions if sid.startswith(raw)]
        return matches[0] if len(matches) == 1 else None

    def resolve_session_ids(self, raw_ids):
        resolved, unknown, seen = [], [], set()
        for raw in raw_ids or ():
            match = self.resolve_session_id(str(raw).strip())
            if not match:
                unknown.append(raw)
            elif match not in seen:
                seen.add(match)
                resolved.append(match)
        return resolved, unknown

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_session_title(self, session_id):
        return (self.sessions.get(session_id) or {}).get("title")

    # -- writes ----------------------------------------------------------
    def set_sessions_archived(self, ids, archived):
        self.archived_calls.append((list(ids), archived))
        return list(ids)

    def delete_session(self, session_id, **_kwargs):
        self.deleted.append(session_id)
        return True

    # -- bulk filter surface ---------------------------------------------
    def list_prune_candidates(self, **kwargs):
        self.filter_calls.append(("list", kwargs))
        return [
            {"id": sid, "source": row.get("source", "cli"), "title": row.get("title"),
             "model": None, "started_at": 1.0, "last_active": 2.0,
             "ended_at": row.get("ended_at"), "message_count": 1, "archived": 0}
            for sid, row in self.sessions.items()
        ]

    def count_prune_matches(self, **kwargs):
        return len(self.sessions)

    def count_open_prune_matches(self, **kwargs):
        self.filter_calls.append(("count_open", kwargs))
        return 0

    def archive_sessions(self, **kwargs):
        self.filter_calls.append(("archive", kwargs))
        return len(self.sessions)

    def prune_sessions(self, **kwargs):
        self.filter_calls.append(("prune", kwargs))
        return len(self.sessions)

    def close(self):
        self.closed = True


def _run(monkeypatch, capsys, argv_tail, db, answer="y"):
    import hermes_cli.main as main_mod
    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db)
    monkeypatch.setattr(sys, "argv", ["hermes", "sessions", *argv_tail])
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
    try:
        main_mod.main()
        code = 0
    except SystemExit as exc:  # argparse / non-zero command return
        code = exc.code or 0
    return code, capsys.readouterr().out


_OPEN_ROW = {"source": "desktop", "title": "left open", "ended_at": None, "pinned": 0}


def test_archive_ids_archives_an_open_session(monkeypatch, capsys):
    """The whole point: a never-ended session is archivable by id (#85007)."""
    db = _FakeDB({"open_one": dict(_OPEN_ROW)})

    _code, out = _run(monkeypatch, capsys, ["archive", "--ids", "open_one", "--yes"], db)

    assert db.archived_calls == [(["open_one"], True)]
    # The id path must not fall through to the filter selector at all.
    assert db.filter_calls == []
    assert "Archived 1 session(s)" in out


def test_archive_ids_rejects_mixing_with_filters(monkeypatch, capsys):
    db = _FakeDB({"open_one": dict(_OPEN_ROW)})

    code, out = _run(
        monkeypatch, capsys, ["archive", "--ids", "open_one", "--source", "cli", "--yes"], db
    )

    assert code == 1
    assert "cannot be combined with metadata filters" in out
    assert db.archived_calls == []


def test_archive_ids_reports_unknown_ids_and_still_archives_the_rest(monkeypatch, capsys):
    db = _FakeDB({"open_one": dict(_OPEN_ROW)})

    code, out = _run(
        monkeypatch, capsys, ["archive", "--ids", "open_one", "ghost_id", "--yes"], db
    )

    assert "Session 'ghost_id' not found." in out
    assert db.archived_calls == [(["open_one"], True)]
    assert code == 1  # unknown ids are reported through the exit status too


def test_archive_ids_dry_run_changes_nothing(monkeypatch, capsys):
    db = _FakeDB({"open_one": dict(_OPEN_ROW)})

    _code, out = _run(monkeypatch, capsys, ["archive", "--ids", "open_one", "--dry-run"], db)

    assert db.archived_calls == []
    assert "open_one" in out
    assert "Dry run" in out


def test_unarchive_by_id_clears_the_flag(monkeypatch, capsys):
    db = _FakeDB({"open_one": {**_OPEN_ROW, "archived": 1}})

    _code, out = _run(monkeypatch, capsys, ["unarchive", "open_one", "--yes"], db)

    assert db.archived_calls == [(["open_one"], False)]
    assert "Unarchived 1 session(s)" in out


def test_unarchive_dry_run_changes_nothing(monkeypatch, capsys):
    db = _FakeDB({"open_one": {**_OPEN_ROW, "archived": 1}})

    _code, out = _run(monkeypatch, capsys, ["unarchive", "--ids", "open_one", "--dry-run"], db)

    assert db.archived_calls == []
    assert "Dry run" in out


@pytest.mark.parametrize("action", ["archive", "prune"])
def test_include_open_is_threaded_into_the_db_filters(monkeypatch, capsys, action):
    """The flag must reach the store, not merely parse (#90360)."""
    db = _FakeDB({"open_one": dict(_OPEN_ROW)})

    _run(monkeypatch, capsys, [action, "--source", "desktop", "--include-open", "--yes"], db)

    listed = [kwargs for name, kwargs in db.filter_calls if name == "list"]
    assert listed and listed[0]["include_open"] is True
    applied = [kwargs for name, kwargs in db.filter_calls if name == action]
    assert applied and applied[0]["include_open"] is True


@pytest.mark.parametrize("action", ["archive", "prune"])
def test_without_include_open_the_ended_gate_is_requested(monkeypatch, capsys, action):
    """Default behavior is unchanged: include_open goes down as False, never omitted."""
    db = _FakeDB({"open_one": dict(_OPEN_ROW)})

    _run(monkeypatch, capsys, [action, "--source", "desktop", "--yes"], db)

    listed = [kwargs for name, kwargs in db.filter_calls if name == "list"]
    assert listed and listed[0]["include_open"] is False


def test_prune_open_session_note_only_when_the_gate_is_on(monkeypatch, capsys):
    """The `_count_open_sessions_skipped` visibility path stays wired for default prune."""
    db = _FakeDB({"open_one": dict(_OPEN_ROW)})

    _run(monkeypatch, capsys, ["prune", "--source", "desktop", "--yes"], db)
    assert any(name == "count_open" for name, _ in db.filter_calls)

    db2 = _FakeDB({"open_one": dict(_OPEN_ROW)})
    _run(monkeypatch, capsys, ["prune", "--source", "desktop", "--include-open", "--yes"], db2)
    # The prune must actually have run (guards against argparse rejecting the flag and this
    # assertion passing vacuously) — but nothing is skipped, so the count is never asked for.
    assert any(name == "prune" for name, _ in db2.filter_calls)
    assert not any(name == "count_open" for name, _ in db2.filter_calls)


def test_delete_accepts_multiple_ids_in_one_invocation(monkeypatch, capsys):
    db = _FakeDB({"a_one": dict(_OPEN_ROW), "b_two": dict(_OPEN_ROW)})

    _code, out = _run(monkeypatch, capsys, ["delete", "a_one", "b_two", "--yes"], db)

    assert db.deleted == ["a_one", "b_two"]
    assert "Deleted 2 session(s)." in out


def test_delete_batch_dry_run_deletes_nothing(monkeypatch, capsys):
    db = _FakeDB({"a_one": dict(_OPEN_ROW), "b_two": dict(_OPEN_ROW)})

    _code, out = _run(monkeypatch, capsys, ["delete", "a_one", "b_two", "--dry-run"], db)

    assert db.deleted == []
    assert "Dry run — nothing deleted." in out


def test_delete_batch_declining_confirmation_deletes_nothing(monkeypatch, capsys):
    db = _FakeDB({"a_one": dict(_OPEN_ROW), "b_two": dict(_OPEN_ROW)})

    _code, out = _run(monkeypatch, capsys, ["delete", "a_one", "b_two"], db, answer="n")

    assert db.deleted == []
    assert "Cancelled." in out
