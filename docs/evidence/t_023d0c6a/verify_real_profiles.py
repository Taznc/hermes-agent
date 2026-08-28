"""Verify the fix against the REAL profiles on this VM, not a mock.

The card requires demonstrating this against a real configured `smart`
profile, because it is a config-propagation bug and mocks are what hide it.

This VM's actual layout is the exact shape that triggered the bug:
  ~/.hermes/config.yaml                     -> no approvals block
  ~/.hermes/profiles/<name>/config.yaml     -> approvals.mode: smart

READ-ONLY: this script never writes to any real config. It exercises the
read path (config.get + _session_info) against the real profile homes.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

REAL_HOME = Path("/home/hermes/.hermes")
os.environ["HERMES_HOME"] = str(REAL_HOME)

import importlib

server = importlib.import_module("tui_gateway.server")

profiles_dir = REAL_HOME / "profiles"
names = sorted(p.name for p in profiles_dir.iterdir() if (p / "config.yaml").exists())

print("launch home :", REAL_HOME)
print("  approvals block in launch config.yaml:", end=" ")
import yaml

launch_cfg = yaml.safe_load((REAL_HOME / "config.yaml").read_text()) or {}
print(launch_cfg.get("approvals"))
print("  launch config.get(approvals.mode) ->", end=" ")
r = server.handle_request(
    {"jsonrpc": "2.0", "id": "0", "method": "config.get", "params": {"key": "approvals.mode"}}
)
print(r["result"]["value"])
print()

ok = True
for name in names:
    home = profiles_dir / name
    on_disk = (yaml.safe_load((home / "config.yaml").read_text()) or {}).get("approvals", {})
    configured = on_disk.get("mode")

    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": name,
            "method": "config.get",
            "params": {"key": "approvals.mode", "profile": name},
        }
    )
    served = resp["result"]["value"]

    class _Agent:
        model = "test/model"
        provider = "test"
        session_id = f"sid-{name}"
        reasoning_config = None
        service_tier = None

    info = server._session_info(
        _Agent(),
        {"cwd": str(home), "session_key": f"sid-{name}", "profile_home": str(home)},
    )

    match = served == configured and info["approval_mode"] == configured
    ok = ok and match
    print(
        f"{name:14s} config.yaml={configured!r:9s} "
        f"config.get={served!r:9s} "
        f"session.info={info['approval_mode']!r:9s} "
        f"profile_name={info['profile_name']!r:14s} "
        f"{'OK' if match else 'MISMATCH'}"
    )

print()
print("ALL REAL PROFILES CONSISTENT:", ok)
