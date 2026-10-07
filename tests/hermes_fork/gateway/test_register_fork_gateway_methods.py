"""The ``gateway-fork-methods`` anchor: fork methods reach the live dispatch table with their own wire
contracts, and an empty fork module list registers nothing."""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

import hermes_fork.gateway as fork_gateway
from hermes_fork.gateway import registry as fork_registry
from tui_gateway.contracts import registry


@pytest.fixture
def server():
    from tui_gateway import server as mod
    return mod


@pytest.fixture
def isolated_catalog(monkeypatch):
    """Private contract table + declaration registry, so synthetic fork modules leave no trace."""
    monkeypatch.setattr(registry, "METHODS", dict(registry.METHODS))
    monkeypatch.setattr(fork_registry, "_DECLARED", dict(fork_registry._DECLARED))
    return registry


class _RecordingServer:
    def __init__(self):
        self.registered: dict = {}

    def register_method(self, name, fn):
        self.registered[name] = fn


def _make_module(tmp_path, monkeypatch, name: str, body: str):
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module(name)


def test_anchor_is_the_registered_entry_point(server):
    """The anchor imports the registry module directly (fork-budget.sh resolves the seam by import)
    and the package re-exports the same function."""
    assert server._register_fork_gateway_methods is fork_registry.register_fork_gateway_methods
    assert fork_gateway.register_fork_gateway_methods is fork_registry.register_fork_gateway_methods


def test_server_import_registers_fork_ping_with_a_contract(server):
    assert "fork.ping" in server._methods
    contract = registry.METHODS["fork.ping"]
    assert contract.result.model_validate({"ok": True, "branch": "next"})
    # The anchor keeps the catalog whole: no fork method without a contract, no orphan contract.
    fork_names = {n for n in server._methods if n.startswith(fork_gateway.FORK_PREFIX)}
    assert fork_names == {n for n in registry.METHODS if n.startswith(fork_gateway.FORK_PREFIX)}


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
    assert fork_gateway.register_fork_gateway_methods(server) == [
        "fork.ping", "fork.session.archive_status",
        "fork.session.compaction_defer", "fork.session.compaction_watermark",
    ]
    assert server._methods["fork.ping"] is not handler_before
    assert server.dispatch({"jsonrpc": "2.0", "id": 9, "method": "fork.ping"})["result"]["ok"] is True


def test_every_methods_module_on_disk_is_listed():
    """A ``methods_*.py`` file left out of ``_fork_method_modules`` would never register."""
    pkg_dir = Path(fork_registry.__file__).parent
    on_disk = {f"hermes_fork.gateway.{p.stem}" for p in pkg_dir.glob("methods_*.py")}
    assert on_disk == {m.__name__ for m in fork_registry._fork_method_modules()}


def test_empty_module_list_is_a_noop(isolated_catalog):
    before = dict(isolated_catalog.METHODS)
    fake = _RecordingServer()
    assert fork_gateway.register_fork_gateway_methods(fake, modules=()) == []
    assert fake.registered == {}
    assert isolated_catalog.METHODS == before


def test_module_without_fork_methods_is_a_noop(tmp_path, monkeypatch, isolated_catalog):
    mod = _make_module(tmp_path, monkeypatch, "fork_helpers_only", "X = 1\n")
    before = dict(isolated_catalog.METHODS)
    fake = _RecordingServer()
    assert fork_gateway.register_fork_gateway_methods(fake, modules=(mod,)) == []
    assert fake.registered == {}
    assert isolated_catalog.METHODS == before


def test_listed_module_registers_contract_and_maps_domain_errors(tmp_path, monkeypatch, isolated_catalog):
    mod = _make_module(tmp_path, monkeypatch, "fork_demo_methods", """
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
    """)
    fake = _RecordingServer()
    assert fork_gateway.register_fork_gateway_methods(fake, modules=(mod,)) == ["fork.demo.echo"]
    assert "fork.demo.echo" in isolated_catalog.METHODS
    handler = fake.registered["fork.demo.echo"]
    assert handler(1, {}) == {"jsonrpc": "2.0", "id": 1, "result": {"echoed": True}}
    assert handler(2, {"fail": True})["error"] == {"code": 4242, "message": "demo failure"}


def test_broken_method_module_is_loud_under_strict_and_logged_otherwise(monkeypatch, caplog):
    def boom():
        raise ImportError("broken fork module")

    monkeypatch.setattr(fork_registry, "_fork_method_modules", boom)
    monkeypatch.setattr(registry, "STRICT", True)
    with pytest.raises(ImportError):
        fork_gateway.register_fork_gateway_methods(_RecordingServer())
    monkeypatch.setattr(registry, "STRICT", False)
    fake = _RecordingServer()
    assert fork_gateway.register_fork_gateway_methods(fake) == []
    assert "no fork.* methods registered" in caplog.text


def test_same_name_from_two_modules_is_rejected(isolated_catalog):
    from tui_gateway.contracts.base import Params, Result

    def a(params):
        return {}

    def b(params):
        return {}

    a.__module__, b.__module__ = "fork_mod_a", "fork_mod_b"
    fork_gateway.fork_method("fork.demo.dup", params=Params, result=Result)(a)
    with pytest.raises(RuntimeError, match="declared by both"):
        fork_gateway.fork_method("fork.demo.dup", params=Params, result=Result)(b)


@pytest.mark.parametrize("name", ["ping", "fork.", "forkping", "session.fork"])
def test_names_outside_the_fork_namespace_are_rejected(name):
    from tui_gateway.contracts.base import Params, Result

    with pytest.raises(ValueError, match="fork"):
        fork_gateway.fork_method(name, params=Params, result=Result)
