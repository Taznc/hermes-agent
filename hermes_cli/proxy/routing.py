"""Failover routing policy for the multi-backend proxy gateway.

Two decisions live here and nowhere else: *which* failures are eligible for a
retry on a different backend, and *how many* attempts one client request may
consume. Keeping them in one module is what makes the anti-loop contract
auditable — the gateway holds no second opinion about either question.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set

try:
    import aiohttp
except ImportError:  # pragma: no cover - proxy entry points already guard this
    aiohttp = None  # type: ignore[assignment]

# Provider-side unavailability. 529 is Anthropic's documented "overloaded".
FAILOVER_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504, 529})

# Header names the gateway owns. A client spelling of any of these is stripped
# at ingress so route context can only ever be minted by the gateway itself.
INTERNAL_ROUTE_HEADERS: frozenset[str] = frozenset({
    "x-hermes-request-id",
    "x-hermes-route-attempt",
    "x-hermes-route-visited",
    "x-hermes-route-backend",
})

REQUEST_ID_HEADER = "X-Hermes-Request-Id"
ROUTE_BACKEND_HEADER = "X-Hermes-Route-Backend"
ROUTE_ATTEMPT_HEADER = "X-Hermes-Route-Attempt"


def is_failover_status(status: int) -> bool:
    """Whether an upstream HTTP status may be retried on another backend.

    Everything not listed is terminal by construction: a validation, auth, or
    configuration error means the *request* is wrong, and replaying it against a
    second subscription only spends a second account's quota on the same 400.
    """
    return int(status) in FAILOVER_STATUS_CODES


def is_failover_exception(exc: BaseException) -> bool:
    """Whether a transport-level failure may be retried on another backend."""
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if aiohttp is not None and isinstance(exc, aiohttp.ClientError):
        return True
    return False


@dataclass
class RouteContext:
    """Per-request routing state: identity, attempt count, visited backends."""

    request_id: str
    max_attempts: int
    visited: Set[str] = field(default_factory=set)
    attempts: int = 0

    @classmethod
    def mint(cls, *, max_attempts: int) -> "RouteContext":
        """Create trusted context. Only the gateway calls this."""
        return cls(request_id=uuid.uuid4().hex, max_attempts=max(1, int(max_attempts)))

    def may_attempt(self, backend: str) -> bool:
        """True only for an unvisited backend within the attempt bound."""
        if self.attempts >= self.max_attempts:
            return False
        return backend not in self.visited

    def record_attempt(self, backend: str) -> None:
        self.visited.add(backend)
        self.attempts += 1


@dataclass
class _CircuitState:
    failures: int = 0
    opened_until: float = 0.0
    probe_in_flight: bool = False
    probe_owner: Optional[str] = None
    probe_started_at: float = 0.0


class BackendCircuit:
    """Per-backend breaker guarding a subscription that is already known to be down.

    Its job is not resilience for its own sake — it is to stop a capped account
    from being re-probed on every request, which both wastes a round trip per
    call and can extend the provider's own rate-limit window.

    Every mutation and the admission decision run under one lock, so the
    check-then-act in :meth:`allows` cannot admit two half-open probes.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._threshold = max(1, int(failure_threshold))
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._probe_timeout = max(1.0, self._cooldown)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._states: Dict[str, _CircuitState] = {}

    def _state(self, backend: str) -> _CircuitState:
        state = self._states.get(backend)
        if state is None:
            state = _CircuitState()
            self._states[backend] = state
        return state

    def allows(self, backend: str, *, probe_id: Optional[str] = None) -> bool:
        """Admit this caller to ``backend``; claims the half-open probe slot.

        A ``True`` for an open circuit means "you are the one probe". The
        caller reports a normal outcome through ``record_success`` or
        ``record_failure`` and calls ``abandon_probe`` if the attempt exits
        without an outcome. As a fallback for disconnects the server does not
        observe, an unreported probe lease expires after one cooldown.
        """
        with self._lock:
            state = self._state(backend)
            if state.opened_until <= 0.0:
                return True
            now = self._clock()
            if now < state.opened_until:
                return False
            if state.probe_in_flight:
                if now - state.probe_started_at < self._probe_timeout:
                    return False
                # A caller can disappear without the server receiving a
                # cancellation. Expire that orphaned lease so one new probe can
                # test the backend instead of excluding it until restart.
                state.probe_in_flight = False
                state.probe_owner = None
            state.probe_in_flight = True
            state.probe_owner = probe_id
            state.probe_started_at = now
            return True

    def abandon_probe(self, backend: str, *, probe_id: Optional[str] = None) -> None:
        """Release only the half-open probe slot owned by ``probe_id``.

        Ownership prevents a late cancellation from an older request from
        releasing a newer request's probe. The circuit stays half-open so the
        next caller can immediately test the recovered backend.
        """
        with self._lock:
            state = self._state(backend)
            if state.probe_in_flight and state.probe_owner == probe_id:
                state.probe_in_flight = False
                state.probe_owner = None
                state.probe_started_at = 0.0

    def record_failure(self, backend: str, *, retry_after_seconds: Optional[float] = None) -> None:
        """Count one failure and (re)open the circuit once past threshold.

        ``retry_after_seconds`` is the upstream's own validated deadline
        (``Retry-After``). It wins over the local cooldown so a half-open probe
        can never fire before the provider says the quota is back.
        """
        with self._lock:
            state = self._state(backend)
            state.failures += 1
            state.probe_in_flight = False
            state.probe_owner = None
            state.probe_started_at = 0.0
            if state.failures < self._threshold and retry_after_seconds is None:
                return
            cooldown = self._cooldown
            if retry_after_seconds is not None:
                cooldown = max(cooldown, max(0.0, float(retry_after_seconds)))
            state.opened_until = self._clock() + cooldown

    def record_success(self, backend: str) -> None:
        """Close the circuit; the backend answered."""
        with self._lock:
            state = self._state(backend)
            state.failures = 0
            state.opened_until = 0.0
            state.probe_in_flight = False
            state.probe_owner = None
            state.probe_started_at = 0.0

    def failure_count(self, backend: str) -> int:
        with self._lock:
            return self._state(backend).failures

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        """Observable breaker state for ``/health``; carries no request data."""
        with self._lock:
            now = self._clock()
            return {
                backend: {
                    "failures": state.failures,
                    "open": bool(state.opened_until > now),
                    "opens_for_seconds": max(0.0, round(state.opened_until - now, 3)),
                    "probe_in_flight": state.probe_in_flight,
                }
                for backend, state in self._states.items()
            }


__all__ = [
    "FAILOVER_STATUS_CODES",
    "INTERNAL_ROUTE_HEADERS",
    "REQUEST_ID_HEADER",
    "ROUTE_ATTEMPT_HEADER",
    "ROUTE_BACKEND_HEADER",
    "BackendCircuit",
    "RouteContext",
    "is_failover_exception",
    "is_failover_status",
]
