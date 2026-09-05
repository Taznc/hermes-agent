"""Cross-profile review dispatch must not leak the implementer's pinned
``model_override``/``provider_override`` onto the reviewer's worker.

Bug: ``request_review`` reassigned the card to the reviewer profile but left
the override columns untouched, so ``_worker_argv`` (``kanban_db_dispatch.py``)
unconditionally pinned the REVIEWER's worker to the IMPLEMENTER's model —
silently destroying cross-model review independence (a Codex reviewer
running review on Anthropic Opus because the implementer had pinned Opus).

Fix: the pin is card-scoped in storage but implementation-scoped in meaning.
``request_review`` now clears the columns for a cross-profile handoff
(reviewer != implementer) and snapshots the implementer's values onto the
``review_requested`` event payload; ``request_changes`` restores them when
routing back to the implementer. Same-profile review (reviewer == implementer,
or no reviewer reassignment at all) is not a cross-profile leak and keeps the
override untouched throughout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _spawn_and_capture(monkeypatch, tmp_path, task):
    """Build the real worker argv via ``_worker_argv`` (acceptance criterion 1
    requires asserting on the argv, not just DB columns)."""
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4245

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kbd._default_spawn(task, str(workspace))
    return captured["cmd"]


def _model_flags(cmd: list[str]) -> tuple[str | None, str | None]:
    model = cmd[cmd.index("-m") + 1] if "-m" in cmd else None
    provider = cmd[cmd.index("--provider") + 1] if "--provider" in cmd else None
    return model, provider


# ---------------------------------------------------------------------------
# 1. Cross-profile review runs the REVIEWER's own model, not the implementer's
# ---------------------------------------------------------------------------


def test_cross_profile_review_does_not_inherit_implementer_model(
    kanban_home: Path, monkeypatch, tmp_path,
) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="impl", assignee="claudeprimary",
            model_override="claude-opus-5", provider_override="anthropic",
        )
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="done", reviewer="codexreview",
            expected_run_id=claimed.current_run_id,
        ) is True

        t = kb.get_task(conn, tid)
        assert t.status == "review"
        assert t.assignee == "codexreview"
        # DB columns are cleared for the cross-profile review run.
        assert t.model_override is None
        assert t.provider_override is None

        review_run = kb.claim_review_task(conn, tid)
        assert review_run is not None
        assert review_run.model_override is None
        assert review_run.provider_override is None

        cmd = _spawn_and_capture(monkeypatch, tmp_path, review_run)
        model, provider = _model_flags(cmd)
        assert model is None, "reviewer worker must run its own profile's model"
        assert provider is None


# ---------------------------------------------------------------------------
# 2. request_changes restores the implementer's pin on the round trip back
# ---------------------------------------------------------------------------


def test_request_changes_restores_implementer_override(
    kanban_home: Path, monkeypatch, tmp_path,
) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="impl", assignee="claudeprimary",
            model_override="claude-opus-5", provider_override="anthropic",
        )
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="done", reviewer="codexreview",
            expected_run_id=claimed.current_run_id,
        ) is True

        review_run = kb.claim_review_task(conn, tid)
        assert review_run is not None
        ok, implementer = kb.request_changes(
            conn, tid, reason="needs work", expected_run_id=review_run.current_run_id,
        )
        assert ok is True
        assert implementer == "claudeprimary"

        t = kb.get_task(conn, tid)
        assert t.assignee == "claudeprimary"
        assert t.model_override == "claude-opus-5", "implementer's pin must be restored"
        assert t.provider_override == "anthropic"

        # And the restored pin actually reaches the worker argv on reclaim.
        reclaimed = kb.claim_task(conn, tid, claimer="claudeprimary:retry")
        assert reclaimed is not None
        cmd = _spawn_and_capture(monkeypatch, tmp_path, reclaimed)
        model, provider = _model_flags(cmd)
        assert model == "claude-opus-5"
        assert provider == "anthropic"


# ---------------------------------------------------------------------------
# 3. Re-review after changes_requested still works (provenance intact)
# ---------------------------------------------------------------------------


def test_rereview_after_changes_requested_still_works(
    kanban_home: Path, monkeypatch, tmp_path,
) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="impl", assignee="claudeprimary",
            model_override="claude-opus-5", provider_override="anthropic",
        )
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="v1", reviewer="codexreview",
            expected_run_id=claimed.current_run_id,
        ) is True
        review_run = kb.claim_review_task(conn, tid)
        kb.request_changes(
            conn, tid, reason="fix it", expected_run_id=review_run.current_run_id,
        )

        # Implementer reclaims, fixes, requests review again with NO explicit
        # reviewer= (relies on _prior_reviewer provenance).
        retry = kb.claim_task(conn, tid, claimer="claudeprimary:retry2")
        assert retry is not None
        assert retry.model_override == "claude-opus-5"
        ok, reason = kb.request_review(
            conn, tid, summary="v2", expected_run_id=retry.current_run_id,
            with_reason=True,
        )
        assert ok is True, reason

        t = kb.get_task(conn, tid)
        assert t.assignee == "codexreview"
        # Second cross-profile handoff cleared the columns again.
        assert t.model_override is None
        assert t.provider_override is None

        review_run2 = kb.claim_review_task(conn, tid)
        assert review_run2 is not None
        cmd = _spawn_and_capture(monkeypatch, tmp_path, review_run2)
        model, _provider = _model_flags(cmd)
        assert model is None

        # And a second round trip back still restores the pin.
        ok2, implementer2 = kb.request_changes(
            conn, tid, reason="one more pass", expected_run_id=review_run2.current_run_id,
        )
        assert ok2 is True
        assert implementer2 == "claudeprimary"
        t2 = kb.get_task(conn, tid)
        assert t2.model_override == "claude-opus-5"
        assert t2.provider_override == "anthropic"


# ---------------------------------------------------------------------------
# 4. A card with NO override is unaffected in either direction
# ---------------------------------------------------------------------------


def test_no_override_card_unaffected(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="claudeprimary")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="done", reviewer="codexreview",
            expected_run_id=claimed.current_run_id,
        ) is True
        t = kb.get_task(conn, tid)
        assert t.model_override is None
        assert t.provider_override is None

        review_run = kb.claim_review_task(conn, tid)
        ok, implementer = kb.request_changes(
            conn, tid, reason="fix", expected_run_id=review_run.current_run_id,
        )
        assert ok is True
        assert implementer == "claudeprimary"
        t2 = kb.get_task(conn, tid)
        assert t2.model_override is None
        assert t2.provider_override is None


# ---------------------------------------------------------------------------
# 5. Same-profile review keeps the override — not a cross-profile leak
# ---------------------------------------------------------------------------


def test_same_profile_review_keeps_override(
    kanban_home: Path, monkeypatch, tmp_path,
) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="impl", assignee="claudeprimary",
            model_override="claude-opus-5", provider_override="anthropic",
        )
        claimed = kb.claim_task(conn, tid)
        # Explicit self-review: reviewer == implementer.
        assert kb.request_review(
            conn, tid, summary="done", reviewer="claudeprimary",
            expected_run_id=claimed.current_run_id,
        ) is True
        t = kb.get_task(conn, tid)
        assert t.assignee == "claudeprimary"
        assert t.model_override == "claude-opus-5"
        assert t.provider_override == "anthropic"

        review_run = kb.claim_review_task(conn, tid)
        assert review_run.model_override == "claude-opus-5"
        cmd = _spawn_and_capture(monkeypatch, tmp_path, review_run)
        model, provider = _model_flags(cmd)
        assert model == "claude-opus-5"
        assert provider == "anthropic"


def test_review_with_no_reviewer_reassignment_keeps_override(kanban_home: Path) -> None:
    """``reviewer=None`` with no prior reviewer provenance leaves ``assignee``
    (and therefore the override) untouched — not a profile handoff at all."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="impl", assignee="claudeprimary",
            model_override="claude-opus-5", provider_override="anthropic",
        )
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="done", expected_run_id=claimed.current_run_id,
        ) is True
        t = kb.get_task(conn, tid)
        assert t.assignee == "claudeprimary"
        assert t.model_override == "claude-opus-5"
        assert t.provider_override == "anthropic"


# ---------------------------------------------------------------------------
# 6. Explicit reviewer-specific override wins over the cleared value
# ---------------------------------------------------------------------------


def test_explicit_reviewer_override_wins(
    kanban_home: Path, monkeypatch, tmp_path,
) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="impl", assignee="claudeprimary",
            model_override="claude-opus-5", provider_override="anthropic",
        )
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="done", reviewer="codexreview",
            expected_run_id=claimed.current_run_id,
            reviewer_model_override="gpt-5.6-sol", reviewer_provider_override="openai-codex",
        ) is True

        t = kb.get_task(conn, tid)
        assert t.model_override == "gpt-5.6-sol"
        assert t.provider_override == "openai-codex"

        review_run = kb.claim_review_task(conn, tid)
        cmd = _spawn_and_capture(monkeypatch, tmp_path, review_run)
        model, provider = _model_flags(cmd)
        assert model == "gpt-5.6-sol"
        assert provider == "openai-codex"

        # And the implementer's original pin still survives the round trip
        # back — the reviewer-specific override doesn't clobber the snapshot.
        ok, implementer = kb.request_changes(
            conn, tid, reason="fix", expected_run_id=review_run.current_run_id,
        )
        assert ok is True
        assert implementer == "claudeprimary"
        t2 = kb.get_task(conn, tid)
        assert t2.model_override == "claude-opus-5"
        assert t2.provider_override == "anthropic"
