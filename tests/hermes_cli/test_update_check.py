"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()


def test_check_for_updates_disabled_via_hermes_dev_env(tmp_path, monkeypatch):
    """HERMES_DEV=1 must short-circuit before any cache read or subprocess call."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # Even a fresh cache claiming "3 behind" must be ignored — dev mode wins.
    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DEV", "1")
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result is None
    mock_run.assert_not_called()


def test_check_for_updates_disabled_via_config_flag(tmp_path, monkeypatch):
    """``updates.check_for_updates: false`` in config.yaml disables the check."""
    from hermes_cli.banner import check_for_updates

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DEV", raising=False)

    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value={"updates": {"check_for_updates": False}},
    ):
        with patch("hermes_cli.banner.subprocess.run") as mock_run:
            result = check_for_updates()

    assert result is None
    mock_run.assert_not_called()


def test_update_checks_disabled_defaults_to_false(monkeypatch):
    """With no env guard and default config, checks are NOT disabled."""
    from hermes_cli.banner import _update_checks_disabled

    monkeypatch.delenv("HERMES_DEV", raising=False)
    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value={"updates": {"check_for_updates": True}},
    ):
        assert _update_checks_disabled() is False




def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5




