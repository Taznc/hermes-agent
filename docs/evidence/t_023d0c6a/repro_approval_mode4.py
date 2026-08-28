"""Repro (Phase 2.2) step 4 — THE DECISIVE ONE.

Steps 1-3 showed the single-profile resolver chain is correct end to end:
config.yaml(smart) -> _get_approval_mode -> _load_approval_mode -> config.get
all answer 'smart'. So the bug is not the resolver.

This script tests the case the Desktop actually runs in: MORE THAN ONE PROFILE.
The Desktop statusbar caches approval mode PER PROFILE
(store/approval-mode.ts keys $approvalModes by profile) and asks the backend
for it with:

    requestGateway('config.get', { key: 'approvals.mode' })    <-- no profile!

Meanwhile every sibling profile-aware handler in tui_gateway/methods_config.py
is wrapped in @_profile_scoped, which binds params['profile']'s HERMES_HOME for
the duration of the call. config.get is NOT wrapped.

Layout mirrors this dev VM exactly:
  launch profile  : no approvals block  -> resolver default 'manual'
  named profile X : approvals.mode smart
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

root = Path(tempfile.mkdtemp(prefix="hermes_repro_home4_"))
launch_home = root / "launch"
launch_home.mkdir(parents=True)
# Launch profile: NO approvals block at all (exactly like this VM's default
# profile) -> the canonical resolver falls back to 'manual'.
(launch_home / "config.yaml").write_text("model: test/model\napprovals:\n  mode: manual\n", encoding="utf-8")

# Named profile with the user's CONFIGURED smart mode.
named_home = launch_home / "profiles" / "work"
named_home.mkdir(parents=True)
(named_home / "config.yaml").write_text(
    "model: test/model\napprovals:\n  mode: smart\n", encoding="utf-8"
)

os.environ["HERMES_HOME"] = str(launch_home)

import importlib

server = importlib.import_module("tui_gateway.server")

print("launch profile home :", launch_home, "(no approvals block)")
print("named  profile home :", named_home, "(approvals.mode: smart)")
print()

print("=== what each profile's config ACTUALLY says ===")
from tools.approval import _get_approval_mode
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

print("launch  _get_approval_mode() ->", _get_approval_mode())
tok = set_hermes_home_override(named_home)
try:
    print("work    _get_approval_mode() ->", _get_approval_mode(), " <-- configured smart")
finally:
    reset_hermes_home_override(tok)

print()
print("=== what the Desktop statusbar receives ===")
print("The renderer sends NO profile param (store/approval-mode.ts:56):")
resp = server.handle_request(
    {"jsonrpc": "2.0", "id": "1", "method": "config.get", "params": {"key": "approvals.mode"}}
)
print("  config.get{key:approvals.mode}                 ->", resp.get("result"))

print()
print("Even if it DID send one, config.get is not @_profile_scoped:")
resp2 = server.handle_request(
    {
        "jsonrpc": "2.0",
        "id": "2",
        "method": "config.get",
        "params": {"key": "approvals.mode", "profile": "work"},
    }
)
print("  config.get{key:approvals.mode, profile:work}   ->", resp2.get("result"))

print()
print("=== contrast: a sibling handler that IS @_profile_scoped honors it ===")
r3 = server.handle_request(
    {"jsonrpc": "2.0", "id": "3", "method": "projects.tree", "params": {"profile": "work"}}
)
print("  projects.tree{profile:work} routed without error:", "result" in r3)

print()
print("=== and the session.info the deferred agent build emits ===")


class _FakeAgent:
    model = "test/model"
    provider = "test"
    session_id = "s1"
    reasoning_config = None
    service_tier = None


# A session BOUND to the 'work' profile: profile_home is set, so the payload
# reports profile_name='work' -- but approval_mode is resolved from ambient
# HERMES_HOME (the launch profile), not from that session's profile.
session = {
    "cwd": str(named_home),
    "session_key": "s1",
    "running": False,
    "profile_home": str(named_home),
}
info = server._session_info(_FakeAgent(), session)
print("  info['profile_name']  ->", info.get("profile_name"))
print("  info['approval_mode'] ->", info.get("approval_mode"), " <-- 'work' config says smart")

shutil.rmtree(root, ignore_errors=True)
