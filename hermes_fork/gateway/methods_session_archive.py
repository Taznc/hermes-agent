"""Read-only archive admission discovery in the fork RPC namespace."""

from hermes_fork.gateway.registry import ForkRpcError, fork_method
from hermes_fork.session_archive import archive_blockers
from hermes_fork.session_archive.transports import rpc_target
from tui_gateway.contracts.base import Params, Result


class ArchiveStatusParams(Params):
    session_id: str
    profile: str | None = None


class ArchiveStatusResult(Result):
    session_key: str
    archivable: bool
    blockers: list[str]


@fork_method(
    "fork.session.archive_status",
    params=ArchiveStatusParams,
    result=ArchiveStatusResult,
    doc="Read-only owned-work archive blockers. Advisory only; archive rechecks atomically.",
)
def archive_status(params):
    import tui_gateway.server as server

    target, session, context = rpc_target(params, vars(server), writer=False)
    with context as db:
        if db is None:
            raise ForkRpcError(5007, "Session storage unavailable")
        key = (
            session["session_key"]
            if session is not None
            else db.resolve_session_id(target)
        )
        if not key:
            raise ForkRpcError(4001, "Session not found")
        blockers = archive_blockers(db, key, (target,))
        return {"session_key": key, "archivable": not blockers, "blockers": blockers}
