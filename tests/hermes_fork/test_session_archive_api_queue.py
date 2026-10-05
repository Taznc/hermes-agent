"""Owned queued runs must block even before agent creation/turn lease admission."""

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB


def test_queued_structured_api_run_blocks_its_session(tmp_path):
    from gateway.config import PlatformConfig
    from gateway.platforms import api_server_runs
    from gateway.platforms.api_server import APIServerAdapter

    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", "api_server")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    token = set_hermes_home_override(tmp_path)
    try:
        api_server_runs._set_run_status(
            adapter, "run-test", "queued", session_id="parent"
        )
        with pytest.raises(ValueError, match="active owned work"):
            db.set_session_archived("parent", True)
        api_server_runs._set_run_status(
            adapter, "run-test", "completed", session_id="parent"
        )
        assert db.set_session_archived("parent", True)
    finally:
        reset_hermes_home_override(token)
        db.close()


def test_foreign_profile_queued_run_does_not_block_same_id(tmp_path):
    from gateway.config import PlatformConfig
    from gateway.platforms import api_server_runs
    from gateway.platforms.api_server import APIServerAdapter

    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", "api_server")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    token = set_hermes_home_override(tmp_path / "foreign")
    try:
        api_server_runs._set_run_status(
            adapter, "run-foreign", "queued", session_id="parent"
        )
        assert db.set_session_archived("parent", True)
    finally:
        reset_hermes_home_override(token)
        db.close()
