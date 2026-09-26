"""The ``gateway-fork-methods`` anchor: fork methods reach the live dispatch table with their own wire
contracts, and an empty fork package registers nothing."""

from __future__ import annotations

import sys
import textwrap

import pytest

import hermes_fork.gateway as fork_gateway
from tui_gateway.contracts import registry


@pytest.fixture
def server():
    from tui_gateway import server as mod
    return mod


@pytest.fixture
def isolated_catalog(monkeypatch):
    """Private contract table + declaration registry, so synthetic fork packages leave no trace."""
    monkeypatch.setattr(registry, "METHODS", dict(registry.METHODS))
    monkeypatch.setattr(fork_gateway, "_DECLARED", dict(fork_gateway._DECLARED))
    return registry


class _RecordingServer:
    def __init__(self):
        self.registered: dict = {}

    def register_method(self, name, fn):
        self.registered[name] = fn


def _make_package(tmp_path, monkeypatch, name: str, files: dict[str, str]) -> str:
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for fname, body in files.items():
        (pkg / fname).write_text(textwrap.dedent(body))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)
    return name


def test_server_import_registers_fork_ping_with_a_contract(server):
    assert "fork.ping" in server._methods
    contract = registry.METHODS["fork.ping"]
    assert contract.result.model_validate({"ok": True, "branch": "next"})
    # The anchor keeps the catalog whole: no fork method without a contract, no orphan contract.
    fork_names = {n for n in server._methods if n.startswith(fork_gateway.FORK_PREFIX)}
    assert fork_names and fork_names <= set(registry.METHODS)


def test_fork_ping_round_trips_through_dispatch(server):
    resp = server.dispatch({"jsonrpc": "2.0", "id": 7, "method": "fork.ping", "params": {}})
    assert resp == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True, "branch": "next"}}


def test_fork_ping_contract_is_enforced_at_admission(server):
    resp = server.dispatch({"jsonrpc": "2.0", "id": 8, "method": "fork.ping", "params": {"bogus": 1}})
    assert resp["error"]["code"] == 4000
    assert "bogus" in resp["error"]["message"]


def test_reregistration_is_idempotent(server):
    """tests re-import the server under ``patch.dict(sys.modules)``; the anchor runs again."""
    handler_before = server._methods["fork.ping"]
    assert fork_gateway.register_fork_gateway_methods(server) == ["fork.ping"]
    assert server._methods["fork.ping"] is not handler_before
    assert server.dispatch({"jsonrpc": "2.0", "id": 9, "method": "fork.ping"})["result"]["ok"] is True


def test_empty_fork_package_is_a_noop(tmp_path, monkeypatch, isolated_catalog):
    pkg = _make_package(tmp_path, monkeypatch, "fork_empty_pkg", {"helpers.py": "X = 1\n"})
    before = dict(isolated_catalog.METHODS)
    fake = _RecordingServer()
    assert fork_gateway.register_fork_gateway_methods(fake, package=pkg) == []
    assert fake.registered == {}
    assert isolated_catalog.METHODS == before


def test_discovered_module_registers_contract_and_maps_domain_errors(tmp_path, monkeypatch, isolated_catalog):
    pkg = _make_package(tmp_path, monkeypatch, "fork_demo_pkg", {"methods_demo.py": """
        from tui_gateway.contracts.base import Params, Result
        from hermes_fork.gateway import ForkRpcError, fork_method

        class P(Params):
            fail: bool = False

        class R(Result):
            echoed: bool

        @fork_method("fork.demo.echo", params=P, result=R)
        def _(params):
            if params.get("fail"):
                raise ForkRpcError(4242, "demo failure")
            return {"echoed": True}
    """, "not_a_methods_module.py": "raise RuntimeError('must not be imported')\n"})
    fake = _RecordingServer()
    assert fork_gateway.register_fork_gateway_methods(fake, package=pkg) == ["fork.demo.echo"]
    assert "fork.demo.echo" in isolated_catalog.METHODS
    handler = fake.registered["fork.demo.echo"]
    assert handler(1, {}) == {"jsonrpc": "2.0", "id": 1, "result": {"echoed": True}}
    assert handler(2, {"fail": True})["error"] == {"code": 4242, "message": "demo failure"}


@pytest.mark.parametrize("name", ["ping", "fork.", "forkping", "session.fork"])
def test_names_outside_the_fork_namespace_are_rejected(name):
    from tui_gateway.contracts.base import Params, Result

    with pytest.raises(ValueError, match="fork"):
        fork_gateway.fork_method(name, params=Params, result=Result)
