"""A resumed session keeps the surface stored on its row (tools/AGENTS.md: surface is a property of the
SESSION, never of the process env).

Regression: a Desktop chat resumed by a client that omitted ``source`` (the session-tile delegate, a
reconnect after a backend restart) against a backend without ``HERMES_DESKTOP=1`` (the web-desktop
backend unit) ran as ``platform=tui`` — Desktop-only tools then answered "unsupported".
"""

import pytest

from tui_gateway import server


@pytest.fixture
def resume_env(monkeypatch, tmp_path):
    from hermes_state import SessionDB

    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid: None)
    try:
        yield db
    finally:
        server._sessions.clear()
        db.close()


def _resume(params: dict) -> dict:
    resp = server.handle_request({"id": "1", "method": "session.resume", "params": params})
    assert "result" in resp, resp
    return server._sessions[resp["result"]["session_id"]]


def test_resume_without_source_keeps_the_stored_desktop_surface(resume_env):
    resume_env.create_session("row-desktop", source="desktop")
    record = _resume({"session_id": "row-desktop"})
    assert record["source"] == "desktop"
    assert server._resolve_agent_platform(record["source"]) == "desktop"
    assert resume_env.get_session("row-desktop")["source"] == "desktop"


def test_explicit_source_still_wins_over_the_stored_row(resume_env):
    resume_env.create_session("row-desktop", source="desktop")
    assert _resume({"session_id": "row-desktop", "source": "tui"})["source"] == "tui"


def test_rowless_resume_falls_back_to_the_process_env(resume_env, monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    from hermes_fork.gateway.session_surface import resume_source

    assert resume_source({}, None) == "desktop"
    assert resume_source({}, {"source": ""}) == "desktop"
    monkeypatch.delenv("HERMES_DESKTOP")
    assert resume_source({}, {}) == "tui"
    assert resume_source({}, {"source": "telegram"}) == "telegram"


def test_the_gateway_anchor_installs_the_fix_once():
    """Installed by ``gateway-fork-methods`` (server import), idempotently, on upstream's ``_Resume.mint``."""
    from hermes_fork.gateway import session_surface
    from tui_gateway import methods_session

    mint = methods_session._Resume.mint
    assert getattr(mint, "__fork_wrapped__", None) is not None, "gateway anchor did not install the resume fix"
    session_surface.install()
    assert methods_session._Resume.mint is mint
