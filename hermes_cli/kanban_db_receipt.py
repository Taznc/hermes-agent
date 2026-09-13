"""Deterministic Kanban workspace plans and preflight receipts.

Receipts are dispatcher-produced evidence, not model output.  Their cache key
binds repository provenance, the claim, selected checks, tool/interpreter and
the capability environment.  Full probe output remains in a board artifact;
the worker packet receives only the bounded summary and artifact paths.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


RECEIPT_VERSION = 2
_ARTIFACT_ENV = "HERMES_KANBAN_PREFLIGHT_RECEIPT"
_ARTIFACT_METADATA_KEYS = ("artifact_sha", "commit", "reviewed_commit", "head_sha")


class PreflightError(RuntimeError):
    """A deterministic workspace prerequisite is absent or contradictory."""


@dataclass(frozen=True)
class WorkspacePlan:
    role: str
    base_ref: str
    artifact_ref: Optional[str]
    source: str


@dataclass(frozen=True)
class PreflightReceipt:
    path: Path
    cache_key: str
    reused: bool
    payload: dict[str, Any]


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError(f"git preflight failed: {exc}") from exc


def _git_value(path: Path, *args: str, label: str) -> str:
    result = _git(path, *args)
    value = (result.stdout or "").strip()
    if result.returncode != 0 or not value:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise PreflightError(f"{label}: {detail}")
    return value


def _resolve_commit(repo: Path, ref: str, *, label: str) -> str:
    return _git_value(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", label=label)


def _repo_root(workspace: Path) -> Path:
    common = _git_value(
        workspace,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        label="workspace is not a git repository",
    )
    common_path = Path(common).resolve(strict=False)
    return common_path.parent if common_path.name == ".git" else common_path


def validate_workspace_binding(
    workspace: Path,
    task,
    *,
    base_ref: str,
    artifact_ref: Optional[str],
    role: str,
) -> dict[str, str]:
    """Resolve and validate the exact repository/base/head binding.

    Implementer branches may advance from the declared base, but that base must
    be an ancestor.  Reviewer workspaces are immutable inputs: their HEAD must
    equal the implementation artifact named by the review handoff.
    """
    workspace = workspace.expanduser().resolve(strict=False)
    repo = _repo_root(workspace)
    base_sha = _resolve_commit(
        repo, base_ref, label=f"declared base {base_ref!r} is unavailable"
    )
    head_sha = _resolve_commit(workspace, "HEAD", label="workspace HEAD is unavailable")
    branch = _git_value(
        workspace, "branch", "--show-current", label="workspace branch is unavailable"
    )
    expected_branch = (task.branch_name or "").strip() or f"wt/{task.id}"
    if branch != expected_branch:
        raise PreflightError(
            f"workspace branch mismatch: expected {expected_branch!r}, found {branch!r}"
        )
    if role == "reviewer":
        if not artifact_ref:
            raise PreflightError("review handoff has no artifact SHA")
        artifact_sha = _resolve_commit(
            repo,
            artifact_ref,
            label=f"review artifact {artifact_ref!r} is unavailable",
        )
        if head_sha != artifact_sha:
            raise PreflightError(
                "review artifact mismatch: "
                f"workspace HEAD {head_sha} does not equal handoff artifact {artifact_sha}"
            )
    ancestor = _git(repo, "merge-base", "--is-ancestor", base_sha, head_sha)
    if ancestor.returncode != 0:
        raise PreflightError(
            f"wrong workspace anchor: declared base {base_sha} is not an ancestor of HEAD {head_sha}"
        )
    return {
        "repo": str(repo),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "branch": branch,
    }


def _metadata_artifact(metadata: Any) -> Optional[str]:
    if not isinstance(metadata, dict):
        return None
    for key in _ARTIFACT_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _latest_closed_artifact(conn, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return _metadata_artifact(_kb._json_dict(row["metadata"]))


def _repository_for_task(task, *, board: Optional[str]) -> Optional[Path]:
    """Return the canonical repository authorized for a worktree task.

    Task paths may name an existing checkout, a repository root, or a not-yet
    created ``.worktrees/<task>`` target.  Resolve all three through the shared
    worktree helpers, then canonicalize linked worktrees through their common
    Git directory so repositories compare by identity rather than checkout.
    """
    from hermes_cli import kanban_db_workspace as _kbw

    raw_path = (task.workspace_path or "").strip()
    if not raw_path:
        raw_path = str(_kb.read_board_metadata(board).get("default_workdir") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if path.exists():
        try:
            return _repo_root(path)
        except PreflightError:
            pass
    repo = _kbw._repo_root_for_worktree_target(path)
    if repo is None:
        return None
    try:
        return _repo_root(repo)
    except PreflightError:
        return None


def _parent_artifacts_for_repo(
    conn,
    task,
    *,
    board: Optional[str],
    repo: Path,
) -> list[tuple[str, str, str]]:
    """Return ``(parent_id, declared_ref, sha)`` for this repository only."""
    artifacts: list[tuple[str, str, str]] = []
    for parent_id in _kb.parent_ids(conn, task.id):
        artifact = _latest_closed_artifact(conn, parent_id)
        if not artifact:
            continue
        parent = _kb.get_task(conn, parent_id)
        if parent is None:
            continue
        # A project mismatch is authoritative even if two repositories happen
        # to share objects (forks commonly do).  Otherwise compare canonical
        # Git common directories, not worktree checkout paths.
        if task.project_id and parent.project_id and task.project_id != parent.project_id:
            continue
        parent_repo = _repository_for_task(parent, board=board)
        if parent_repo is None:
            if not (task.project_id and parent.project_id == task.project_id):
                continue
        elif parent_repo != repo:
            continue
        sha = _resolve_commit(
            repo,
            artifact,
            label=f"same-repository parent {parent_id} artifact {artifact!r} is unavailable",
        )
        artifacts.append((parent_id, artifact, sha))
    return artifacts


def _descendant_parent_base(repo: Path, artifacts: list[tuple[str, str, str]]) -> str:
    """Choose the one parent artifact containing every same-repo parent."""
    for _parent_id, artifact, candidate_sha in artifacts:
        if all(
            _git(repo, "merge-base", "--is-ancestor", other_sha, candidate_sha).returncode == 0
            for _other_id, _other_ref, other_sha in artifacts
        ):
            return artifact
    parents = ", ".join(f"{parent_id}={sha}" for parent_id, _ref, sha in artifacts)
    raise PreflightError(
        "same-repository parent artifacts diverge; no single declared base contains all parents: "
        + parents
    )


def workspace_plan(conn, task, *, board: Optional[str] = None) -> WorkspacePlan:
    """Choose the declared base and optional review artifact without guessing.

    A review binds to the immediately preceding implementation artifact.  A
    child implementation extends completed parent artifacts when present;
    otherwise the board's explicit ``land_target`` is the base declaration.
    Multiple parent artifacts are allowed only when one is a descendant of all
    the others; validation against the repository settles that after workspace
    resolution.
    """
    source_state = (
        _kb._retry_status_for_run(conn, task.id, task.current_run_id)
        if task.status == "running"
        else task.status
    )
    role = "reviewer" if source_state == "review" else "implementer"
    board_meta = _kb.read_board_metadata(board)
    land_target = str(board_meta.get("land_target") or "").strip()
    if role == "reviewer":
        artifact = _latest_closed_artifact(conn, task.id)
        if not artifact:
            raise PreflightError(
                "review handoff must declare artifact_sha, commit, reviewed_commit, or head_sha"
            )
        return WorkspacePlan(role, land_target or artifact, artifact, "review_handoff")

    repo = _repository_for_task(task, board=board)
    if repo is None:
        raise PreflightError("worktree task has no authorized git repository")
    parent_artifacts = _parent_artifacts_for_repo(
        conn, task, board=board, repo=repo
    )
    if parent_artifacts:
        base_ref = _descendant_parent_base(repo, parent_artifacts)
        return WorkspacePlan(role, base_ref, None, "parent_artifact")
    if not land_target:
        raise PreflightError(
            "worktree task has no declared base: set the board land_target or provide a completed parent artifact"
        )
    return WorkspacePlan(role, land_target, None, "board_land_target")


def _edit_targets(body: Optional[str]) -> list[str]:
    for line in (body or "").splitlines():
        if line.strip().lower().startswith("edit-targets:"):
            return [
                item.strip()
                for item in line.split(":", 1)[1].split(",")
                if item.strip()
            ]
    return []


def _selected_commands(task, repo: Path) -> dict[str, list[str]]:
    targets = _edit_targets(task.body)
    tests = [target for target in targets if target.startswith("tests/")]
    sources = [target for target in targets if not target.startswith("tests/")]
    runner = repo / "scripts" / "run_tests.sh"
    test_command = [str(runner), *(tests or [])] if runner.is_file() else []
    lint_targets = [
        target for target in sources if target.endswith(".py") or target.endswith("/")
    ]
    lint_command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        *(lint_targets or ["hermes_cli/"]),
    ]
    return {"test": test_command, "lint": lint_command}


def _tool_version() -> str:
    try:
        return importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        return "source-tree"


def _capabilities(
    repo: Optional[Path], workspace: Path, commands: dict[str, list[str]]
) -> dict[str, Any]:
    return {
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "git": shutil.which("git") is not None,
        "test_runner": bool(
            commands["test"] and os.access(commands["test"][0], os.X_OK)
        ),
        "ruff": importlib.util.find_spec("ruff") is not None,
        "repo_writable": os.access(repo, os.W_OK) if repo is not None else None,
        "workspace_writable": os.access(workspace, os.W_OK),
    }


def _environment(capabilities: dict[str, Any]) -> dict[str, Any]:
    selected = {
        "os_name": os.name,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "virtual_env": os.environ.get("VIRTUAL_ENV"),
        "path": os.environ.get("PATH", ""),
        "capabilities": capabilities,
    }
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {"fingerprint": hashlib.sha256(encoded).hexdigest(), **selected}


def _dependency_locks(workspace: Path) -> list[dict[str, Any]]:
    """Stable identities for root dependency locks used by selected checks."""
    locks: list[dict[str, Any]] = []
    for name in (
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "flake.lock",
    ):
        path = workspace / name
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise PreflightError(f"cannot read dependency lock {path}: {exc}") from exc
        locks.append({
            "path": name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        })
    return locks


def _receipt_dir(task_id: str, board: Optional[str]) -> Path:
    path = _kb.worker_logs_dir(board=board).parent / "receipts" / task_id
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreflightError(
            f"cannot create preflight artifact directory {path}: {exc}"
        ) from exc
    return path


def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise PreflightError(f"cannot write preflight artifact {path}: {exc}") from exc


def build_preflight_receipt(
    task,
    workspace: Path,
    *,
    board: Optional[str],
    base_ref: Optional[str],
    artifact_ref: Optional[str],
    role: str,
) -> PreflightReceipt:
    """Build or reuse a deterministic preflight receipt for one claim."""
    workspace = workspace.expanduser().resolve(strict=False)
    if task.workspace_kind == "worktree":
        if not base_ref:
            raise PreflightError("worktree preflight has no declared base")
        binding: dict[str, Any] = validate_workspace_binding(
            workspace,
            task,
            base_ref=base_ref,
            artifact_ref=artifact_ref,
            role=role,
        )
        binding["kind"] = "worktree"
        repo: Optional[Path] = Path(binding["repo"])
        commands = _selected_commands(task, workspace)
        lock_identity = _dependency_locks(workspace)
    else:
        binding = {
            "kind": task.workspace_kind or "scratch",
            "repo": None,
            "base_sha": None,
            "head_sha": None,
            "branch": task.branch_name,
        }
        repo = None
        commands = {"test": [], "lint": []}
        lock_identity = []
    capabilities = _capabilities(repo, workspace, commands)
    if task.workspace_kind == "worktree" and not capabilities["git"]:
        raise PreflightError("required capability unavailable: git")
    if commands["test"] and not capabilities["test_runner"]:
        raise PreflightError(
            f"required test runner is not executable: {commands['test'][0]}"
        )
    if commands["lint"] and not capabilities["ruff"]:
        raise PreflightError("required capability unavailable: ruff")
    if not capabilities["workspace_writable"]:
        raise PreflightError(f"workspace is not writable: {workspace}")
    if repo is not None and not capabilities["repo_writable"]:
        raise PreflightError(f"repository metadata is not writable: {repo}")
    environment = _environment(capabilities)
    key_input = {
        "receipt_version": RECEIPT_VERSION,
        "task_id": task.id,
        "role": role,
        "workspace": {**binding, "path": str(workspace)},
        "artifact_ref": artifact_ref,
        "claim_identity": {
            "lock": task.claim_lock,
            "run_id": task.current_run_id,
        },
        "lock_identity": lock_identity,
        "commands": commands,
        "tool_version": _tool_version(),
        "interpreter": {
            "executable": sys.executable,
            "version": platform.python_version(),
        },
        "environment_fingerprint": environment["fingerprint"],
    }
    cache_key = hashlib.sha256(
        json.dumps(key_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    receipt_dir = _receipt_dir(task.id, board)
    receipt_path = receipt_dir / f"{cache_key}.json"
    log_path = receipt_dir / f"{cache_key}.log"
    if receipt_path.is_file() and log_path.is_file():
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("cache_key") == cache_key
            and payload.get("task_id") == task.id
        ):
            return PreflightReceipt(receipt_path, cache_key, True, payload)

    checks = [
        ("repo", binding["repo"]),
        ("base_sha", binding["base_sha"]),
        ("head_sha", binding["head_sha"]),
        ("branch", binding["branch"]),
        ("test_runner", str(capabilities["test_runner"])),
        ("ruff", str(capabilities["ruff"])),
    ]
    _write_atomic(
        log_path, "".join(f"{name}: exit=0 value={value}\n" for name, value in checks)
    )
    payload = {
        **key_input,
        "cache_key": cache_key,
        "environment": environment,
        "summary": {
            "exit_code": 0,
            "checks_run": len(checks),
            "checks_failed": 0,
            "excerpt": "; ".join(f"{name}={value}" for name, value in checks[:4]),
        },
        "artifacts": {"receipt": str(receipt_path), "full_log": str(log_path)},
    }
    _write_atomic(receipt_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return PreflightReceipt(receipt_path, cache_key, False, payload)


def packet_preflight_receipt(task_id: str) -> Optional[dict[str, Any]]:
    """Return the bounded model-facing projection of the pinned receipt."""
    raw = os.environ.get(_ARTIFACT_ENV, "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if payload.get("task_id") != task_id:
        return None
    return {
        "receipt_version": payload.get("receipt_version"),
        "cache_key": payload.get("cache_key"),
        "workspace": payload.get("workspace"),
        "claim_identity": payload.get("claim_identity"),
        "lock_identity": payload.get("lock_identity"),
        "interpreter": payload.get("interpreter"),
        "commands": payload.get("commands"),
        "capabilities": payload.get("environment", {}).get("capabilities"),
        "summary": payload.get("summary"),
        "artifacts": payload.get("artifacts"),
    }


from hermes_cli import kanban_db as _kb  # noqa: E402
