"""Fork gateway JSON-RPC surface, registered from the single
``# >>> FORK ANCHOR: gateway-fork-methods <<<`` line at the end of
``tui_gateway/server.py`` (after every upstream split module, so fork handlers
may reference any server global).

Owns everything the fork adds to the gateway method table:

- chunked desktop file-attach staging (``hermes_fork.attachments.staging``)
- the sanitized account-limits RPC (``hermes_fork.account_limits.gateway_method``)
- the backend-side Projects repo scan (``projects.scan_repos`` below)
- the fork methods' ``_LONG_HANDLERS`` entries (worker-pool routing)
"""

from __future__ import annotations

from tui_gateway.method_ctx import HandlerRegistry, bind_module

from hermes_fork.account_limits import gateway_method as _account_limits_method
from hermes_fork.attachments import staging as _attachment_staging

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


@method("projects.scan_repos")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Walk THIS backend's own filesystem for git repos (``hermes_fork.repo_scan``), persist, and
    return the merged repo list — the server-side sibling of ``projects.record_repos`` for remote
    gateways whose Projects sidebar the desktop's local crawl can never populate. The policy is
    resolved server-side (the roots are this machine's); an optional ``discovery_policy`` param is
    only a staleness check — a mismatch refuses the scan rather than persisting a stale result."""
    try:
        from hermes_cli import projects_db as pdb
        from hermes_fork.repo_scan import scan_repos_on_disk
        policy = _repo_discovery_policy()
        policy_key = _repo_discovery_policy_key(policy)
        incoming = params.get("discovery_policy")
        # No policy sent means "use whatever this backend is configured for" — the honest default for
        # a remote scan, since the client cannot know the remote machine's roots.
        policy_matches = (not isinstance(incoming, dict)
                          or _repo_discovery_policy_key(_repo_discovery_policy(incoming)) == policy_key)
        accepted = bool(policy["enabled"] and policy_matches)
        pairs: list[tuple[str, str | None]] = []
        if accepted:  # a disabled policy must not touch the filesystem at all
            pairs = [(root, label) for root, label in scan_repos_on_disk(policy) if not _is_repo_junk(root)]
        with pdb.connect_closing() as conn:
            _reconcile_repo_discovery(pdb, conn, policy, policy_key)
            if accepted:
                pdb.record_discovered_repos(conn, pairs, replace=True, policy_key=policy_key)
            elif not policy["enabled"]:
                pdb.clear_discovered_repos(conn, policy_key=policy_key)
        with _profile_db(params) as db:
            repos = [] if db is None else _discover_repos_payload(db, include_cached=policy["enabled"])
            return _ok(rid, {"repos": repos, "accepted": accepted, "discovery_policy": policy})
    except Exception as e:
        return _err(rid, 5061, str(e))


# projects.scan_repos walks this backend's own disk (bounded, but thousands of stat calls on a
# cold FS); account_limits.get makes synchronous provider HTTP requests — never on the WS reader.
_FORK_LONG_HANDLERS = frozenset({"projects.scan_repos", "account_limits.get"})


def register_fork_gateway_methods(server) -> None:
    """Install every fork gateway module onto ``server`` and route the slow fork
    methods onto the RPC worker pool."""
    _attachment_staging.register(server)
    _account_limits_method.register(server)
    bind_module(globals(), server, skip=("_", "register_fork_gateway_methods"))
    server._LONG_HANDLERS = server._LONG_HANDLERS | _FORK_LONG_HANDLERS
