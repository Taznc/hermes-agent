"""Fork ``sessions.rate_limit_default_recovery`` config round-trip tests
(moved from tests/test_hermes_state.py; implementation in
``hermes_fork.state_limits``, public name on ``hermes_state``)."""

import hermes_state


class TestResolvedRateLimitDefaultRecovery:
    """Config round-trip for ``sessions.rate_limit_default_recovery``
    (Phase 2.12) — same pattern as ``resolved_max_resume_messages`` /
    ``resolved_max_export_messages`` in hermes_state."""

    @staticmethod
    def _patch_cfg(monkeypatch, cfg):
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: cfg,
        )

    def test_default_is_ask_when_unset(self, monkeypatch):
        self._patch_cfg(monkeypatch, {})
        assert hermes_state.resolved_rate_limit_default_recovery() == "ask"

    def test_reads_configured_resume_at_reset(self, monkeypatch):
        self._patch_cfg(
            monkeypatch,
            {"sessions": {"rate_limit_default_recovery": "resume_at_reset"}},
        )
        assert (
            hermes_state.resolved_rate_limit_default_recovery()
            == "resume_at_reset"
        )

    def test_reads_configured_ask_explicitly(self, monkeypatch):
        self._patch_cfg(
            monkeypatch, {"sessions": {"rate_limit_default_recovery": "ask"}}
        )
        assert hermes_state.resolved_rate_limit_default_recovery() == "ask"

    def test_unrecognized_value_falls_back_to_ask(self, monkeypatch):
        """An unrecognized value must not silently enable unattended
        auto-resume -- fall back to the safe "ask" default."""
        self._patch_cfg(
            monkeypatch,
            {"sessions": {"rate_limit_default_recovery": "auto_resume_always"}},
        )
        assert hermes_state.resolved_rate_limit_default_recovery() == "ask"

    def test_config_unavailable_falls_back_to_ask(self, monkeypatch):
        def _boom():
            raise RuntimeError("config subsystem unavailable")

        monkeypatch.setattr("hermes_cli.config.load_config_readonly", _boom)
        assert hermes_state.resolved_rate_limit_default_recovery() == "ask"
