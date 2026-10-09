"""``hermes_fork.plugin_update_activation``: a changed plugin update reaches every serve backend.

Reviewer ask on t_d3791089: ``hermes plugins update`` (git pull and catalog re-pin) and the dashboard
git update never called ``activate_plugin_now``, so the ``serve-peer-activation`` anchor never ran
and both backends kept the old plugin. These tests drive the REAL update entry points (the facade
names the CLI dispatch table and the dashboard route call) against real catalog/custom installs in a
temp HERMES_HOME (``installed`` fixture, real PM transaction), with the same two-backend topology as
``test_serve_peers``: a host owner holding the real host lock and record, and a second backend that
bound second through ``web_server._publish_host_rendezvous`` and wrote a peer record. Only the two
HTTP listeners stand in for the backends' activate endpoints.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.hermes_cli.test_plugin_update_transaction import _version, installed  # noqa: F401
from tests.hermes_fork.test_serve_peers import OWNER_SCRIPT, _Backend
from tests.pm.test_plugin_survival_contract import admission_env  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
ACTIVATE = "/api/dashboard/agent-plugins/activate"

SECOND_BACKEND_SCRIPT = textwrap.dedent("""
    import sys
    import hermes_cli.web_server as web_server
    web_server._SESSION_TOKEN = sys.argv[2]
    web_server._publish_host_rendezvous("127.0.0.1", int(sys.argv[1]))
    print("ready", flush=True)
    sys.stdin.read()
""")


@pytest.fixture
def backends(installed, tmp_path, monkeypatch):
    """Dashboard owns the host record; the Desktop backend bound second and published a peer record."""
    from gateway import host_rendezvous as hr

    _, home, *_ = installed
    lock_dir = tmp_path / "locks"
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(lock_dir))
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    env = {**os.environ, "HERMES_GATEWAY_LOCK_DIR": str(lock_dir), "HERMES_HOME": str(home),
           "HOME": str(tmp_path), "PYTHONPATH": str(REPO)}
    dashboard, desktop = _Backend("dash-token", "dashboard"), _Backend("desk-token", "desktop")
    procs = []
    for script, backend in ((OWNER_SCRIPT, dashboard), (SECOND_BACKEND_SCRIPT, desktop)):
        proc = subprocess.Popen([sys.executable, "-c", script, str(backend.port), backend.token], cwd=REPO,
                                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        procs.append(proc)
        assert proc.stdout.readline().strip() == "ready"
    assert hr.read_record(hr.ROLE_SERVE).port == dashboard.port
    assert len(list(lock_dir.glob("fork-serve-peer-*.json"))) == 1
    try:
        yield installed, dashboard, desktop, home
    finally:
        for proc in procs:
            proc.stdin.close()
            proc.wait(timeout=30)
        dashboard.close()
        desktop.close()


def _posts(backend) -> list:
    return [(path, token, body["name"]) for path, token, body in backend.calls]


@pytest.mark.parametrize("installed", ["catalog", "custom"], indirect=True)
def test_changed_cli_update_activates_both_backends(backends):
    from hermes_cli import plugins_cmd as pc

    (_, _, repo, target, state), dashboard, desktop, _ = backends
    state["sha"] = _version(repo, "2.0.0")

    pc.plugins_command(type("Args", (), {"plugins_action": "update", "name": "transactional"})())

    assert "2.0.0" in (target / "__init__.py").read_text()
    assert _posts(dashboard) == [(ACTIVATE, "dash-token", "transactional")]
    assert _posts(desktop) == [(ACTIVATE, "desk-token", "transactional")]


@pytest.mark.parametrize("installed", ["catalog", "custom"], indirect=True)
def test_noop_cli_update_activates_nothing(backends):
    from hermes_cli import plugins_cmd as pc

    _, dashboard, desktop, _ = backends
    pc.cmd_update("transactional")

    assert dashboard.calls == [] and desktop.calls == []


@pytest.mark.parametrize("installed", ["catalog", "custom"], indirect=True)
def test_failed_cli_update_activates_nothing(backends, monkeypatch):
    from hermes_cli import plugins_cmd as pc

    (_, _, repo, target, state), dashboard, desktop, _ = backends
    state["sha"] = _version(repo, "2.0.0")

    def refuse(staged, source, **kwargs):
        raise pc.PluginScanBlocked("fixture refuses candidate")

    monkeypatch.setattr(pc, "_scan_plugin_tree", refuse)
    with pytest.raises(SystemExit):
        pc.cmd_update("transactional")

    assert "1.0.0" in (target / "__init__.py").read_text()
    assert dashboard.calls == [] and desktop.calls == []


@pytest.mark.parametrize("installed", ["custom"], indirect=True)
def test_unattended_auto_apply_keeps_upstream_behaviour(backends):
    """``plugins_cadence`` auto-apply (interactive=False) is out of scope: no activation added."""
    from hermes_cli import plugins_cmd as pc

    (_, _, repo, target, state), dashboard, desktop, _ = backends
    state["sha"] = _version(repo, "2.0.0")
    pc.cmd_update("transactional", interactive=False)

    assert "2.0.0" in (target / "__init__.py").read_text()
    assert dashboard.calls == [] and desktop.calls == []


@pytest.mark.parametrize("installed", ["catalog", "custom"], indirect=True)
def test_changed_dashboard_update_activates_in_process_and_every_sibling(backends, monkeypatch):
    """The dashboard route runs in one backend: it goes live in-process and POSTs the others once each.
    The catalog branch already activated upstream; the wrapper must not activate it a second time."""
    from hermes_cli import plugins_activation
    from hermes_cli import plugins_cmd as pc

    (_, _, repo, target, state), dashboard, desktop, _ = backends
    in_process = []
    monkeypatch.setattr(plugins_activation, "load_and_go_live",
                        lambda name: in_process.append(name) or {"key": name, "by": "self"})
    state["sha"] = _version(repo, "2.0.0")

    result = pc.dashboard_update_user_plugin("transactional")

    assert result["ok"] is True and result["activation"] == {"key": "transactional", "by": "self"}
    assert "2.0.0" in (target / "__init__.py").read_text()
    assert in_process == ["transactional"]
    assert _posts(dashboard) == [(ACTIVATE, "dash-token", "transactional")]
    assert _posts(desktop) == [(ACTIVATE, "desk-token", "transactional")]


@pytest.mark.parametrize("installed", ["catalog", "custom"], indirect=True)
def test_noop_and_failed_dashboard_update_activate_nothing(backends, monkeypatch):
    from hermes_cli import plugins_activation
    from hermes_cli import plugins_cmd as pc

    (_, _, repo, _, state), dashboard, desktop, _ = backends
    monkeypatch.setattr(plugins_activation, "load_and_go_live", lambda name: pytest.fail("activated"))

    assert pc.dashboard_update_user_plugin("transactional")["ok"] is True  # already at the pin / up to date

    state["sha"] = _version(repo, "2.0.0")

    def refuse(staged, source, **kwargs):
        raise pc.PluginScanBlocked("fixture refuses candidate")

    monkeypatch.setattr(pc, "_scan_plugin_tree", refuse)
    assert pc.dashboard_update_user_plugin("transactional")["ok"] is False
    assert dashboard.calls == [] and desktop.calls == []
