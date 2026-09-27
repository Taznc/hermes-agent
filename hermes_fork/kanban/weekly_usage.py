"""Pre-claim weekly Anthropic OAuth admission for unattended Kanban workers only.

Disabled unless ``kanban.anthropic_weekly_guard.enabled: true`` is explicitly
set. The enabled default threshold is 70% ``Current week`` (and the matching
Opus/Sonnet week, when reported). Configure
``kanban.anthropic_weekly_guard.threshold_percent`` to change it. No change to
personal chat, cron, other-provider Kanban workers or Hindsight's own processes.

Operator CLI, scoped to an assignee's account::

    hermes kanban weekly-usage --assignee coder status
    hermes kanban weekly-usage --assignee coder override --threshold 90 --until-reset --reason "release"
    hermes kanban weekly-usage --assignee coder override --bypass --hours 2 --reason "urgent"
    hermes kanban weekly-usage --assignee coder clear

The per-profile cache is bounded (5 minutes); an unavailable/old sample never
permits new Claude workers. Failures are retried at most once per minute, and
both cached state and the temporary override survive gateway restarts. The
operator's reason and expiry are appended to a per-profile JSONL audit file.
"""
from __future__ import annotations

import getpass
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CACHE_TTL = timedelta(minutes=5)
DEFAULT_THRESHOLD = 70
MAX_OVERRIDE = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _date(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def _profile_home(profile="default") -> Path:
    from hermes_cli.profiles import get_profile_dir, profile_exists
    if not profile_exists(profile):
        raise ValueError(f"unknown profile: {profile}")
    return get_profile_dir(profile)


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".weekly-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _identity(conn, row, assignee):
    from hermes_cli.profiles import get_profile_dir
    from hermes_cli.config import load_config_readonly
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope
    task = conn.execute("SELECT model_override, provider_override FROM tasks WHERE id = ?", (row["id"],)).fetchone()
    # Match the spawned worker's resolved configuration, not just the raw YAML:
    # defaults and managed/profile overlays can change the provider or model.
    with _worker_profile_scope(str(get_profile_dir(assignee))):
        cfg = load_config_readonly()
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        model_cfg = {"default": model_cfg}
    if not isinstance(model_cfg, dict):
        raise ValueError("invalid model configuration")
    model_override = task["model_override"] if task else None
    model = str(model_override or model_cfg.get("default") or model_cfg.get("model") or "").lower()
    provider_override = task["provider_override"] if task and model_override else None
    provider = str(provider_override or model_cfg.get("provider") or "auto").lower()
    # Explicit non-Anthropic providers do not spend the OAuth account even if
    # their catalog model name includes 'anthropic/claude'.
    if provider not in ("auto", "anthropic"):
        return None
    return model if (provider == "anthropic" or model.startswith("claude-") or model.startswith("anthropic/claude-")) else None


def _threshold():
    from hermes_cli.config import load_config_readonly
    cfg = (load_config_readonly().get("kanban") or {}).get("anthropic_weekly_guard", {})
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    # Explicit opt-in: malformed thresholds fail closed rather than silently
    # turning an enabled guard into a disabled one.
    value = cfg.get("threshold_percent", DEFAULT_THRESHOLD)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 100:
        raise ValueError("kanban.anthropic_weekly_guard.threshold_percent must be in (0, 100]")
    return float(value)


def _sample(home, now, *, fresh=False):
    """Read an at-most-5-minute sample, or fetch anew. Never use stale data for admission."""
    from agent.account_usage import fetch_account_usage
    path = home / "kanban-weekly-usage.json"
    cached = _json(path)
    checked = _date(cached.get("checked_at"))
    if not fresh and checked and timedelta(0) <= now - checked < CACHE_TTL:
        return cached if cached.get("weekly_percent") is not None else None
    last_error = _date(cached.get("last_error_at"))
    if not fresh and last_error and timedelta(0) <= now - last_error < timedelta(minutes=1):
        return None
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope
    with _worker_profile_scope(str(home)):
        snapshot = fetch_account_usage("anthropic")
    if snapshot and snapshot.available and snapshot.source == "oauth_usage_api":
        weekly = next((w for w in snapshot.windows if w.label == "Current week"), None)
        if weekly and weekly.used_percent is not None and math.isfinite(weekly.used_percent) and 0 <= weekly.used_percent <= 100:
            result = {"checked_at": now.isoformat(), "weekly_percent": weekly.used_percent,
                      "reset_at": weekly.reset_at.isoformat() if weekly.reset_at else None}
            for label, key in (("Opus week", "opus_percent"), ("Sonnet week", "sonnet_percent")):
                window = next((w for w in snapshot.windows if w.label == label), None)
                if window and window.used_percent is not None and math.isfinite(window.used_percent) and 0 <= window.used_percent <= 100:
                    result[key] = window.used_percent
            _atomic_json(path, result)
            return result
    # Keep the last sample for diagnostics, but never admit on its stale value.
    cached["last_error_at"] = now.isoformat()
    _atomic_json(path, cached)
    return None


def _override(home, now):
    data = _json(home / "kanban-weekly-override.json")
    expiry = _date(data.get("expires_at"))
    created = _date(data.get("created_at"))
    if not expiry or not created or not created <= now < expiry or expiry - created > MAX_OVERRIDE:
        return None
    if not str(data.get("reason") or "").strip():
        return None
    if data.get("bypass") is True:
        return data
    threshold = data.get("threshold_percent")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 < threshold <= 100:
        return None
    return data


def admit(conn, row, assignee):
    """Return a reason to defer without claiming, or None to proceed."""
    try:
        threshold = _threshold()
        if threshold is None:
            return None  # A home that has not opted in keeps upstream behavior.
    except Exception as exc:
        return f"weekly Anthropic guard: invalid guard configuration ({type(exc).__name__}: {exc})"
    try:
        model = _identity(conn, row, assignee)
        if model is None:
            return None
    except Exception as exc:
        # Unknown model/profile scope may be Anthropic; never silently admit.
        return f"weekly Anthropic guard: cannot resolve worker model: {exc}"
    try:
        now = _now()
        home = _profile_home(assignee)
        override = _override(home, now)
        if override and override.get("bypass") is True:
            return None
        sample = _sample(home, now)
        if sample is None:
            return "weekly Anthropic guard: OAuth usage unavailable; retry at next dispatch tick (or use a timed override)"
        effective = float(override["threshold_percent"]) if override else threshold
        relevant = [sample["weekly_percent"]]
        key = "opus_percent" if "opus" in model else "sonnet_percent" if "sonnet" in model else None
        if key and key in sample:
            relevant.append(sample[key])
        used = max(relevant)
        if used >= effective:
            return f"weekly Anthropic guard: {used:g}% >= {effective:g}% (reset {sample.get('reset_at') or 'unknown'})"
        return None
    except Exception as exc:
        return f"weekly Anthropic guard: admission check failed ({type(exc).__name__}: {exc})"


def add_parser(sub):
    p = sub.add_parser("weekly-usage", help="Inspect/temporarily override Anthropic weekly Kanban admission")
    actions = p.add_subparsers(dest="weekly_usage_action")
    p.add_argument("--assignee", default="default", help="Kanban worker assignee whose Anthropic account to inspect or override")
    actions.add_parser("status", help="Show threshold, last sample, and active override")
    override = actions.add_parser("override", help="Temporarily raise threshold or bypass with audit")
    choice = override.add_mutually_exclusive_group(required=True)
    choice.add_argument("--threshold", type=float, help="Temporary threshold percent (1-100)")
    choice.add_argument("--bypass", action="store_true", help="Explicitly bypass weekly guard")
    expiry = override.add_mutually_exclusive_group(required=True)
    expiry.add_argument("--hours", type=float, help="Positive duration (maximum 168 hours)")
    expiry.add_argument("--until-reset", action="store_true", help="Expire at the fresh OAuth weekly reset")
    override.add_argument("--reason", required=True, help="Operator reason recorded in audit log")
    actions.add_parser("clear", help="End the active override and audit the change")


def _audit(home, record):
    path = home / "kanban-weekly-audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write(json.dumps(record, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


def command(args):
    """Operator-only CLI (not an agent tool, independent of any board)."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        print("weekly-usage: workers cannot change operator overrides")
        return 2
    try:
        home = _profile_home(getattr(args, "assignee", "default"))
    except ValueError as exc:
        print(f"weekly-usage: {exc}")
        return 2
    now = _now()
    action = getattr(args, "weekly_usage_action", None)
    if action == "status":
        print(json.dumps({"threshold_percent": _threshold(), "sample": _json(home / "kanban-weekly-usage.json"),
                          "active_override": _override(home, now)}, sort_keys=True))
        return 0
    if action == "clear":
        try:
            _audit(home, {"at": now.isoformat(), "actor": getpass.getuser(), "profile": getattr(args, "assignee", "default"), "action": "clear"})
            (home / "kanban-weekly-override.json").unlink(missing_ok=True)
        except OSError as exc:
            print(f"weekly-usage: {exc}")
            return 2
        print("Weekly usage override cleared")
        return 0
    if action != "override":
        print("Usage: hermes kanban weekly-usage {status,override,clear}")
        return 2
    reason = str(getattr(args, "reason", "") or "").strip()
    threshold = getattr(args, "threshold", None)
    bypass = getattr(args, "bypass", False)
    hours = getattr(args, "hours", None)
    if not reason or len(reason) > 500 or (not bypass and (threshold is None or not math.isfinite(threshold) or not DEFAULT_THRESHOLD < threshold <= 100)):
        print("weekly-usage: reason required; raised threshold must be > 70 and <= 100")
        return 2
    try:
        reset = None
        if getattr(args, "until_reset", False):
            sample = _sample(home, now, fresh=True)
            reset = _date(sample.get("reset_at")) if sample else None
            if not reset or not now < reset <= now + MAX_OVERRIDE:
                raise ValueError("fresh OAuth weekly reset unavailable or more than 7 days away")
            expiry = reset
        else:
            if hours is None or not math.isfinite(hours) or not 0 < hours <= 168:
                raise ValueError("--hours must be positive and <= 168")
            expiry = now + timedelta(hours=hours)
        record = {"action": "override", "actor": getpass.getuser(), "profile": getattr(args, "assignee", "default"),
                  "created_at": now.isoformat(), "expires_at": expiry.isoformat(),
                  "threshold_percent": threshold, "bypass": bool(bypass), "reason": reason,
                  "reset_at": reset.isoformat() if reset else None}
        # Audit first; if it fails, do not enable an unlogged override.
        _audit(home, record)
        _atomic_json(home / "kanban-weekly-override.json", record)
    except (OSError, ValueError) as exc:
        print(f"weekly-usage: {exc}")
        return 2
    print(f"Weekly usage override expires {expiry.isoformat()} (reason: {reason})")
    return 0
