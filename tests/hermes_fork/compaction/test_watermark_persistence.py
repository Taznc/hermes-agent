"""Durable record round-trip on a real ``SessionDB`` (spec t_07c75c42 §3)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_fork.compaction import watermark as wm
from hermes_state import SessionDB

from .conftest import make_compressor


@pytest.fixture
def db(tmp_path: Path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


def _flush():
    wm._get_executor().submit(lambda: None).result(timeout=5)


def _bound(db: SessionDB, session_id: str):
    cc = make_compressor()
    cc.bind_session_state(db, session_id)
    return cc


def _deferred(db: SessionDB, session_id: str):
    """Bind, raise once, flush the durable write."""
    cc = _bound(db, session_id)
    cc.last_prompt_tokens = 490_000
    plan = wm.plan_raise(cc)
    wm.install_watermark(cc, plan.record)
    wm.schedule_durable_write(cc, plan.record).result(timeout=5)
    return cc


def test_durable_write_and_lazy_load_round_trip(db):
    db.create_session("P", source="tui")
    _deferred(db, "P")
    stored = db.get_session_model_config_value("P", wm.KEY)
    assert stored["tokens"] == 586_800 and stored["v"] == 1 and "session_id" not in stored
    # A fresh compressor (gateway restart + resume) loads it on its first getter read.
    fresh = _bound(db, "P")
    assert fresh.threshold_tokens == 586_800


def test_clear_tombstones_the_durable_record(db):
    db.create_session("P", source="tui")
    cc = _deferred(db, "P")
    wm.clear_watermark(cc)
    _flush()
    assert db.get_session_model_config_value("P", wm.KEY) is wm.CLEARED
    assert cc.threshold_tokens == 484_000
    assert _bound(db, "P").threshold_tokens == 484_000


def _rotate(db: SessionDB, parent: str, child: str) -> None:
    assert db.try_acquire_compression_lock(parent, "holder")
    db.publish_compression_child(
        parent_session_id=parent, child_session_id=child, source="tui",
        messages=[{"role": "user", "content": "[CONTEXT COMPACTION] summary"}], model="test/model",
        model_config={"reasoning_effort": "medium"}, compression_lock_holder="holder",
    )


def test_a_cleared_child_does_not_reinherit_from_its_parent(db):
    db.create_session("P", source="tui")
    cc = _deferred(db, "P")
    _rotate(db, "P", "C")
    cc.bind_session_state(db, "C")
    assert cc.threshold_tokens == 586_800
    wm.clear_watermark(cc)
    _flush()
    assert cc.threshold_tokens == 484_000
    # Restart + resume of the child: the tombstone wins over the parent's record.
    assert _bound(db, "C").threshold_tokens == 484_000


def test_clear_just_before_rotation_is_not_resurrected(db):
    """In-process rotation trusts the compressor's memory, even with the tombstone write still queued."""
    db.create_session("P", source="tui")
    cc = _deferred(db, "P")
    gate = threading.Event()
    wm._get_executor().submit(gate.wait, 5)  # hold the single writer
    try:
        wm.clear_watermark(cc)
        _rotate(db, "P", "C")
        cc.bind_session_state(db, "C")
        assert cc.threshold_tokens == 484_000
    finally:
        gate.set()
    _flush()


@pytest.mark.parametrize("marker", ["_branched_from", "_reset_from"])
def test_branch_and_reset_rows_start_at_the_default(db, marker):
    db.create_session("P", source="tui")
    _deferred(db, "P")
    db.end_session("P", "session_reset" if marker == "_reset_from" else "branched")
    db.create_session("C", source="tui", parent_session_id="P", model_config={marker: "P"})
    assert _bound(db, "C").threshold_tokens == 484_000


def test_rotating_compaction_child_inherits_the_record(db):
    """Legacy rotation publishes the child with the agent's session-init model_config, not a copy of the
    parent's; the lazy load follows the compression lineage instead."""
    db.create_session("P", source="tui")
    cc = _deferred(db, "P")
    assert db.try_acquire_compression_lock("P", "holder")
    db.publish_compression_child(
        parent_session_id="P", child_session_id="C", source="tui",
        messages=[{"role": "user", "content": "[CONTEXT COMPACTION] summary"}], model="test/model",
        model_config={"reasoning_effort": "medium"}, compression_lock_holder="holder",
    )
    cc.bind_session_state(db, "C")  # same compressor rebinds to the child id
    assert cc.threshold_tokens == 586_800
    _flush()
    assert db.get_session_model_config_value("C", wm.KEY)["tokens"] == 586_800
    # And after a restart, the child row is self-contained.
    assert _bound(db, "C").threshold_tokens == 586_800


def test_magicmock_session_db_does_no_load_and_does_not_raise():
    cc = make_compressor()
    cc.bind_session_state(MagicMock(), "S")
    assert cc.threshold_tokens == 484_000
    assert vars(cc).get(wm._SLOT) is None


@pytest.mark.parametrize("raw", ["586800", {"v": 9, "tokens": 1, "context_length": 1},
                                 {"v": 1, "tokens": "lots", "context_length": 1_000_000}])
def test_invalid_durable_record_is_ignored_with_a_warning(db, raw, caplog):
    db.create_session("P", source="tui")
    db.patch_session_model_config("P", {wm.KEY: raw})
    with caplog.at_level(logging.WARNING, logger=wm.__name__):
        cc = _bound(db, "P")
        assert cc.threshold_tokens == 484_000
    assert any("fork_compaction_watermark" in r.getMessage() and "P" in r.getMessage() for r in caplog.records)
