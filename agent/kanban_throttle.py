"""Usage-aware Kanban admission control (``kanban.usage_throttle``).

The Kanban dispatcher consults this module once per tick, before it decides
how many workers a tick may start.  The driving signal is the REAL,
authenticated provider quota read by :func:`agent.account_usage.fetch_account_usage`
— never local token/cost accounting, which measures an unrelated thing.

Three ideas carry the whole design:

* **Pressure is one global number.** One subscription is one shared resource,
  so the worst *active* window across every configured provider drives one
  state shared by every board.  The state lives in its own SQLite row under
  the shared Kanban home, so separate board dispatchers (and a dispatcher
  restarted mid-pressure) all observe the same thing.

* **Automatic state is never operator intent.** The throttle writes no config
  and no dispatch-pause sentinel.  It NARROWS the effective concurrency cap in
  memory and rewrites the route of the task it is about to spawn, so the
  operator's own ``kanban.max_in_progress`` and the card's stored route are
  still there, untouched, when pressure clears.  Recovery is therefore not a
  restore operation that could clobber something — it is the automatic value
  going away.

* **No signal means no guess.** A missing, failed or stale reading changes
  nothing in either direction (escalating would invent pressure; recovering
  would invent headroom) and emits a visible degraded record naming what to
  do about it.

Lever ladder, applied by rising pressure: reduce concurrency -> downgrade the
model within the configured ladder -> drain (finish in-flight work, claim
nothing new).  Cross-provider failover is a fourth, separate lever that is
OFF by default and cannot act without fresh capacity signals for BOTH the
source and the destination account.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# --- States -------------------------------------------------------------

STATE_NORMAL = "normal"
STATE_REDUCE = "reduce_concurrency"
STATE_DOWNGRADE = "downgrade_model"
STATE_DRAIN = "drain"

#: Escalation order.  A higher rank subsumes every lever below it: draining
#: implies the concurrency clamp and the downgrade are also in force, so a
#: state is a single point on this ladder rather than a set of flags.
_STATE_RANK = {STATE_NORMAL: 0, STATE_REDUCE: 1, STATE_DOWNGRADE: 2, STATE_DRAIN: 3}

#: Degraded reason codes.  Stable strings: they are fingerprinted for
#: deduplication and rendered to operators.
DEGRADED_NO_SIGNAL = "no_fresh_capacity_signal"
DEGRADED_FAILOVER_DESTINATION = "failover_destination_capacity_unknown"
#: Every configured source provider is one this build has no capacity contract
#: for.  Separated from ``DEGRADED_NO_SIGNAL`` because the operator action is
#: completely different: no amount of waiting or re-authenticating will produce
#: a reading, the configuration itself names a provider that cannot be read.
DEGRADED_UNSUPPORTED_SOURCE = "configured_providers_unsupported"

_RECOVERY_HINT = (
    "check `hermes /usage` for the configured provider account, or set "
    "kanban.usage_throttle.enabled=false to disable admission throttling"
)

_UNSUPPORTED_HINT = (
    "set kanban.usage_throttle.source_providers to an account with an "
    "authenticated quota signal, or set kanban.usage_throttle.enabled=false "
    "to disable admission throttling"
)


# --- Configuration ------------------------------------------------------


@dataclass(frozen=True)
class LeverConfig:
    """One pressure lever: a toggle plus the percentage that arms it."""

    enabled: bool
    threshold_pct: float


@dataclass(frozen=True)
class ConcurrencyLever(LeverConfig):
    #: Absolute cap while this lever is armed.  Applied as
    #: ``min(operator_value, max_in_progress)`` so it can only ever tighten.
    max_in_progress: int = 2


@dataclass(frozen=True)
class DowngradeLever(LeverConfig):
    #: One global ordered ladder, cheapest last.  A task whose model appears in
    #: the ladder moves one rung down; a model that is absent (or already on the
    #: last rung) is left alone rather than guessed at.
    ladder: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailoverLever(LeverConfig):
    #: Explicit allowlist.  Empty (the default) makes the lever unusable even
    #: when ``enabled`` — a profile must be named, never inferred from having a
    #: route configured for some other provider.
    eligible_profiles: tuple[str, ...] = ()
    destination_provider: Optional[str] = None
    destination_model: Optional[str] = None
    #: The destination must itself prove headroom.  Another provider is not an
    #: unlimited overflow valve.
    destination_max_pressure_pct: float = 50.0


@dataclass(frozen=True)
class ThrottleConfig:
    enabled: bool
    source_providers: tuple[str, ...]
    signal_max_age_seconds: int
    poll_interval_seconds: int
    fetch_timeout_seconds: float
    resume_threshold_pct: float
    log_event: bool
    reduce: ConcurrencyLever
    downgrade: DowngradeLever
    drain: LeverConfig
    failover: FailoverLever


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _pct(value: Any, default: float) -> float:
    """Percentages are clamped to [0, 100]; a nonsense value falls back."""
    parsed = _num(value, default)
    return min(100.0, max(0.0, parsed))


def _int(value: Any, default: int, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= minimum else default


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _section(cfg: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = cfg.get(key)
    return value if isinstance(value, Mapping) else {}


def host_kanban_config() -> Mapping[str, Any]:
    """Read ``kanban`` policy from the DEFAULT (host) Hermes home.

    Workers and dispatchers run with ``HERMES_HOME`` scoped to a profile, but
    one subscription is shared across profiles, so the throttle has exactly one
    authoritative configuration.  Reuses the quota circuit's reader rather than
    growing a second one that could drift from it.
    """
    from hermes_cli.kanban_quota_circuit import _kanban_config

    return _kanban_config()


def load_throttle_config(kanban_cfg: Optional[Mapping[str, Any]] = None) -> ThrottleConfig:
    """Parse ``kanban.usage_throttle``.  Never raises: a malformed value falls
    back to its shipped default rather than wedging dispatch."""
    if kanban_cfg is None:
        try:
            kanban_cfg = host_kanban_config()
        except Exception:
            logger.debug("kanban throttle: host config unreadable", exc_info=True)
            kanban_cfg = {}
    raw = _section(kanban_cfg if isinstance(kanban_cfg, Mapping) else {}, "usage_throttle")
    levers = _section(raw, "levers")
    reduce_raw = _section(levers, "reduce_concurrency")
    downgrade_raw = _section(levers, "downgrade_model")
    drain_raw = _section(levers, "pause_drain")
    failover_raw = _section(levers, "cross_provider_failover")
    destination = _section(failover_raw, "destination")
    resume_raw = _section(raw, "resume")
    return ThrottleConfig(
        enabled=_bool(raw.get("enabled"), True),
        source_providers=_strings(raw.get("source_providers")) or ("anthropic",),
        signal_max_age_seconds=_int(raw.get("signal_max_age_seconds"), 900),
        poll_interval_seconds=_int(raw.get("poll_interval_seconds"), 120),
        fetch_timeout_seconds=max(1.0, _num(raw.get("fetch_timeout_seconds"), 20.0)),
        resume_threshold_pct=_pct(resume_raw.get("threshold_pct"), 50.0),
        log_event=_bool(resume_raw.get("log_event"), True),
        reduce=ConcurrencyLever(
            enabled=_bool(reduce_raw.get("enabled"), True),
            threshold_pct=_pct(reduce_raw.get("threshold_pct"), 70.0),
            max_in_progress=_int(reduce_raw.get("max_in_progress"), 2),
        ),
        downgrade=DowngradeLever(
            enabled=_bool(downgrade_raw.get("enabled"), True),
            threshold_pct=_pct(downgrade_raw.get("threshold_pct"), 80.0),
            ladder=_strings(downgrade_raw.get("ladder")),
        ),
        drain=LeverConfig(
            enabled=_bool(drain_raw.get("enabled"), True),
            threshold_pct=_pct(drain_raw.get("threshold_pct"), 90.0),
        ),
        failover=FailoverLever(
            # Default OFF, and it stays off until an operator names both an
            # eligible profile and a destination route.
            enabled=_bool(failover_raw.get("enabled"), False),
            threshold_pct=_pct(failover_raw.get("threshold_pct"), 95.0),
            eligible_profiles=_strings(failover_raw.get("eligible_profiles")),
            destination_provider=(str(destination.get("provider") or "").strip() or None),
            destination_model=(str(destination.get("model") or "").strip() or None),
            destination_max_pressure_pct=_pct(
                destination.get("max_pressure_pct"), 50.0
            ),
        ),
    )


# --- Capacity signals ---------------------------------------------------


@dataclass(frozen=True)
class CapacitySignal:
    """One provider's worst ACTIVE quota window, or why there isn't one."""

    provider: str
    fresh: bool
    used_percent: Optional[float] = None
    window_label: Optional[str] = None
    observed_at: Optional[int] = None
    reason: Optional[str] = None


_cache_lock = threading.Lock()
#: provider -> (expires_at, snapshot_or_None).  Negative results are cached too,
#: so an unreachable endpoint costs one bounded call per poll interval rather
#: than one per dispatcher tick on every board.
_snapshot_cache: dict[str, tuple[float, Any]] = {}


def reset_signal_cache() -> None:
    """Drop the memoized provider snapshots (tests, and `/usage` refreshes)."""
    with _cache_lock:
        _snapshot_cache.clear()


def _fetch_snapshot(provider: str, *, timeout: float):
    """Wall-clock-bounded authenticated fetch through the capacity adapters.

    :func:`agent.kanban_throttle_capacity.fetch_capacity_snapshot` already fails
    open to ``None``; the pool bounds a hung socket so a slow provider cannot
    stall a dispatch tick.
    """
    import concurrent.futures

    from agent.kanban_throttle_capacity import fetch_capacity_snapshot

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fetch_capacity_snapshot, provider).result(timeout=timeout)


def _cached_snapshot(provider: str, *, cfg: ThrottleConfig, now: float):
    with _cache_lock:
        entry = _snapshot_cache.get(provider)
        if entry is not None and entry[0] > now:
            return entry[1]
    try:
        snapshot = _fetch_snapshot(provider, timeout=cfg.fetch_timeout_seconds)
    except Exception:
        logger.debug("kanban throttle: %s capacity fetch failed", provider, exc_info=True)
        snapshot = None
    with _cache_lock:
        _snapshot_cache[provider] = (now + cfg.poll_interval_seconds, snapshot)
    return snapshot


def _unsupported_reason(provider: str) -> Optional[str]:
    """``unsupported_provider`` when no adapter can ever answer for *provider*.

    Answered from the capability table alone, so it costs no network call and is
    settled BEFORE the fetch seam: "never readable" and "did not answer this
    time" are different operator problems, and deciding them at different points
    is what keeps them from collapsing into one reason code.
    """
    from agent.kanban_throttle_capacity import UNSUPPORTED_PROVIDER, capability_for

    return None if capability_for(provider).supported else UNSUPPORTED_PROVIDER


def _worst_active_window(snapshot) -> tuple[Optional[float], Optional[str]]:
    """Worst ``used_percent`` among windows that are not explicitly inactive.

    ``is_active`` is tri-state upstream: ``False`` means the provider says this
    window is not currently counting, and ``None`` means it did not say — which
    must not be read as "inactive", or a provider that omits the field would
    silently report no pressure at all.

    A window the provider marks ``limit_reached`` counts as fully consumed
    whatever percentage it reports beside that flag.  Codex serves exactly this
    shape — ``limit_reached`` true alongside a low ``used_percent`` — and taking
    the percentage at face value would admit a wave of workers onto an account
    the provider has already closed.  The flag is the provider's own
    authoritative statement, so honouring it is faithfulness, not estimation.
    """
    worst: Optional[float] = None
    label: Optional[str] = None
    for window in getattr(snapshot, "windows", ()) or ():
        if getattr(window, "is_active", None) is False:
            continue
        used = getattr(window, "used_percent", None)
        if getattr(window, "limit_reached", None) is True:
            used = 100.0
        if used is None or isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        value = float(used)
        if worst is None or value > worst:
            worst, label = value, getattr(window, "label", None)
    return worst, label


def _snapshot_exhausted(snapshot) -> bool:
    """The provider states this account cannot currently serve requests.

    Snapshot-wide counterpart to the per-window flag: Codex reports
    ``allowed``/``limit_reached`` at the top level of its usage payload, and an
    account in that state has zero capacity regardless of which window is worst.
    """
    return (
        getattr(snapshot, "limit_reached", None) is True
        or getattr(snapshot, "allowed", None) is False
    )


def capacity_signal(
    provider: str, *, cfg: ThrottleConfig, now: Optional[int] = None,
) -> CapacitySignal:
    """Read one provider's capacity, classifying every unusable case by name."""
    current = int(time.time()) if now is None else int(now)
    unsupported = _unsupported_reason(provider)
    if unsupported is not None:
        # No adapter will ever answer for this provider.  Decided before the
        # fetch, so an unsupported provider costs no network call at all, and is
        # distinct from a failed fetch: retrying cannot help, so the operator's
        # action is to change the configured source rather than to wait.
        return CapacitySignal(provider=provider, fresh=False, reason=unsupported)
    snapshot = _cached_snapshot(provider, cfg=cfg, now=float(current))
    if snapshot is None:
        return CapacitySignal(provider=provider, fresh=False, reason="fetch_unavailable")
    if getattr(snapshot, "unavailable_reason", None):
        # Provider-stated unavailability (wrong credential kind, expired login).
        # A code this module minted survives by name — `stale_portal_reading`
        # tells the operator to re-authenticate, which `provider_unavailable`
        # does not — while arbitrary provider prose generalizes rather than
        # being echoed into a durable audit row.
        from agent.kanban_throttle_capacity import classify_unavailable_reason

        named = classify_unavailable_reason(snapshot.unavailable_reason)
        return CapacitySignal(
            provider=provider, fresh=False, reason=named or "provider_unavailable",
        )
    fetched_at = getattr(snapshot, "fetched_at", None)
    observed_at: Optional[int] = None
    if fetched_at is not None:
        with contextlib.suppress(Exception):
            observed_at = int(fetched_at.timestamp())
    if observed_at is None:
        return CapacitySignal(provider=provider, fresh=False, reason="no_timestamp")
    if current - observed_at > cfg.signal_max_age_seconds:
        return CapacitySignal(
            provider=provider, fresh=False, observed_at=observed_at, reason="stale",
        )
    used, label = _worst_active_window(snapshot)
    if _snapshot_exhausted(snapshot):
        # Account-level exhaustion outranks any window reading, including a
        # snapshot that carries no usable window at all: "provider says it is
        # closed" is a stronger, fresher fact than "no window parsed".
        return CapacitySignal(
            provider=provider, fresh=True, used_percent=100.0,
            window_label=label or "account", observed_at=observed_at,
            reason="provider_limit_reached",
        )
    if used is None:
        return CapacitySignal(
            provider=provider, fresh=False, observed_at=observed_at,
            reason="no_active_window",
        )
    return CapacitySignal(
        provider=provider, fresh=True, used_percent=used,
        window_label=label, observed_at=observed_at,
    )


def worst_capacity_signal(
    *, cfg: ThrottleConfig, providers: Optional[Sequence[str]] = None,
    now: Optional[int] = None,
) -> tuple[Optional[CapacitySignal], tuple[CapacitySignal, ...]]:
    """``(worst usable signal or None, every signal read)``."""
    read = tuple(
        capacity_signal(provider, cfg=cfg, now=now)
        for provider in (providers if providers is not None else cfg.source_providers)
    )
    usable = [s for s in read if s.fresh and s.used_percent is not None]
    if not usable:
        return None, read
    return max(usable, key=lambda s: s.used_percent or 0.0), read


def _degraded_classification(
    signals: Sequence[CapacitySignal],
) -> tuple[str, str, tuple[str, ...]]:
    """``(reason, recovery hint, per-provider detail)`` for an unusable read.

    Distinguishes "configured to read a provider that has no capacity contract"
    from "a provider that does have one did not answer this time".  Both hold
    the board's state exactly where it is; only the operator's remedy differs,
    and the record has to say which one it is or the hint sends them to
    `hermes /usage` for an account that will never report.

    An unsupported provider's detail carries the adapters' own EVIDENCE for
    that verdict, because "unsupported" with no justification is indistinguish-
    able from a bug: the operator has to be able to see that the provider was
    actually probed and serves no quota document, without reading source.
    """
    from agent.kanban_throttle_capacity import UNSUPPORTED_PROVIDER, capability_for

    detail = []
    for signal in signals:
        reason = signal.reason or "unknown"
        if reason == UNSUPPORTED_PROVIDER:
            reason = f"{reason} ({capability_for(signal.provider).evidence})"
        detail.append(f"{signal.provider}:{reason}")
    if signals and all(s.reason == UNSUPPORTED_PROVIDER for s in signals):
        return DEGRADED_UNSUPPORTED_SOURCE, _UNSUPPORTED_HINT, tuple(detail)
    return DEGRADED_NO_SIGNAL, _RECOVERY_HINT, tuple(detail)


# --- Persistent global state -------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS throttle_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    state             TEXT    NOT NULL,
    pressure_percent  REAL,
    provider          TEXT,
    window_label      TEXT,
    changed_at        INTEGER NOT NULL,
    observed_at       INTEGER NOT NULL,
    operator_intent   TEXT    NOT NULL DEFAULT '{}',
    degraded_reason   TEXT,
    degraded_since    INTEGER
);
CREATE TABLE IF NOT EXISTS throttle_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT    NOT NULL UNIQUE,
    kind         TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_throttle_events_created ON throttle_events(created_at);
CREATE TABLE IF NOT EXISTS throttle_degraded_episodes (
    key     TEXT    PRIMARY KEY,
    detail  TEXT,
    since   INTEGER NOT NULL,
    seq     INTEGER NOT NULL
);
"""


def throttle_state_db_path() -> Path:
    """Host coordination DB under the SHARED kanban home (never profile-scoped).

    Resolving through :func:`hermes_cli.kanban_db.kanban_home` keeps the
    ``HERMES_KANBAN_HOME`` sandbox honoured, so a test or probe pinned to a
    temporary home can never write the live throttle state.
    """
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / "kanban" / "usage-throttle.db"


def _connect() -> sqlite3.Connection:
    path = throttle_state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


@contextlib.contextmanager
def _write_conn():
    conn = _connect()
    try:
        # BEGIN IMMEDIATE, not a read: two boards' dispatchers evaluate on
        # their own ticks, and the transition decision is a read-modify-write.
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextlib.contextmanager
def _read_conn():
    """Read-only access with no ``BEGIN IMMEDIATE``.

    Diagnostics and the "is anything actually degraded right now" fast path are
    plain SELECTs; taking the write lock for them would serialise them against
    dispatcher ticks for no benefit.
    """
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _read_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM throttle_state WHERE id = 1").fetchone()
    if row is None:
        return {
            "state": STATE_NORMAL, "pressure_percent": None, "provider": None,
            "window_label": None, "changed_at": 0, "observed_at": 0,
            "operator_intent": {}, "degraded_reason": None, "degraded_since": None,
        }
    data = dict(row)
    try:
        intent = json.loads(data.get("operator_intent") or "{}")
    except Exception:
        intent = {}
    data["operator_intent"] = intent if isinstance(intent, dict) else {}
    return data


def _write_state(conn: sqlite3.Connection, state: Mapping[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO throttle_state
            (id, state, pressure_percent, provider, window_label, changed_at,
             observed_at, operator_intent, degraded_reason, degraded_since)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            state = excluded.state,
            pressure_percent = excluded.pressure_percent,
            provider = excluded.provider,
            window_label = excluded.window_label,
            changed_at = excluded.changed_at,
            observed_at = excluded.observed_at,
            operator_intent = excluded.operator_intent,
            degraded_reason = excluded.degraded_reason,
            degraded_since = excluded.degraded_since
        """,
        (
            state["state"], state.get("pressure_percent"), state.get("provider"),
            state.get("window_label"), int(state.get("changed_at") or 0),
            int(state.get("observed_at") or 0),
            json.dumps(state.get("operator_intent") or {}, sort_keys=True),
            state.get("degraded_reason"), state.get("degraded_since"),
        ),
    )


def _record_event(
    conn: sqlite3.Connection, kind: str, fingerprint: str, payload: Mapping[str, Any],
    *, now: int,
) -> bool:
    """Append one audit row.  Returns False when the fingerprint already exists.

    Deduplication is a UNIQUE constraint rather than a "did I log this
    recently" heuristic, so two boards racing the same transition record it
    exactly once and a restart cannot re-emit history.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO throttle_events (fingerprint, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (fingerprint, kind, json.dumps(dict(payload), sort_keys=True), int(now)),
    )
    return cur.rowcount == 1


def _open_degraded_episode(
    conn: sqlite3.Connection, key: str, detail: str, *, now: int,
) -> Optional[int]:
    """Start (or continue) a degraded episode; return its marker when NEW.

    ``None`` means the same condition is already being reported and this
    observation belongs to the episode already on record.  The marker is the
    per-episode component of the audit fingerprint, so a condition that recurs
    after :func:`_close_degraded_episode` earns a fresh row instead of being
    deduplicated against the first occurrence for the life of the store.

    The marker is a persisted monotonic sequence rather than the clock: two
    episodes can open and close inside one second, and a timestamp would let
    the second one collide with the first on the UNIQUE fingerprint — the very
    silent-dedup failure this exists to prevent.

    This is the same episode contract the no-signal path gets from the
    ``degraded_since`` column on the state row.  It lives in its own table
    because it is written from the per-spawn route-planning path, which must
    not join the dispatch tick's read-modify-write of that row.
    """
    row = conn.execute(
        "SELECT detail, seq FROM throttle_degraded_episodes WHERE key = ?", (key,)
    ).fetchone()
    if row is not None and row["detail"] is not None and str(row["detail"]) == detail:
        return None
    marker = int(row["seq"] if row is not None else 0) + 1
    conn.execute(
        "INSERT INTO throttle_degraded_episodes (key, detail, since, seq) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET detail = excluded.detail, "
        "since = excluded.since, seq = excluded.seq",
        (key, detail, int(now), marker),
    )
    return marker


def _close_degraded_episode(conn: sqlite3.Connection, key: str, *, now: int) -> None:
    """Mark the condition resolved, so a later recurrence is a NEW episode.

    The row is kept (with a null detail) rather than deleted: the sequence it
    carries is what makes the next episode's fingerprint distinct from this
    one's.
    """
    conn.execute(
        "UPDATE throttle_degraded_episodes SET detail = NULL, since = ? WHERE key = ?",
        (int(now), key),
    )


def recent_throttle_events(limit: int = 20) -> list[dict[str, Any]]:
    """Newest-first audit records, for diagnostics surfaces."""
    with _read_conn() as conn:
        rows = conn.execute(
            "SELECT kind, payload, created_at FROM throttle_events "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(max(1, limit)),),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            payload = {}
        events.append(
            {"kind": row["kind"], "created_at": int(row["created_at"]), "payload": payload}
        )
    return events


# --- Decision -----------------------------------------------------------


@dataclass(frozen=True)
class ThrottleDecision:
    """What admission control should do this tick."""

    enabled: bool
    state: str
    previous_state: str
    changed: bool
    pressure_percent: Optional[float]
    provider: Optional[str]
    window_label: Optional[str]
    degraded: bool
    degraded_reason: Optional[str]
    observed_at: int
    operator_intent: Mapping[str, Any]
    config: Optional[ThrottleConfig] = None

    @property
    def drain(self) -> bool:
        """No NEW claims.  In-flight work is never touched."""
        return self.state == STATE_DRAIN

    @property
    def downgrade(self) -> bool:
        return _STATE_RANK[self.state] >= _STATE_RANK[STATE_DOWNGRADE]

    @property
    def max_in_progress(self) -> Optional[int]:
        """Automatic concurrency ceiling, or ``None`` when the lever is idle."""
        if self.config is None or not self.config.reduce.enabled:
            return None
        if _STATE_RANK[self.state] < _STATE_RANK[STATE_REDUCE]:
            return None
        return self.config.reduce.max_in_progress

    def narrowed_max_in_progress(self, configured: Optional[int]) -> Optional[int]:
        """Combine with the operator's own cap.  Only ever tightens: the
        operator's value stays authoritative and is returned unchanged whenever
        it is already at or below the automatic ceiling."""
        automatic = self.max_in_progress
        bounds = [b for b in (configured, automatic) if b is not None]
        return min(bounds) if bounds else None

    def public_state(self) -> dict[str, Any]:
        """JSON-safe summary for CLI/dashboard/telemetry.  Carries no
        credential, account identifier or raw provider payload."""
        return {
            "enabled": self.enabled,
            "state": self.state,
            "previous_state": self.previous_state,
            "changed": self.changed,
            "pressure_percent": self.pressure_percent,
            "provider": self.provider,
            "window": self.window_label,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "observed_at": self.observed_at,
            "max_in_progress": self.max_in_progress,
            "drain": self.drain,
            "downgrade": self.downgrade,
            "operator_intent": dict(self.operator_intent or {}),
            "recovery": self.recovery_hint,
        }

    @property
    def recovery_hint(self) -> Optional[str]:
        """What the operator should actually DO about the current degraded
        state.  An unsupported source provider is not fixed by checking
        `/usage`, so pointing there would send them chasing a reading that
        cannot exist."""
        if not self.degraded:
            return None
        if self.degraded_reason == DEGRADED_UNSUPPORTED_SOURCE:
            return _UNSUPPORTED_HINT
        return _RECOVERY_HINT


def _lever_for(state: str, cfg: ThrottleConfig) -> Optional[LeverConfig]:
    return {
        STATE_REDUCE: cfg.reduce, STATE_DOWNGRADE: cfg.downgrade, STATE_DRAIN: cfg.drain,
    }.get(state)


def target_state(pressure: float, cfg: ThrottleConfig) -> str:
    """Highest armed lever at this pressure, ignoring hysteresis."""
    for state in (STATE_DRAIN, STATE_DOWNGRADE, STATE_REDUCE):
        lever = _lever_for(state, cfg)
        if lever is not None and lever.enabled and pressure >= lever.threshold_pct:
            return state
    return STATE_NORMAL


def next_state(current: str, pressure: float, cfg: ThrottleConfig) -> str:
    """Deterministic transition with threshold hysteresis.

    Escalation is immediate.  De-escalation happens ONLY all the way back to
    ``normal``, and only at or below ``resume.threshold_pct`` — a separate knob
    from every step-down threshold, so the board cannot oscillate across a
    single boundary.  A lever the operator turned off releases its own state at
    once, otherwise disabling a lever would leave the board stuck in it.
    """
    current = current if current in _STATE_RANK else STATE_NORMAL
    target = target_state(pressure, cfg)
    if _STATE_RANK[target] > _STATE_RANK[current]:
        return target
    lever = _lever_for(current, cfg)
    if lever is not None and not lever.enabled:
        return target
    if pressure <= cfg.resume_threshold_pct:
        return STATE_NORMAL
    return current


def _inert_decision(cfg: Optional[ThrottleConfig], now: int) -> ThrottleDecision:
    return ThrottleDecision(
        enabled=False, state=STATE_NORMAL, previous_state=STATE_NORMAL, changed=False,
        pressure_percent=None, provider=None, window_label=None, degraded=False,
        degraded_reason=None, observed_at=now, operator_intent={}, config=cfg,
    )


def evaluate_throttle(
    *, kanban_cfg: Optional[Mapping[str, Any]] = None,
    operator_max_in_progress: Optional[int] = None,
    board: Optional[str] = None,
    now: Optional[int] = None,
    persist: bool = True,
) -> ThrottleDecision:
    """Evaluate global admission pressure and persist any transition.

    Called once per dispatcher tick.  ``operator_max_in_progress`` is the cap
    the operator configured; it is recorded as intent while the board is
    healthy so the recovery record can state exactly what was restored.

    ``persist=False`` (a ``--dry-run`` tick) reads the same live state and the
    same real capacity signal but writes neither the state row nor an audit
    event: a report of what a tick *would* do must not manufacture a transition
    record the operator then sees as history.
    """
    current_time = int(time.time()) if now is None else int(now)
    cfg = load_throttle_config(kanban_cfg)
    if not cfg.enabled:
        # Fully inert: no fetch, no state row, no events.
        return _inert_decision(cfg, current_time)

    signal, all_signals = worst_capacity_signal(cfg=cfg, now=current_time)

    with _write_conn() as conn:
        stored = _read_state(conn)
        previous = str(stored.get("state") or STATE_NORMAL)
        intent = dict(stored.get("operator_intent") or {})

        if not persist:
            if signal is None or signal.used_percent is None:
                reason, _hint, _detail = _degraded_classification(all_signals)
                return ThrottleDecision(
                    enabled=True, state=previous, previous_state=previous, changed=False,
                    pressure_percent=None, provider=None, window_label=None, degraded=True,
                    degraded_reason=reason, observed_at=current_time,
                    operator_intent=intent, config=cfg,
                )
            resolved = next_state(previous, float(signal.used_percent), cfg)
            return ThrottleDecision(
                enabled=True, state=resolved, previous_state=previous,
                changed=resolved != previous, pressure_percent=float(signal.used_percent),
                provider=signal.provider, window_label=signal.window_label, degraded=False,
                degraded_reason=None, observed_at=current_time,
                operator_intent=intent, config=cfg,
            )

        if signal is None or signal.used_percent is None:
            # Unknown pressure: hold the persisted state verbatim in BOTH
            # directions and say so.  One degraded episode records once; a
            # later episode (after a healthy reading) records again.
            reason, hint, detail = _degraded_classification(all_signals)
            degraded_since = stored.get("degraded_since") or current_time
            if stored.get("degraded_reason") != reason or not stored.get("degraded_since"):
                _record_event(
                    conn, "degraded", f"degraded:{reason}:{degraded_since}",
                    {
                        "reason": reason, "state": previous,
                        "providers": list(cfg.source_providers),
                        # Per-provider reason CODES only: the adapters classify
                        # every failure by name, and none of those names carry a
                        # credential, account identifier or payload fragment.
                        "provider_detail": list(detail),
                        "recovery": hint,
                    },
                    now=current_time,
                )
                logger.warning(
                    "kanban usage throttle: no usable capacity signal (%s); holding "
                    "state %r and making no speculative change (%s)",
                    reason, previous, hint,
                )
            _write_state(conn, {
                **stored, "state": previous, "operator_intent": intent,
                "degraded_reason": reason, "degraded_since": degraded_since,
                "observed_at": current_time,
                "changed_at": int(stored.get("changed_at") or current_time),
            })
            return ThrottleDecision(
                enabled=True, state=previous, previous_state=previous, changed=False,
                pressure_percent=None, provider=None, window_label=None, degraded=True,
                degraded_reason=reason, observed_at=current_time,
                operator_intent=intent, config=cfg,
            )

        pressure = float(signal.used_percent)
        resolved = next_state(previous, pressure, cfg)
        changed = resolved != previous
        if resolved == STATE_NORMAL:
            # Healthy: the operator's own settings are the live ones, so keep
            # the recorded intent current for the next escalation.
            intent = {"max_in_progress": operator_max_in_progress}
        if changed:
            _record_event(
                conn, "state_change",
                f"state:{previous}->{resolved}:{current_time}",
                {
                    "from": previous, "to": resolved,
                    "pressure_percent": round(pressure, 2),
                    "provider": signal.provider, "window": signal.window_label,
                    "board": board,
                    "restored_operator_intent": intent if resolved == STATE_NORMAL else None,
                    "automatic_max_in_progress": (
                        cfg.reduce.max_in_progress
                        if cfg.reduce.enabled
                        and _STATE_RANK[resolved] >= _STATE_RANK[STATE_REDUCE]
                        else None
                    ),
                },
                now=current_time,
            )
            logger.warning(
                "kanban usage throttle: %s -> %s at %.1f%% (%s %s)",
                previous, resolved, pressure, signal.provider, signal.window_label or "",
            )
        _write_state(conn, {
            "state": resolved, "pressure_percent": pressure, "provider": signal.provider,
            "window_label": signal.window_label,
            "changed_at": current_time if changed else int(stored.get("changed_at") or current_time),
            "observed_at": current_time, "operator_intent": intent,
            "degraded_reason": None, "degraded_since": None,
        })

    return ThrottleDecision(
        enabled=True, state=resolved, previous_state=previous, changed=changed,
        pressure_percent=pressure, provider=signal.provider,
        window_label=signal.window_label, degraded=False, degraded_reason=None,
        observed_at=current_time, operator_intent=intent, config=cfg,
    )


# --- Route planning -----------------------------------------------------


@dataclass(frozen=True)
class RoutePlan:
    """An in-memory route change for ONE spawn.  The card row is never rewritten."""

    model: Optional[str]
    provider: Optional[str]
    kind: str  # "downgrade" | "failover"
    reason: str


def _ladder_step(model: Optional[str], ladder: Sequence[str]) -> Optional[str]:
    """Next rung down, or ``None`` when the model is absent from the ladder or
    already on its last rung.  An unknown model is left alone: guessing a
    cheaper equivalent is exactly the speculative change this design refuses."""
    name = str(model or "").strip()
    if not name or not ladder:
        return None
    rungs = [r for r in ladder if r]
    try:
        index = next(i for i, rung in enumerate(rungs) if rung.casefold() == name.casefold())
    except StopIteration:
        return None
    return rungs[index + 1] if index + 1 < len(rungs) else None


#: Episode key for the failover-destination degraded condition.  One key,
#: because one destination account is one condition: a refusal that changes
#: detail code (stale -> missing) is the same ongoing episode re-described,
#: and gets its own row because the detail is part of the fingerprint.
_EPISODE_FAILOVER_DESTINATION = "failover_destination"


def _failover_declined(reason: str, *, now: int) -> None:
    """Record one deduplicated degraded episode for a refused reroute.

    Deduplication is per EPISODE, not for the lifetime of the store: a refusal
    that recurs after the destination was proven healthy again is a new fact an
    operator needs, so it earns its own audit row and warning.  Within one
    episode the refusal records once however many spawns consult it.
    """
    with contextlib.suppress(Exception):
        with _write_conn() as conn:
            marker = _open_degraded_episode(
                conn, _EPISODE_FAILOVER_DESTINATION, reason, now=now,
            )
            if marker is None:
                return
            if _record_event(
                conn, "degraded",
                f"failover:{DEGRADED_FAILOVER_DESTINATION}:{reason}:{marker}",
                {
                    "reason": DEGRADED_FAILOVER_DESTINATION, "detail": reason,
                    "action": "no route change", "recovery": _RECOVERY_HINT,
                },
                now=now,
            ):
                logger.warning(
                    "kanban usage throttle: cross-provider failover declined (%s); "
                    "no route change", reason,
                )


def _failover_destination_proven(*, now: int) -> None:
    """The destination read cleanly: end any open refusal episode.

    Consulted on every spawn while the lever is armed, so the common case —
    nothing to close — is answered by a plain SELECT and never takes the write
    lock away from a dispatcher tick.
    """
    with contextlib.suppress(Exception):
        with _read_conn() as conn:
            row = conn.execute(
                "SELECT detail FROM throttle_degraded_episodes WHERE key = ?",
                (_EPISODE_FAILOVER_DESTINATION,),
            ).fetchone()
            if row is None or row["detail"] is None:
                return
        with _write_conn() as conn:
            _close_degraded_episode(conn, _EPISODE_FAILOVER_DESTINATION, now=now)


def failover_eligible_profiles(
    decision: ThrottleDecision, *, now: Optional[int] = None,
) -> tuple[str, ...]:
    """Profiles whose work may be rerouted RIGHT NOW, or ``()``.

    Every gate is evaluated here, dual capacity freshness included, so callers
    get one answer rather than each re-deriving the policy.  ``()`` is the
    shipped answer and the answer whenever anything is unproven.

    This is what makes the failover lever reachable at all: pressure high
    enough to arm it is also high enough to drain, and a drain that returned
    unconditionally would make the lever dead code.  Only these profiles may
    still be admitted while draining — everyone else waits, because their work
    would land on the exhausted account.
    """
    cfg = decision.config
    if cfg is None or not decision.enabled or not cfg.failover.enabled:
        return ()
    lever = cfg.failover
    if not lever.eligible_profiles or not lever.destination_provider:
        return ()
    if decision.degraded or decision.pressure_percent is None:
        return ()
    if decision.pressure_percent < lever.threshold_pct:
        return ()
    current = int(time.time()) if now is None else int(now)
    destination = capacity_signal(lever.destination_provider, cfg=cfg, now=current)
    if not destination.fresh or destination.used_percent is None:
        _failover_declined(destination.reason or "unknown", now=current)
        return ()
    if destination.used_percent > lever.destination_max_pressure_pct:
        _failover_declined("destination_under_pressure", now=current)
        return ()
    # Proven healthy: any refusal episode is over, so the NEXT refusal is a new
    # episode with its own audit row rather than a silent duplicate of the last.
    _failover_destination_proven(now=current)
    return lever.eligible_profiles


def plan_failover(
    decision: ThrottleDecision, *, assignee: Optional[str], now: Optional[int] = None,
) -> Optional[RoutePlan]:
    """Plan a cross-provider reroute, or ``None`` when ANY gate is unmet.

    Every gate must hold: the lever is explicitly enabled, the assignee is named
    in the allowlist, a destination route is configured, the SOURCE reading is
    fresh and at/over the failover threshold, and the DESTINATION's own
    authenticated reading is fresh and proves headroom.  A destination with no
    trustworthy signal is treated as unknown, not as spare capacity.
    """
    profile = str(assignee or "").strip()
    if not profile:
        return None
    if profile not in failover_eligible_profiles(decision, now=now):
        return None
    cfg = decision.config
    assert cfg is not None  # guaranteed by failover_eligible_profiles
    lever = cfg.failover
    current = int(time.time()) if now is None else int(now)
    destination = capacity_signal(lever.destination_provider or "", cfg=cfg, now=current)
    return RoutePlan(
        model=lever.destination_model, provider=lever.destination_provider,
        kind="failover",
        reason=(
            f"source {decision.pressure_percent or 0:.0f}% >= {lever.threshold_pct:.0f}%; "
            f"destination {destination.used_percent or 0:.0f}% "
            f"<= {lever.destination_max_pressure_pct:.0f}%"
        ),
    )


def plan_route_change(
    decision: ThrottleDecision, *, assignee: Optional[str], model: Optional[str],
    provider: Optional[str], now: Optional[int] = None,
) -> Optional[RoutePlan]:
    """The route this spawn should use instead, or ``None`` to leave it alone.

    Failover is considered first because it is the higher-pressure lever; it is
    off by default, so the ordinary answer is the ladder downgrade.
    """
    cfg = decision.config
    if cfg is None or not decision.enabled or decision.degraded:
        return None
    plan = plan_failover(decision, assignee=assignee, now=now)
    if plan is not None:
        return plan
    if not decision.downgrade or not cfg.downgrade.enabled:
        return None
    downgraded = _ladder_step(model, cfg.downgrade.ladder)
    if downgraded is None:
        return None
    return RoutePlan(
        model=downgraded, provider=provider, kind="downgrade",
        reason=(
            f"pressure {decision.pressure_percent:.0f}% "
            f">= {cfg.downgrade.threshold_pct:.0f}%"
        ),
    )


__all__ = [
    "STATE_DRAIN", "STATE_DOWNGRADE", "STATE_NORMAL", "STATE_REDUCE",
    "DEGRADED_FAILOVER_DESTINATION", "DEGRADED_NO_SIGNAL",
    "DEGRADED_UNSUPPORTED_SOURCE",
    "CapacitySignal", "RoutePlan", "ThrottleConfig", "ThrottleDecision",
    "capacity_signal", "evaluate_throttle", "failover_eligible_profiles",
    "load_throttle_config", "next_state", "plan_failover", "plan_route_change",
    "recent_throttle_events", "reset_signal_cache", "target_state",
    "throttle_state_db_path", "worst_capacity_signal",
]

# ``replace`` is re-exported for callers building variant configs in tests
# without reaching for dataclasses internals.
config_replace = replace
