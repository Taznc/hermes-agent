"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kbc.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_kanban_show_text_renders_run_analytics_when_present(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="analytics task", assignee="alice")
        claimed = kb.claim_task(conn, tid)
        conn.execute(
            """
            UPDATE task_runs
               SET model = 'gpt-5.6-sol', provider = 'openai', reasoning_effort = 'high',
                   input_tokens = 1000, output_tokens = 500, cache_read_tokens = 200,
                   reasoning_tokens = 50, api_calls = 12, tool_calls = 30,
                   estimated_cost_usd = 1.23
             WHERE id = ?
            """,
            (claimed.current_run_id,),
        )
        conn.commit()

    output = kc.run_slash(f"show {tid}")

    assert "model: gpt-5.6-sol · openai · high" in output
    assert "tokens: in 1,000 · out 500 · cache 200 · reasoning 50" in output
    assert "calls: API 12 · tools 30" in output
    assert "estimated cost: $1.2300" in output


def test_kanban_show_text_omits_absent_run_analytics(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="alice")
        kb.claim_task(conn, tid)

    output = kc.run_slash(f"show {tid}")

    assert "model:" not in output
    assert "tokens:" not in output
    assert "undefined" not in output


def test_list_text_renders_known_and_unknown_priority_tiers(kanban_home):
    """Priority 0 must render as Normal rather than vanish by truthiness."""
    with kbc.connect() as conn:
        kb.create_task(conn, title="normal prio task", priority=0)
        kb.create_task(conn, title="custom prio task", priority=90)

    out = kc.run_slash("list")

    assert "normalpriotask(normal)" in out.replace(" ", "")
    assert "custompriotask(90)" in out.replace(" ", "")


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kbc.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kbc.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kbc.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Roadmap lanes — create --idea/--roadmap plus refine/demote/spawn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag,expected", [("--idea", "idea"), ("--roadmap", "roadmap")])
def test_create_lane_flag_needs_no_assignee(kanban_home, flag, expected):
    """``--idea``/``--roadmap`` land the card in the inert lane; no assignee required."""
    out = kc.run_slash(f'create "wishlist item" {flag} --json')
    payload = json.loads(out)
    assert payload["status"] == expected
    assert payload["assignee"] is None


def test_create_lane_flags_are_mutually_exclusive_with_triage(kanban_home):
    out = kc.run_slash('create "x" --idea --triage')
    assert "mutually exclusive" in out
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_refine_demote_spawn_slash_round_trip(kanban_home):
    """The three verbs move a card through the lanes and into the work queue."""
    tid = json.loads(kc.run_slash('create "wish" --idea --json'))["id"]

    assert "Refined to roadmap" in kc.run_slash(f"refine {tid}")
    assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["status"] == "roadmap"

    assert "Demoted to idea" in kc.run_slash(f"demote {tid}")
    assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["status"] == "idea"

    kc.run_slash(f"refine {tid}")
    assert "Spawned to triage" in kc.run_slash(f"spawn {tid}")
    assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["status"] == "triage"


def test_spawn_to_ready_opts_out_of_triage(kanban_home):
    tid = json.loads(kc.run_slash('create "wish" --roadmap --json'))["id"]
    assert "Spawned to ready" in kc.run_slash(f"spawn {tid} --to ready")
    assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["status"] == "ready"


def test_refine_on_live_work_reports_the_attempted_transition(kanban_home):
    """A refused lane move names from->to so the operator sees why nothing happened, and
    the live card is untouched."""
    tid = json.loads(kc.run_slash('create "real work" --assignee alice --json'))["id"]
    out = kc.run_slash(f"refine {tid}")
    assert "'ready' -> 'roadmap'" in out
    assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["status"] == "ready"


def test_ls_groups_lanes_under_a_roadmap_header_after_live_work(kanban_home):
    """Wishlist cards render below every live column so the listing still reads as
    'what is in flight'."""
    kc.run_slash('create "live task" --assignee alice')
    kc.run_slash('create "wishlist item" --idea')
    out = kc.run_slash("list")
    assert "Roadmap (inert" in out
    assert out.index("live task") < out.index("Roadmap (inert") < out.index("wishlist item")


def test_ls_status_filter_accepts_lane_names(kanban_home):
    kc.run_slash('create "wishlist item" --idea')
    kc.run_slash('create "agreed item" --roadmap')
    ideas = json.loads(kc.run_slash("list --status idea --json"))
    assert [t["title"] for t in ideas] == ["wishlist item"]


def test_spawn_to_ready_reports_the_gated_landing_not_the_request(kanban_home):
    """``--to ready`` under an unfinished parent lands in ``todo``; the CLI must say where the
    card actually went. Printing the requested "Spawned to ready" would tell the operator their
    card is queued for dispatch when it is sitting in ``todo`` waiting on its parent."""
    epic = json.loads(kc.run_slash('create "epic" --assignee alice --json'))["id"]
    tid = json.loads(kc.run_slash(f'create "wish" --roadmap --parent {epic} --json'))["id"]

    out = kc.run_slash(f"spawn {tid} --to ready")
    assert "Spawned to todo" in out
    assert "requested ready" in out
    assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["status"] == "todo"


# ---------------------------------------------------------------------------
# dispatch — review-round cap must be visible in both output modes
# ---------------------------------------------------------------------------


def _card_at_the_review_round_cap(title: str) -> str:
    """A ready card with three ``changes_requested`` rounds — one more than the
    default ``kanban.max_review_rounds`` allows, so the next dispatch tick caps
    and blocks it instead of re-dispatching."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="implementer")
        for reason in ("first", "second", "third"):
            kb._append_event(conn, tid, "changes_requested", {"reason": reason})
        conn.commit()
    return tid


def test_dispatch_json_reports_the_review_round_cap(kanban_home):
    """A capped card is a state change the operator must be able to see. The
    JSON payload carried every other dispatcher transition but silently dropped
    this one, so `hermes kanban dispatch --json` reported an empty tick while a
    card had just been blocked."""
    tid = _card_at_the_review_round_cap("runaway rework")

    payload = json.loads(kc.run_slash("dispatch --dry-run --json"))

    assert payload["blocked_review_round_cap"] == [
        {"task_id": tid, "changes_rounds": 3}
    ]
    assert payload["spawned"] == []


def test_dispatch_text_reports_the_review_round_cap(kanban_home):
    """Same transition, human-readable mode: the line must name the card, the
    round count, and the cap that stopped it."""
    tid = _card_at_the_review_round_cap("runaway rework")

    out = kc.run_slash("dispatch --dry-run")

    assert "kanban.max_review_rounds=3" in out
    assert "after 3 change requests" in out
    assert tid in out


def test_dispatch_text_stays_silent_when_nothing_was_capped(kanban_home):
    """Negative case: the cap line is per-entry, never an unconditional header."""
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="ordinary card", assignee="implementer")

    out = kc.run_slash("dispatch --dry-run")

    assert "max_review_rounds" not in out
