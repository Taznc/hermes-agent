"""Tests for the session-targeted resume seam (``resume_session_id``).

Covers:

* ``create_job(resume_session_id=...)`` shape, validation, and persistence.
* ``update_job`` invariant re-check (can't combine with no_agent/monitor).
* ``scheduler.run_job``'s resume short-circuit: no live gateway, session
  gone, session present (resume + resubmit succeeds).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


# ---------------------------------------------------------------------------
# create_job / update_job: data-layer semantics
# ---------------------------------------------------------------------------


def test_create_job_persists_resume_session_id(hermes_env):
    from cron.jobs import create_job, get_job

    job = create_job(
        prompt="Retry the last turn.",
        schedule="2099-01-01T00:00:00",
        deliver="local",
        resume_session_id="sess-abc123",
    )
    assert job["resume_session_id"] == "sess-abc123"

    reloaded = get_job(job["id"])
    assert reloaded["resume_session_id"] == "sess-abc123"


def test_create_job_omits_resume_session_id_key_when_unset(hermes_env):
    """Absent key for the common case — byte-identical to pre-feature jobs."""
    from cron.jobs import create_job

    job = create_job(prompt="hello", schedule="every 5m", deliver="local")
    assert "resume_session_id" not in job


def test_create_job_rejects_resume_session_id_with_no_agent(hermes_env):
    from cron.jobs import create_job

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")
    with pytest.raises(ValueError, match="resume_session_id cannot be combined with no_agent"):
        create_job(
            prompt=None,
            schedule="every 5m",
            script="w.sh",
            no_agent=True,
            deliver="local",
            resume_session_id="sess-1",
        )


def test_create_job_rejects_resume_session_id_with_monitor(hermes_env):
    from cron.jobs import create_job

    script_path = hermes_env / "scripts" / "mon.sh"
    script_path.write_text("echo hi\n")
    with pytest.raises(ValueError, match="resume_session_id cannot be combined with monitor"):
        create_job(
            prompt="check in",
            schedule="every 5m",
            monitor_script="mon.sh",
            deliver="local",
            resume_session_id="sess-1",
        )


def test_update_job_can_set_and_clear_resume_session_id(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    job = create_job(
        prompt="Retry the last turn.",
        schedule="2099-01-01T00:00:00",
        deliver="local",
    )
    assert "resume_session_id" not in job or job.get("resume_session_id") is None

    update_job(job["id"], {"resume_session_id": "sess-xyz"})
    reloaded = get_job(job["id"])
    assert reloaded["resume_session_id"] == "sess-xyz"

    update_job(job["id"], {"resume_session_id": ""})
    reloaded = get_job(job["id"])
    assert reloaded.get("resume_session_id") is None


def test_update_job_rejects_resume_session_id_combined_with_no_agent(hermes_env):
    from cron.jobs import create_job, update_job

    script_path = hermes_env / "scripts" / "w2.sh"
    script_path.write_text("echo hi\n")
    job = create_job(prompt="hello", schedule="every 5m", deliver="local")
    with pytest.raises(ValueError, match="resume_session_id cannot be combined with no_agent"):
        update_job(
            job["id"],
            {"resume_session_id": "sess-1", "no_agent": True, "script": "w2.sh"},
        )


# ---------------------------------------------------------------------------
# scheduler.run_job: resume short-circuit
# ---------------------------------------------------------------------------


def test_run_job_resume_no_gateway_is_harmless_noop(hermes_env, monkeypatch):
    """Standalone cron tick with no live tui_gateway.server module loaded:
    nothing to resume into — must be a silent success, not an error."""
    import sys
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)

    job = create_job(
        prompt="Retry the last turn.",
        schedule="2099-01-01T00:00:00",
        deliver="local",
        resume_session_id="sess-gone",
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER
    assert "no-op" in doc.lower()


def test_run_job_resume_session_not_found_is_noop(hermes_env, monkeypatch):
    """session.resume returns a 4007 'session not found' error — no crash,
    no error alert, just a recorded no-op."""
    import sys
    import types
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    def fake_resume(rid, params):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": 4007, "message": "session not found"}}

    def fake_submit(rid, params):  # pragma: no cover - must not be reached
        raise AssertionError("prompt.submit must not be called when resume fails")

    fake_server = types.SimpleNamespace(
        _methods={"session.resume": fake_resume, "prompt.submit": fake_submit},
        # The global tests/conftest.py::_reset_tui_gateway_server_state
        # fixture directly accesses mod._sessions in its teardown (its own
        # ordering vs. this test's monkeypatch undo is not guaranteed) —
        # give the fake module the attribute so that access is harmless.
        _sessions={},
    )
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake_server)

    job = create_job(
        prompt="Retry the last turn.",
        schedule="2099-01-01T00:00:00",
        deliver="local",
        resume_session_id="sess-gone",
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER
    assert "session not found" in doc.lower()


def test_run_job_resume_session_present_resumes_and_resubmits(hermes_env, monkeypatch):
    """Happy path: session.resume finds the session, prompt.submit re-fires
    the stored prompt into it — and no new AIAgent/session is constructed."""
    import sys
    import types
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    resume_calls = []
    submit_calls = []

    def fake_resume(rid, params):
        resume_calls.append(params)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"session_id": "ui-live-1", "resumed": params["session_id"]},
        }

    def fake_submit(rid, params):
        submit_calls.append(params)
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "streaming"}}

    fake_server = types.SimpleNamespace(
        _methods={"session.resume": fake_resume, "prompt.submit": fake_submit},
        _sessions={},
    )
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake_server)

    job = create_job(
        prompt="Retry the last turn.",
        schedule="2099-01-01T00:00:00",
        deliver="local",
        resume_session_id="sess-present",
    )
    success, doc, final_response, error = run_job(job)

    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER
    assert "resumed and resubmitted" in doc.lower()

    assert len(resume_calls) == 1
    assert resume_calls[0]["session_id"] == "sess-present"

    assert len(submit_calls) == 1
    assert submit_calls[0]["session_id"] == "ui-live-1"
    assert submit_calls[0]["text"] == "Retry the last turn."


def test_run_job_resume_never_constructs_aiagent(hermes_env, monkeypatch):
    """The resume branch must short-circuit before any AIAgent import/build —
    this is the whole point of the seam (no fresh cron_{job_id}_{ts} session)."""
    import sys
    import types
    from cron.jobs import create_job
    from cron.scheduler import run_job

    def fake_resume(rid, params):
        return {"jsonrpc": "2.0", "id": rid, "result": {"session_id": "ui-live-1"}}

    def fake_submit(rid, params):
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "streaming"}}

    fake_server = types.SimpleNamespace(
        _methods={"session.resume": fake_resume, "prompt.submit": fake_submit},
        _sessions={},
    )
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake_server)

    import run_agent
    boom = MagicMock(side_effect=AssertionError("AIAgent must not be constructed for a resume job"))
    monkeypatch.setattr(run_agent, "AIAgent", boom)

    job = create_job(
        prompt="Retry the last turn.",
        schedule="2099-01-01T00:00:00",
        deliver="local",
        resume_session_id="sess-present",
    )
    success, _doc, _final_response, error = run_job(job)
    assert success is True
    assert error is None
    boom.assert_not_called()


def test_run_job_resume_empty_prompt_is_noop(hermes_env, monkeypatch):
    """A resume job with a blank stored prompt has nothing to resubmit."""
    import sys
    import types
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    def fake_resume(rid, params):
        return {"jsonrpc": "2.0", "id": rid, "result": {"session_id": "ui-live-1"}}

    def fake_submit(rid, params):  # pragma: no cover - must not be reached
        raise AssertionError("prompt.submit must not be called with an empty prompt")

    fake_server = types.SimpleNamespace(
        _methods={"session.resume": fake_resume, "prompt.submit": fake_submit},
        _sessions={},
    )
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake_server)

    # Bypass create_job's own empty-payload guard by editing the stored
    # record directly — a resume job's prompt could still be hand-cleared.
    from cron.jobs import update_job

    job = create_job(
        prompt="placeholder",
        schedule="2099-01-01T00:00:00",
        deliver="local",
        resume_session_id="sess-present",
    )
    # update_job's _PAYLOAD_FIELDS guard would reject a blank prompt on a
    # normal job, but this job is exempt because job_payload_is_empty only
    # gates the pre-resume fail-closed check in run_job, which we short
    # circuit before reaching. Simulate a legacy/hand-edited record.
    from cron.jobs import load_jobs, save_jobs, _jobs_lock

    with _jobs_lock():
        jobs = load_jobs()
        for j in jobs:
            if j["id"] == job["id"]:
                j["prompt"] = "   "
        save_jobs(jobs)

    from cron.jobs import get_job
    reloaded = get_job(job["id"])
    success, doc, final_response, error = run_job(reloaded)
    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER
    assert "nothing to resubmit" in doc.lower()
