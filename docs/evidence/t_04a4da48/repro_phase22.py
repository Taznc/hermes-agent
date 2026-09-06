"""Phase 2.2 reproduction — is a configured `smart` approval mode honored at
Desktop session start, on CURRENT dev?

Runs entirely inside an isolated development HERMES_HOME under ~/.hermes-dev
(never the managed ~/.hermes runtime). Records, for each scenario:

  1. the RESOLVED CONFIGURATION   (what config.yaml / DEFAULT_CONFIG say)
  2. the INITIAL SESSION STATE    (tui_gateway `_session_info` -> approval_mode)
  3. the VISIBLE DESKTOP MODE     (what config.get / session.info hand the
                                   renderer's approval-mode statusbar)

Read-only with respect to real config: everything is written under
~/.hermes-dev/<scenario>/ and nothing under ~/.hermes is touched.
"""
import json
import os
import sys
from pathlib import Path

DEV_ROOT = Path.home() / ".hermes-dev" / "t_04a4da48"


def scenario(name: str, launch_yaml: str, named_yaml: str | None = None) -> dict:
    """Build a throwaway profile layout and probe every layer in a subprocess."""
    home = DEV_ROOT / name
    launch = home / "launch"
    launch.mkdir(parents=True, exist_ok=True)
    (launch / "config.yaml").write_text(launch_yaml, encoding="utf-8")
    if named_yaml is not None:
        named = launch / "profiles" / "work"
        named.mkdir(parents=True, exist_ok=True)
        (named / "config.yaml").write_text(named_yaml, encoding="utf-8")
    return {"home": str(launch), "named": named_yaml is not None}


PROBE = r'''
import json, os, sys
from pathlib import Path
REPO = os.environ["REPO"]
sys.path.insert(0, REPO)
launch = Path(os.environ["HERMES_HOME"])
named = launch / "profiles" / "work"
has_named = os.environ.get("HAS_NAMED") == "1"

out = {}

# --- layer 1: resolved configuration -----------------------------------
from hermes_cli.config import load_config_readonly
import yaml
raw = yaml.safe_load((launch / "config.yaml").read_text()) or {}
out["raw_launch_yaml_approvals"] = raw.get("approvals")
out["merged_launch_approvals_mode"] = (load_config_readonly().get("approvals") or {}).get("mode")

from tools.approval_context import _get_approval_mode
out["resolver_launch"] = _get_approval_mode()

if has_named:
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    rawn = yaml.safe_load((named / "config.yaml").read_text()) or {}
    out["raw_named_yaml_approvals"] = rawn.get("approvals")
    tok = set_hermes_home_override(named)
    try:
        out["resolver_named"] = _get_approval_mode()
    finally:
        reset_hermes_home_override(tok)

# --- layer 2 + 3: gateway payloads the Desktop reads --------------------
import importlib
server = importlib.import_module("tui_gateway.server")

def rpc(method, params):
    return server.handle_request({"jsonrpc": "2.0", "id": "x", "method": method,
                                  "params": params}).get("result")

out["config_get_unscoped"] = rpc("config.get", {"key": "approvals.mode"})
if has_named:
    out["config_get_named"] = rpc("config.get", {"key": "approvals.mode", "profile": "work"})

class _A:
    model = "test/model"; provider = "test"; session_id = "s1"
    reasoning_config = None; service_tier = None

sess_launch = {"cwd": str(launch), "session_key": "s1", "running": False}
i = server._session_info(_A(), sess_launch)
out["session_info_launch"] = {"profile_name": i.get("profile_name"),
                              "approval_mode": i.get("approval_mode"), "yolo": i.get("yolo")}
if has_named:
    sess_named = {"cwd": str(named), "session_key": "s2", "running": False,
                  "profile_home": str(named)}
    j = server._session_info(_A(), sess_named)
    out["session_info_named"] = {"profile_name": j.get("profile_name"),
                                 "approval_mode": j.get("approval_mode"), "yolo": j.get("yolo")}

print("@@JSON@@" + json.dumps(out))
'''


def run(spec: dict) -> dict:
    import subprocess
    env = dict(os.environ)
    env["HERMES_HOME"] = spec["home"]
    env["REPO"] = str(Path(__file__).resolve().parents[3])
    env["HAS_NAMED"] = "1" if spec["named"] else "0"
    env.pop("HERMES_PROFILE", None)
    p = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True)
    # Importing the agent stack can re-point stdout, so scan BOTH streams.
    for line in (p.stdout + "\n" + p.stderr).splitlines():
        if line.startswith("@@JSON@@"):
            return json.loads(line[len("@@JSON@@"):])
    raise SystemExit(f"probe failed:\n{p.stdout}\n{p.stderr}")


CASES = {
    # The layout the dev VM actually has: launch profile with NO approvals
    # block, named worker profiles each configured `smart`.
    "A_no_block_named_smart": ("model: test/model\n",
                               "model: test/model\napprovals:\n  mode: smart\n"),
    # Single profile, explicitly configured smart. The card's literal symptom.
    "B_single_smart": ("model: test/model\napprovals:\n  mode: smart\n", None),
    # Single profile, nothing configured at all: what IS the intended default?
    "C_single_empty": ("model: test/model\n", None),
    # Launch manual + named smart — the t_023d0c6a two-profile shape.
    "D_manual_vs_smart": ("model: test/model\napprovals:\n  mode: manual\n",
                          "model: test/model\napprovals:\n  mode: smart\n"),
}

if __name__ == "__main__":
    DEV_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"isolated development home: {DEV_ROOT}\n")
    for name, (launch_yaml, named_yaml) in CASES.items():
        spec = scenario(name, launch_yaml, named_yaml)
        print(f"=== {name} ===")
        for k, v in run(spec).items():
            print(f"  {k:32s} {v}")
        print()
