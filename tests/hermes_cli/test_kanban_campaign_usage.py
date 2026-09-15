"""Whole-campaign Kanban inference usage reconciliation.

Covers the acceptance criteria of t_b89c96a0:

* AC1 final receipts reconcile main + auxiliary + delegated use exactly once, idempotently
* AC2 mixed providers/models stay separated; cache writes and unavailable-vs-zero truthful
* AC3 campaign root aggregates specification/implementation/review/rework/landing/children
* AC4 tool operation counts deduplicate by tool-call ID
* AC5 raw transcript contents are never exported
* AC6 completion/review transitions still work when analytics storage is unavailable
* AC7 legacy boards migrate additively

The reconciler's contract, and the reason a ``sessions``-only copy undercounted, live in
``hermes_cli/kanban_usage.py``'s module docstring.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_usage as ku


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


# ── state.db fixture builder ─────────────────────────────────────────────────

_SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    model TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    api_call_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    estimated_cost_usd REAL
)
"""

_MODEL_USAGE_DDL = """
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
)
"""

_MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tool_calls TEXT,
    active INTEGER NOT NULL DEFAULT 1
)
"""


class StateDB:
    """Minimal stand-in for a profile's ``state.db``, shaped like the real schema."""

    def __init__(self, profile_home: Path, *, with_model_usage: bool = True,
                 with_messages: bool = True):
        profile_home.mkdir(parents=True, exist_ok=True)
        self.path = profile_home / "state.db"
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute(_SESSIONS_DDL)
        if with_model_usage:
            self.conn.execute(_MODEL_USAGE_DDL)
        if with_messages:
            self.conn.execute(_MESSAGES_DDL)
        self.conn.commit()

    def session(self, session_id: str, *, parent: str | None = None, **cols):
        self.conn.execute(
            "INSERT INTO sessions (id, parent_session_id, model, billing_provider, "
            "billing_base_url, billing_mode, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, reasoning_tokens, api_call_count, "
            "tool_call_count, estimated_cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, parent, cols.get("model"), cols.get("billing_provider"),
                cols.get("billing_base_url", ""), cols.get("billing_mode", ""),
                cols.get("input_tokens", 0), cols.get("output_tokens", 0),
                cols.get("cache_read_tokens", 0), cols.get("cache_write_tokens", 0),
                cols.get("reasoning_tokens", 0), cols.get("api_call_count", 0),
                cols.get("tool_call_count", 0), cols.get("estimated_cost_usd"),
            ),
        )
        self.conn.commit()
        return self

    def usage(self, session_id: str, *, task: str = "", model: str = "m",
              provider: str = "p", **cols):
        self.conn.execute(
            "INSERT INTO session_model_usage (session_id, model, billing_provider, "
            "billing_base_url, billing_mode, task, api_call_count, input_tokens, "
            "output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, "
            "estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, model, provider, cols.get("base_url", ""),
                cols.get("billing_mode", ""), task, cols.get("api_call_count", 0),
                cols.get("input_tokens", 0), cols.get("output_tokens", 0),
                cols.get("cache_read_tokens", 0), cols.get("cache_write_tokens", 0),
                cols.get("reasoning_tokens", 0), cols.get("estimated_cost_usd", 0.0),
            ),
        )
        self.conn.commit()
        return self

    def bump_usage(self, session_id: str, *, task: str = "", model: str = "m",
                   provider: str = "p", **cols):
        """Simulate a LATE write — the worker tail landing after finalize."""
        self.conn.execute(
            "UPDATE session_model_usage SET api_call_count = api_call_count + ?, "
            "input_tokens = input_tokens + ?, output_tokens = output_tokens + ? "
            "WHERE session_id = ? AND task = ? AND model = ? AND billing_provider = ?",
            (
                cols.get("api_call_count", 0), cols.get("input_tokens", 0),
                cols.get("output_tokens", 0), session_id, task, model, provider,
            ),
        )
        self.conn.commit()
        return self

    def message(self, session_id: str, tool_calls, *, active: int = 1):
        self.conn.execute(
            "INSERT INTO messages (session_id, tool_calls, active) VALUES (?, ?, ?)",
            (session_id, json.dumps(tool_calls) if tool_calls is not None else None, active),
        )
        self.conn.commit()
        return self

    def close(self):
        self.conn.close()


def _make_run(task_title: str = "t", *, profile: str = "elias",
              session_id: str | None = "20260101_000000_aaaaaa"):
    """Create + claim a task, pinning ``session_id`` on the run as the dispatcher does."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=task_title, assignee=profile)
        claimed = kb.claim_task(conn, tid)
        run_id = claimed.current_run_id
        if session_id is not None:
            conn.execute(
                "UPDATE task_runs SET session_id = ? WHERE id = ?", (session_id, run_id)
            )
        conn.commit()
    return tid, run_id


# ─────────────────────────────────────────────────────────────────────────────
# AC1: main + auxiliary + delegated reconciled exactly once, idempotently
# ─────────────────────────────────────────────────────────────────────────────


def test_reconcile_includes_auxiliary_and_delegated_usage(kanban_home):
    """The audit-v2 undercount: aux calls never touch the ``sessions`` counters and
    delegated subagents live in their own session, so a sessions-only copy misses both."""
    session_id = "20260101_000000_aaaaaa"
    child_id = "20260101_000000_bbbbbb"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id, model="claude-sonnet-5", billing_provider="anthropic")
    state.session(child_id, parent=session_id)
    # Main loop.
    state.usage(session_id, task="", model="claude-sonnet-5", provider="anthropic",
                api_call_count=100, input_tokens=1000, output_tokens=500)
    # Auxiliary: compression on the same provider, vision on another.
    state.usage(session_id, task="compression", model="claude-haiku-5", provider="anthropic",
                api_call_count=8, input_tokens=80, output_tokens=40)
    state.usage(session_id, task="vision", model="gemini-3-pro", provider="gemini",
                api_call_count=5, input_tokens=50, output_tokens=25)
    # Delegated subagent.
    state.usage(child_id, task="", model="claude-sonnet-5", provider="anthropic",
                api_call_count=43, input_tokens=430, output_tokens=215)
    state.close()

    tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)
        run = kb.get_run(conn, run_id)

    # 100 main + 8 + 5 aux + 43 delegated = 156, exactly the audit's "final" figure,
    # against the 143 a lifecycle snapshot of main-only + one aux would have reported.
    assert receipt["api_calls"] == 156
    assert receipt["input_tokens"] == 1000 + 80 + 50 + 430
    assert receipt["usage_status"] == ku.STATUS_RECONCILED
    assert run.api_calls == 156
    assert run.usage_status == ku.STATUS_RECONCILED
    scopes = {(r["scope"], r["task"]) for r in receipt["routes"]}
    assert scopes == {("main", ""), ("main", "compression"), ("main", "vision"),
                      ("delegated", "")}


def test_reconcile_is_idempotent(kanban_home):
    """Reconciling unchanged input twice must be byte-identical — no accumulation."""
    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.usage(session_id, api_call_count=10, input_tokens=100, output_tokens=50)
    state.usage(session_id, task="vision", model="gemini-3-pro", provider="gemini",
                api_call_count=2, input_tokens=20)
    state.close()

    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        first = ku.reconcile_run_usage(conn, run_id)
        rows_first = [dict(r) for r in conn.execute(
            "SELECT * FROM task_run_usage WHERE run_id = ? ORDER BY scope, task", (run_id,)
        )]
        for _ in range(4):
            repeat = ku.reconcile_run_usage(conn, run_id)
        rows_repeat = [dict(r) for r in conn.execute(
            "SELECT * FROM task_run_usage WHERE run_id = ? ORDER BY scope, task", (run_id,)
        )]

    assert first["api_calls"] == repeat["api_calls"] == 12
    assert len(rows_first) == len(rows_repeat) == 2
    for a, b in zip(rows_first, rows_repeat):
        assert {k: v for k, v in a.items() if k != "updated_at"} == \
               {k: v for k, v in b.items() if k != "updated_at"}


def test_reconcile_picks_up_late_worker_tail(kanban_home):
    """kanban_complete is a tool call, so the worker's remaining turns land in state.db
    AFTER finalize. Re-reconciling must include them without double-counting."""
    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.usage(session_id, api_call_count=140, input_tokens=1400, output_tokens=700)

    tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        kb.complete_task(conn, tid, summary="done")
        at_finalize = kb.get_run(conn, run_id).api_calls

    # The tail: three more calls after the lifecycle transition already ran.
    state.bump_usage(session_id, api_call_count=3, input_tokens=30, output_tokens=15)
    state.close()

    with kbc.connect_closing() as conn:
        after = ku.reconcile_run_usage(conn, run_id)

    assert at_finalize == 140
    assert after["api_calls"] == 143
    assert after["input_tokens"] == 1430


# ─────────────────────────────────────────────────────────────────────────────
# AC2: mixed routes separated; cache writes and unavailable-vs-zero truthful
# ─────────────────────────────────────────────────────────────────────────────


def test_mixed_providers_stay_separated(kanban_home):
    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.usage(session_id, model="claude-opus-5", provider="anthropic",
                api_call_count=10, input_tokens=100)
    state.usage(session_id, model="gpt-5.6-sol", provider="openai-codex",
                api_call_count=7, input_tokens=70)
    state.close()

    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)

    by_model = {r["model"]: r for r in receipt["routes"]}
    assert by_model["claude-opus-5"]["provider"] == "anthropic"
    assert by_model["claude-opus-5"]["api_calls"] == 10
    assert by_model["gpt-5.6-sol"]["provider"] == "openai-codex"
    assert by_model["gpt-5.6-sol"]["api_calls"] == 7
    assert receipt["api_calls"] == 17


def test_cache_write_tokens_are_persisted(kanban_home):
    """Cache writes cost ~50x a read, so they are the money — and the old seven-column
    copy had no column for them at all."""
    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.usage(session_id, cache_read_tokens=96_000, cache_write_tokens=4_000,
                api_call_count=1)
    state.close()

    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)
        run = kb.get_run(conn, run_id)

    assert receipt["cache_write_tokens"] == 4_000
    assert receipt["cache_read_tokens"] == 96_000
    assert run.cache_write_tokens == 4_000


def test_unavailable_is_null_not_zero(kanban_home):
    """A run whose usage cannot be measured must report NULL, never a fabricated 0 —
    otherwise an unreadable store looks like a free run."""
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)
        run = kb.get_run(conn, run_id)

    assert receipt["usage_status"] == ku.STATUS_UNAVAILABLE
    assert receipt["api_calls"] is None
    assert receipt["input_tokens"] is None
    assert run.api_calls is None
    assert run.usage_status == ku.STATUS_UNAVAILABLE


def test_measured_zero_is_zero_not_null(kanban_home):
    """The mirror image: a run that genuinely spent nothing reports 0 and is marked
    reconciled, so it is distinguishable from an unmeasurable one."""
    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.usage(session_id, api_call_count=0, input_tokens=0, output_tokens=0)
    state.close()

    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)

    assert receipt["usage_status"] == ku.STATUS_RECONCILED
    assert receipt["api_calls"] == 0
    assert receipt["input_tokens"] == 0


def test_older_state_schema_is_labelled_session_only(kanban_home):
    """No ``session_model_usage`` table: the flat fallback has no aux rows and no route
    split, so it must be labelled degraded rather than passed off as reconciled."""
    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias", with_model_usage=False)
    state.session(session_id, model="claude-sonnet-5", billing_provider="anthropic",
                  api_call_count=12, input_tokens=120)
    state.close()

    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)
        run = kb.get_run(conn, run_id)

    assert receipt["usage_status"] == ku.STATUS_SESSION_ONLY
    assert receipt["api_calls"] == 12
    assert run.usage_status == ku.STATUS_SESSION_ONLY
    assert receipt["routes"][0]["source"] == "session_row"


# ─────────────────────────────────────────────────────────────────────────────
# AC3: campaign root aggregation over every phase and required child
# ─────────────────────────────────────────────────────────────────────────────


def _campaign_board(profile: str = "elias"):
    """spec -> implementation -> {review, required child}; review -> rework -> landing."""
    ids = {}
    with kbc.connect_closing() as conn:
        ids["spec"] = kb.create_task(conn, title="specification", assignee=profile)
        ids["impl"] = kb.create_task(conn, title="implementation", assignee=profile,
                                     parents=[ids["spec"]])
        ids["review"] = kb.create_task(conn, title="review", assignee="reviewer",
                                       parents=[ids["impl"]])
        ids["child"] = kb.create_task(conn, title="required child", assignee=profile,
                                      parents=[ids["impl"]])
        ids["rework"] = kb.create_task(conn, title="rework", assignee=profile,
                                       parents=[ids["review"]])
        ids["landing"] = kb.create_task(conn, title="landing", assignee=profile,
                                        parents=[ids["rework"]])
    return ids


def test_campaign_root_and_closure_cover_every_phase(kanban_home):
    ids = _campaign_board()
    with kbc.connect_closing() as conn:
        # Asked from the middle of the campaign, the root is still the specification.
        assert ku.campaign_root_ids(conn, ids["rework"]) == [ids["spec"]]
        assert set(ku.campaign_task_ids(conn, ids["rework"])) == set(ids.values())
        # And from the root itself.
        assert set(ku.campaign_task_ids(conn, ids["spec"])) == set(ids.values())


def _add_run(conn, task_id: str, session_id: str, *, profile: str = "elias") -> int:
    """Insert a finished run row directly.

    Phase tasks in a campaign have parents, so they sit in ``todo`` and cannot be
    claimed without running the whole dispatcher; and a retry's earlier runs are
    already-ended rows. Aggregation is what is under test here, not the claim path —
    ``test_crash_path_still_reconciles`` covers the real dispatcher-driven route.
    """
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
        "outcome, session_id) VALUES (?, ?, 'done', 1, 2, 'completed', ?)",
        (task_id, profile, session_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_campaign_usage_counts_every_run_exactly_once(kanban_home):
    ids = _campaign_board()
    state = StateDB(kanban_home / "profiles" / "elias")
    reviewer_state = StateDB(kanban_home / "profiles" / "reviewer")

    per_phase = {"spec": 5, "impl": 40, "review": 12, "child": 9, "rework": 15,
                 "landing": 3}
    with kbc.connect_closing() as conn:
        for phase, calls in per_phase.items():
            session_id = f"20260101_000000_{phase}"
            is_review = phase == "review"
            _add_run(conn, ids[phase], session_id,
                     profile="reviewer" if is_review else "elias")
            target = reviewer_state if is_review else state
            target.session(session_id)
            target.usage(session_id, api_call_count=calls, input_tokens=calls * 10)
    state.close()
    reviewer_state.close()

    with kbc.connect_closing() as conn:
        campaign = ku.campaign_usage(conn, ids["impl"])

    assert campaign["campaign_roots"] == [ids["spec"]]
    assert len(campaign["run_ids"]) == 6
    assert campaign["runs_measured"] == 6
    assert campaign["totals"]["api_calls"] == sum(per_phase.values())
    assert campaign["totals"]["input_tokens"] == sum(per_phase.values()) * 10


def test_campaign_counts_retried_runs_separately_but_each_once(kanban_home):
    """A task retried three times contributes three DISTINCT runs — and aggregating
    repeatedly must not inflate them."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="retried", assignee="elias")
    state = StateDB(kanban_home / "profiles" / "elias")

    with kbc.connect_closing() as conn:
        for attempt in range(3):
            session_id = f"20260101_00000{attempt}_aaaaaa"
            _add_run(conn, tid, session_id)
            state.session(session_id)
            state.usage(session_id, api_call_count=10)
    state.close()

    with kbc.connect_closing() as conn:
        first = ku.campaign_usage(conn, tid)
        second = ku.campaign_usage(conn, tid)

    assert len(first["run_ids"]) == 3
    assert first["totals"]["api_calls"] == 30
    assert second["totals"]["api_calls"] == 30


def test_campaign_totals_null_when_nothing_measurable(kanban_home):
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    tid, _run_id = _make_run()
    with kbc.connect_closing() as conn:
        campaign = ku.campaign_usage(conn, tid)

    assert campaign["runs_measured"] == 0
    assert campaign["totals"]["api_calls"] is None
    assert campaign["runs_unavailable"]


# ─────────────────────────────────────────────────────────────────────────────
# AC4: tool operation counts deduplicate by tool ID
# ─────────────────────────────────────────────────────────────────────────────


def test_tool_calls_deduplicate_by_id_across_compaction_clones(kanban_home):
    """Compaction clones tail rows byte-exactly, so the same tool-call id appears on an
    inactive original AND an active clone. ``sessions.tool_call_count`` counts both;
    dedupe by id must count the operation once."""
    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id, tool_call_count=99)
    state.usage(session_id, api_call_count=1)
    state.message(session_id, [{"id": "call_a"}, {"id": "call_b"}], active=0)
    state.message(session_id, [{"id": "call_a"}, {"id": "call_b"}], active=1)  # clone
    state.message(session_id, [{"id": "call_c"}], active=1)
    state.close()

    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)
        run = kb.get_run(conn, run_id)

    assert receipt["tool_calls"] == 3
    assert run.tool_calls == 3


def test_tool_calls_span_delegated_sessions(kanban_home):
    session_id = "20260101_000000_aaaaaa"
    child_id = "20260101_000000_bbbbbb"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.session(child_id, parent=session_id)
    state.usage(session_id, api_call_count=1)
    state.message(session_id, [{"id": "parent_1"}])
    state.message(child_id, [{"id": "child_1"}, {"id": "child_2"}])
    state.close()

    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)

    assert receipt["tool_calls"] == 3


def test_id_less_tool_calls_count_once_per_active_row(kanban_home):
    """Non-OpenAI shapes carry no id, so they have no dedupe key; counting them once per
    active row is the honest fallback rather than dropping them."""
    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.usage(session_id, api_call_count=1)
    state.message(session_id, [{"name": "terminal"}, {"name": "read_file"}], active=1)
    state.message(session_id, [{"name": "terminal"}], active=0)  # compacted away
    state.message(session_id, None)
    state.close()

    _tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)

    assert receipt["tool_calls"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# AC5: raw transcript contents are never exported
# ─────────────────────────────────────────────────────────────────────────────


def test_no_transcript_content_reaches_the_board(kanban_home):
    """Every persisted usage value must be numeric/route metadata. A tool name, tool id,
    or argument string leaking into the board DB is a privacy regression."""
    session_id = "20260101_000000_aaaaaa"
    secret_id = "call_SECRET_TOOL_ID"
    secret_arg = "SECRET_ARGUMENT_PAYLOAD"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.usage(session_id, api_call_count=1)
    state.message(session_id, [
        {"id": secret_id, "function": {"name": "terminal", "arguments": secret_arg}},
    ])
    state.close()

    tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        receipt = ku.reconcile_run_usage(conn, run_id)
        campaign = ku.campaign_usage(conn, tid)
        dumped = "".join(
            str(tuple(r)) for r in conn.execute("SELECT * FROM task_run_usage")
        ) + "".join(
            str(tuple(r)) for r in conn.execute("SELECT * FROM task_runs")
        )

    for blob in (dumped, json.dumps(receipt), json.dumps(campaign)):
        assert secret_id not in blob
        assert secret_arg not in blob
        assert "terminal" not in blob
    assert receipt["tool_calls"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# AC6: lifecycle transitions survive an unavailable analytics store
# ─────────────────────────────────────────────────────────────────────────────


def test_complete_succeeds_with_no_state_db(kanban_home, caplog):
    caplog.set_level("DEBUG", logger="hermes_cli.kanban_db")
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, summary="done") is True
        run = kb.get_run(conn, run_id)
    assert run.input_tokens is None
    assert run.usage_status == ku.STATUS_UNAVAILABLE


def test_complete_succeeds_with_corrupt_state_db(kanban_home):
    """A truncated/garbage state.db must not block finalize."""
    profile_home = kanban_home / "profiles" / "elias"
    profile_home.mkdir(parents=True)
    (profile_home / "state.db").write_bytes(b"this is not a sqlite database at all")
    tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, summary="done") is True
        run = kb.get_run(conn, run_id)
    assert run.usage_status == ku.STATUS_UNAVAILABLE


def test_complete_succeeds_with_missing_session_row(kanban_home):
    state = StateDB(kanban_home / "profiles" / "elias")
    state.close()
    tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, summary="done") is True
        run = kb.get_run(conn, run_id)
    assert run.input_tokens is None
    assert run.usage_status == ku.STATUS_UNAVAILABLE


def test_complete_succeeds_when_reconciler_itself_raises(kanban_home, monkeypatch):
    """The failure-isolation backstop: even a bug inside the reconciler must not be able
    to strand a task mid-transition."""
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session("20260101_000000_aaaaaa")
    state.usage("20260101_000000_aaaaaa", api_call_count=5)
    state.close()

    def boom(conn, run_id):
        raise RuntimeError("analytics storage exploded")

    monkeypatch.setattr(ku, "reconcile_run_usage", boom)
    tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, summary="done") is True
        task = kb.get_task(conn, tid)
    assert task.status == "done"


def test_review_and_rework_transitions_survive_unavailable_storage(kanban_home):
    """Review and rework are lifecycle transitions too, not just completion."""
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="done", reviewer="reviewer",
            expected_run_id=claimed.current_run_id,
        )
        reviewer_claim = kb.claim_review_task(conn, tid)
        ok, _ = kb.request_changes(
            conn, tid, reason="please fix", expected_run_id=reviewer_claim.current_run_id,
        )
        assert ok
        task = kb.get_task(conn, tid)
    assert task.status in {"ready", "todo", "running"}


def test_crash_path_still_reconciles(kanban_home):
    """A crashed run never reaches kanban_complete, but its usage was still spent."""
    from hermes_cli import kanban_db_dispatch as kbd

    session_id = "20260101_000000_aaaaaa"
    state = StateDB(kanban_home / "profiles" / "elias")
    state.session(session_id)
    state.usage(session_id, api_call_count=17, input_tokens=170)
    state.close()

    tid, run_id = _make_run()
    with kbc.connect_closing() as conn:
        kbd._record_task_failure(
            conn, tid, "boom", outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        run = kb.get_run(conn, run_id)

    assert run.api_calls == 17
    assert run.usage_status == ku.STATUS_RECONCILED


# ─────────────────────────────────────────────────────────────────────────────
# AC7: legacy boards migrate additively
# ─────────────────────────────────────────────────────────────────────────────


def test_legacy_board_without_usage_columns_migrates(kanban_home):
    """A board predating this feature gains the columns and the table, and its existing
    rows read back NULL rather than failing."""
    db_path = kb.kanban_db_path()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="elias")
        run_id = kb.claim_task(conn, tid).current_run_id

    # Simulate the pre-feature shape by dropping what this card added.
    raw = sqlite3.connect(str(db_path))
    raw.execute("DROP TABLE task_run_usage")
    raw.execute("ALTER TABLE task_runs DROP COLUMN usage_status")
    raw.execute("ALTER TABLE task_runs DROP COLUMN cache_write_tokens")
    raw.commit()
    raw.close()

    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert {"usage_status", "cache_write_tokens"} <= cols
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_run_usage'"
        ).fetchone() is not None
        run = kb.get_run(conn, run_id)

    assert run.usage_status is None
    assert run.cache_write_tokens is None


def test_migration_is_idempotent_across_reinit(kanban_home):
    for _ in range(3):
        kb._INITIALIZED_PATHS.clear()
        with kbc.connect_closing() as conn:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")]
    assert cols.count("usage_status") == 1
    assert cols.count("cache_write_tokens") == 1
