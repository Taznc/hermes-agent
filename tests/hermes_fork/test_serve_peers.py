"""``hermes_fork.serve_peers``: plugin activation reaches every serve backend on the host.

Two-backend topology from 2026-09-29: the supervised dashboard owns the ``serve`` host record and
the web-Desktop ``hermes serve`` bound second (HELD_BY_OTHER, "observe-only"), so
``hermes plugins enable`` only ever dialed the dashboard. Everything here runs on a scratch
HERMES_HOME and a scratch rendezvous dir with REAL host records: the owner is a separate process
holding the real host lock, the second backend goes through the ``serve-peer-record`` anchor in
``web_server._publish_host_rendezvous``, and the CLI side runs ``activate_plugin_now`` (the
``serve-peer-activation`` anchor) in its own process, as ``hermes plugins enable`` does. Only the
two HTTP listeners are stand-ins for the backends' ``/api/dashboard/agent-plugins/activate``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gateway import host_rendezvous as hr
from hermes_fork import serve_peers

REPO = Path(__file__).resolve().parents[2]

OWNER_SCRIPT = textwrap.dedent("""
    import sys
    from gateway import host_rendezvous as hr
    outcome, _ = hr.claim_host_lock(hr.ROLE_SERVE)
    assert outcome is hr.HostLockOutcome.ACQUIRED, outcome
    hr.publish_record(hr.ROLE_SERVE, host="0.0.0.0", port=int(sys.argv[1]), token=sys.argv[2],
                      profiles=("default",))
    print("ready", flush=True)
    sys.stdin.read()
""")

CLI_SCRIPT = textwrap.dedent("""
    import json
    from hermes_cli.plugins_activation import activate_plugin_now
    print(json.dumps(activate_plugin_now("demo", in_process=False)))
""")


class _Backend:
    """A loopback listener standing in for one backend's activate endpoint."""

    def __init__(self, token: str, label: str):
        self.token, self.label, self.calls = token, label, []
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                backend.calls.append((self.path, self.headers.get("X-Hermes-Session-Token"), body))
                ok = self.headers.get("X-Hermes-Session-Token") == backend.token
                payload = json.dumps({"ok": True, "activation": {"key": "demo", "by": backend.label}}
                                     if ok else {"detail": "Unauthorized"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def topology(monkeypatch, tmp_path):
    """Scratch rendezvous dir + HERMES_HOME; yields a helper that starts the host owner."""
    lock_dir = tmp_path / "locks"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(lock_dir))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    env = {**os.environ, "HERMES_GATEWAY_LOCK_DIR": str(lock_dir), "HERMES_HOME": str(home),
           "PYTHONPATH": str(REPO)}
    started: list = []

    def start_owner(port: int, token: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", OWNER_SCRIPT, str(port), token], cwd=REPO, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        started.append(proc)
        assert proc.stdout.readline().strip() == "ready"
        return proc

    def run_cli() -> dict:
        out = subprocess.run([sys.executable, "-c", CLI_SCRIPT], cwd=REPO, env=env, capture_output=True,
                             text=True, timeout=120, check=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    yield type("T", (), {"lock_dir": lock_dir, "home": home, "start_owner": staticmethod(start_owner),
                         "run_cli": staticmethod(run_cli)})
    serve_peers.remove_own_peer_records()
    for proc in started:
        proc.stdin.close()
        proc.wait(timeout=10)


def _publish_second_backend(monkeypatch, port: int, token: str) -> None:
    """This process plays the web-Desktop ``hermes serve`` that binds after the dashboard."""
    import hermes_cli.web_server as web_server

    monkeypatch.setattr(web_server, "_SESSION_TOKEN", token)
    monkeypatch.setattr(hr, "cleanup_on_exit", lambda role: None)
    monkeypatch.setattr(serve_peers, "_prepend_terminate_handler", lambda: None)
    web_server._publish_host_rendezvous("127.0.0.1", port)


def test_one_enable_activates_both_backends(topology, monkeypatch):
    """AC1/AC2: dashboard owns the host record, Desktop backend bound second — both get the POST."""
    dashboard, desktop = _Backend("dash-token", "dashboard"), _Backend("desk-token", "desktop")
    try:
        owner = topology.start_owner(dashboard.port, dashboard.token)
        _publish_second_backend(monkeypatch, desktop.port, desktop.token)

        # Upstream still sees exactly one host owner: the dashboard, untouched by the fork.
        record = hr.read_record(hr.ROLE_SERVE)
        assert (record.pid, record.port) == (owner.pid, dashboard.port)
        peer = topology.lock_dir / f"fork-serve-peer-{os.getpid()}.json"
        assert stat.S_IMODE(peer.stat().st_mode) == 0o600
        assert json.loads(peer.read_text())["port"] == desktop.port

        result = topology.run_cli()

        body = {"name": "demo", "home": str(topology.home)}
        path = "/api/dashboard/agent-plugins/activate"
        assert dashboard.calls == [(path, "dash-token", body)]
        assert desktop.calls == [(path, "desk-token", body)]
        assert result["activation"] == {"key": "demo", "by": "dashboard"}
    finally:
        dashboard.close()
        desktop.close()


def test_peer_answers_when_the_host_owner_is_gone(topology, monkeypatch):
    """Only the Desktop backend is up (its peer record outlived the owner): it still activates, and
    the CLI reports its answer instead of "restart required"."""
    desktop = _Backend("desk-token", "desktop")
    try:
        owner = topology.start_owner(1, "dead-owner-token")
        _publish_second_backend(monkeypatch, desktop.port, desktop.token)
        owner.stdin.close()
        owner.wait(timeout=10)

        result = topology.run_cli()

        assert [c[1] for c in desktop.calls] == ["desk-token"]
        assert result["activation"] == {"key": "demo", "by": "desktop"}
    finally:
        desktop.close()


def test_single_backend_path_is_unchanged(topology):
    """AC3: one backend, no peers — exactly the one upstream POST, nothing extra."""
    dashboard = _Backend("dash-token", "dashboard")
    try:
        topology.start_owner(dashboard.port, dashboard.token)
        assert list(topology.lock_dir.glob("fork-serve-peer-*.json")) == []

        result = topology.run_cli()

        assert [c[1] for c in dashboard.calls] == ["dash-token"]
        assert result["activation"] == {"key": "demo", "by": "dashboard"}
    finally:
        dashboard.close()


def test_in_process_enable_on_one_backend_reaches_its_sibling(topology, monkeypatch):
    """Enabling from the Desktop backend's own plugin UI (in_process) also tells the dashboard."""
    from hermes_cli import plugins_activation

    dashboard = _Backend("dash-token", "dashboard")
    try:
        topology.start_owner(dashboard.port, dashboard.token)
        monkeypatch.setattr(plugins_activation, "load_and_go_live", lambda name: {"key": name, "by": "self"})

        result = plugins_activation.activate_plugin_now("demo")

        assert [c[1] for c in dashboard.calls] == ["dash-token"]
        assert result["activation"] == {"key": "demo", "by": "self"}
    finally:
        dashboard.close()


def test_dead_and_foreign_peer_records_are_ignored(topology):
    hr.ensure_host_state_dir()
    dead = topology.lock_dir / "fork-serve-peer-999999.json"
    dead.write_text(json.dumps({"pid": 999999, "createTime": 1.0, "host": "127.0.0.1", "port": 1,
                                "token": "t"}))
    (topology.lock_dir / "fork-serve-peer-junk.json").write_text("not json")
    (topology.lock_dir / "fork-serve-peer-list.json").write_text("[]")

    assert serve_peers.live_peers() == []
    assert not dead.exists()


def test_peer_record_is_removed_on_exit(topology):
    path = serve_peers.publish_serve_peer("127.0.0.1", 4321, "tok")
    assert path is not None and path.exists()

    serve_peers.remove_own_peer_records()

    assert not path.exists()
    assert serve_peers.publish_serve_peer("127.0.0.1", None, "tok") is None
    assert serve_peers.publish_serve_peer("127.0.0.1", 4321, "") is None
