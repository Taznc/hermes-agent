"""``fork.ping``: the seam's own liveness probe — proves a client is talking to a backend that
carries the fork gateway surface (upstream builds answer -32601)."""

from __future__ import annotations

from tui_gateway.contracts.base import Params, Result

from hermes_fork.gateway.registry import fork_method


class ForkPingParams(Params):
    pass


class ForkPingResult(Result):
    ok: bool
    branch: str


@fork_method("fork.ping", params=ForkPingParams, result=ForkPingResult,
             doc="Fork gateway liveness probe; answers only on builds carrying hermes_fork.")
def _(params: dict) -> dict:
    return {"ok": True, "branch": "next"}
