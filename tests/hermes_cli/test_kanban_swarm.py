import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_model_routing import KanbanModelRouteDecision
from hermes_cli.kanban_swarm import (
    SwarmWorkerSpec,
    create_swarm,
    latest_blackboard,
    post_blackboard_update,
)


def test_create_swarm_builds_parallel_workers_verifier_and_synthesizer(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Map the target market and produce a decision memo.",
            workers=[
                SwarmWorkerSpec(profile="researcher-a", title="Market scan", body="Find competitors"),
                SwarmWorkerSpec(profile="researcher-b", title="Customer scan", body="Find customer pains"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            tenant="intel",
            created_by="orchestrator",
        )

        root = kb.get_task(conn, created.root_id)
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        assert root is not None
        assert all(task is not None for task in workers)
        workers = [task for task in workers if task is not None]
        assert verifier is not None
        assert synthesizer is not None
        assert root.status == "done"
        assert root.assignee == "orchestrator"
        assert [task.status for task in workers] == ["ready", "ready"]
        assert [task.assignee for task in workers] == ["researcher-a", "researcher-b"]
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"
        assert set(kb.parent_ids(conn, created.verifier_id)) == set(created.worker_ids)
        assert kb.parent_ids(conn, created.synthesizer_id) == [created.verifier_id]
        assert all(created.root_id in (task.body or "") for task in workers)
    finally:
        conn.close()


def test_create_swarm_graph_is_atomic_and_rolls_back_partial_build(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "kanban.db"
    writer = kbc.connect(db_path)
    reader = kbc.connect(db_path)
    original_create = kb.create_task
    original_complete = kb.complete_task
    calls = 0

    def observed_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        task_id = original_create(*args, **kwargs)
        if calls == 1:
            # Releasing the nested create_task savepoint must not expose the
            # root before the whole graph's outer transaction commits.
            visible = reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            assert visible == 0
        if calls == 3:
            raise RuntimeError("synthetic graph-construction failure")
        return task_id

    monkeypatch.setattr(kb, "create_task", observed_create)
    try:
        with pytest.raises(RuntimeError, match="synthetic graph-construction failure"):
            create_swarm(
                writer,
                goal="Build atomically",
                workers=[
                    SwarmWorkerSpec(profile="worker-a", title="A", body="A"),
                    SwarmWorkerSpec(profile="worker-b", title="B", body="B"),
                ],
                verifier_assignee="reviewer",
                synthesizer_assignee="writer",
            )
        assert writer.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        monkeypatch.setattr(kb, "create_task", original_create)
        import hermes_cli.kanban_swarm as ks

        original_activate = ks._activate_root_inline
        monkeypatch.setattr(
            ks, "_activate_root_inline", lambda *args, **kwargs: False
        )
        with pytest.raises(RuntimeError, match="could not activate"):
            create_swarm(
                writer,
                goal="Fail activation atomically",
                workers=[
                    SwarmWorkerSpec(profile="worker-a", title="A", body="A"),
                ],
                verifier_assignee="reviewer",
                synthesizer_assignee="writer",
            )
        assert writer.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        hooks: list[tuple[str, bool]] = []
        monkeypatch.setattr(ks, "_activate_root_inline", original_activate)
        monkeypatch.setattr(
            kb,
            "_fire_kanban_lifecycle_hook",
            lambda event, *_args, **_kwargs: hooks.append(
                (event, writer.in_transaction)
            ),
        )
        create_swarm(
            writer,
            goal="Commit before lifecycle hook",
            workers=[SwarmWorkerSpec(profile="worker-a", title="A", body="A")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )
        assert hooks == [("kanban_task_completed", False)]
    finally:
        reader.close()
        writer.close()


def test_create_swarm_applies_routing_to_every_new_card(tmp_path, monkeypatch):
    conn = kbc.connect(tmp_path / "kanban.db")
    calls: list[dict[str, object]] = []

    route = KanbanModelRouteDecision(
        route_source="mechanical",
        route_name="mechanical",
        model_override="gpt-5.4-mini",
        provider_override="openai-codex",
        reasoning_effort="medium",
    )

    def _fake_resolver(**kwargs):
        calls.append(kwargs)
        return route

    monkeypatch.setattr("hermes_cli.kanban_model_routing.resolve_kanban_model_route", _fake_resolver)
    try:
        created = create_swarm(
            conn,
            goal="Collect evidence for the launch memo.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Research", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            root_title="Swarm root",
            created_by="orchestrator",
            idempotency_key="swarm-routing-demo",
        )

        root = kb.get_task(conn, created.root_id)
        worker = kb.get_task(conn, created.worker_ids[0])
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        assert root is not None and worker is not None and verifier is not None and synthesizer is not None
        assert len(calls) == 4
        assert [call["title"] for call in calls] == ["Swarm root", "Research", "Verify swarm outputs", "Synthesize swarm outputs"]
        assert all(set(call) == {"title", "body"} for call in calls)
        assert root.route_source == "mechanical"
        assert root.route_name == "mechanical"
        assert root.model_override == "gpt-5.4-mini"
        assert worker.route_source == "mechanical"
        assert worker.route_name == "mechanical"
        assert verifier.route_source == "mechanical"
        assert verifier.route_name == "mechanical"
        assert synthesizer.route_source == "mechanical"
        assert synthesizer.route_name == "mechanical"
        assert root.status == "done"
        assert worker.status == "ready"
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"
    finally:
        conn.close()


def test_create_swarm_idempotent_return_skips_rerouting(tmp_path, monkeypatch):
    conn = kbc.connect(tmp_path / "kanban.db")
    calls: list[dict[str, object]] = []

    route = KanbanModelRouteDecision(
        route_source="mechanical",
        route_name="mechanical",
        model_override="gpt-5.4-mini",
        provider_override="openai-codex",
        reasoning_effort="medium",
    )

    def _fake_resolver(**kwargs):
        calls.append(kwargs)
        return route

    monkeypatch.setattr("hermes_cli.kanban_model_routing.resolve_kanban_model_route", _fake_resolver)
    try:
        created = create_swarm(
            conn,
            goal="Keep the same plan.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Research", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            root_title="Swarm root",
            created_by="orchestrator",
            idempotency_key="swarm-routing-idempotent",
        )
        first_call_count = len(calls)
        assert first_call_count == 4

        repeated = create_swarm(
            conn,
            goal="Keep the same plan.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Research", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            root_title="Swarm root",
            created_by="orchestrator",
            idempotency_key="swarm-routing-idempotent",
        )

        assert repeated == created
        assert len(calls) == first_call_count
    finally:
        conn.close()


def test_create_swarm_rechecks_topology_after_idempotent_root_create(tmp_path, monkeypatch):
    """A caller that raced past preflight must not append a second graph."""
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        original = create_swarm(
            conn,
            goal="Keep exactly one graph.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Research", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            idempotency_key="swarm-race",
        )
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        # Simulate a competing caller that did its preflight before the first
        # caller committed. Its root create still resolves to the existing
        # idempotency-keyed task, where topology is now available.
        monkeypatch.setattr(
            "hermes_cli.kanban_swarm._existing_swarm_from_idempotency_key",
            lambda *_args, **_kwargs: (None, None),
        )
        repeated = create_swarm(
            conn,
            goal="Keep exactly one graph.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Research", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            idempotency_key="swarm-race",
        )

        assert repeated == original
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    finally:
        conn.close()


def test_plain_write_txn_nesting_raises_and_allow_nested_composes(tmp_path):
    """B1 regression: nesting is explicit opt-in, never silent.

    Plain ``write_txn`` inside an open transaction must raise loudly (the
    historical invariant). ``allow_nested=True`` composes via a savepoint,
    and an outer rollback discards the inner work without any post-commit
    side effects having fired (the workspace directory survives).
    """
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        workspace = tmp_path / "scratch-ws"
        workspace.mkdir()
        tid = kb.create_task(conn, title="ws task", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (str(workspace), tid),
            )

        # 1) Plain nesting raises loudly.
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            with kb.write_txn(conn):
                with kb.write_txn(conn):
                    pass
        assert not conn.in_transaction

        # 2) allow_nested composes; outer rollback discards inner work
        #    and no side effects (workspace cleanup) fired meanwhile.
        with pytest.raises(RuntimeError, match="outer failure"):
            with kb.write_txn(conn):
                with kb.write_txn(conn, allow_nested=True):
                    conn.execute(
                        "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,)
                    )
                    kb._append_event(conn, tid, "completed", {"result_len": 0})
                # Inner savepoint released, but the outer txn now fails.
                raise RuntimeError("outer failure")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"  # inner 'done' flip was discarded
        assert not any(
            e.kind == "completed" for e in kb.list_events(conn, tid)
        )
        assert workspace.is_dir()  # no _cleanup_workspace side effect fired
    finally:
        conn.close()


def test_swarm_blackboard_merges_structured_updates(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Collect evidence.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Evidence", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        post_blackboard_update(
            conn,
            created.root_id,
            author="researcher",
            key="sources",
            value=["https://example.com/a"],
        )
        post_blackboard_update(
            conn,
            created.root_id,
            author="reviewer",
            key="risks",
            value={"missing_primary_source": True},
        )

        board = latest_blackboard(conn, created.root_id)
        assert board["sources"] == ["https://example.com/a"]
        assert board["risks"] == {"missing_primary_source": True}
        assert board["_authors"]["sources"] == "researcher"
    finally:
        conn.close()


def test_swarm_verifier_and_synthesis_are_dependency_gated(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Research two branches then verify and synthesize.",
            workers=[
                SwarmWorkerSpec(profile="a", title="Branch A", body="A"),
                SwarmWorkerSpec(profile="b", title="Branch B", body="B"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        kb.complete_task(
            conn,
            created.worker_ids[0],
            summary="A done",
            metadata={"confidence": 0.8},
        )
        kb.recompute_ready(conn)
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert verifier is not None
        assert synthesizer is not None
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"

        kb.complete_task(conn, created.worker_ids[1], summary="B done")
        kb.recompute_ready(conn)
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert verifier is not None
        assert synthesizer is not None
        assert verifier.status == "ready"
        assert synthesizer.status == "todo"

        kb.complete_task(
            conn,
            created.verifier_id,
            summary="Verified both branches",
            metadata={"gate": "pass"},
        )
        kb.recompute_ready(conn)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert synthesizer is not None
        assert synthesizer.status == "ready"
    finally:
        conn.close()
