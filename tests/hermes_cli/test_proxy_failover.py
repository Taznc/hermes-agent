"""Failover-gateway contract tests: routing policy, anti-loop, circuits, protocol.

These cover the multi-backend ingress only. Single-provider pass-through
behaviour is covered by ``test_proxy.py`` and must remain unchanged.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from hermes_cli.proxy.routing import (
    FAILOVER_STATUS_CODES,
    BackendCircuit,
    RouteContext,
    is_failover_exception,
    is_failover_status,
)

aiohttp = pytest.importorskip("aiohttp")


@pytest.mark.parametrize("status", sorted(FAILOVER_STATUS_CODES))
def test_allowed_failure_statuses_fail_over(status):
    assert is_failover_status(status) is True


@pytest.mark.parametrize("status", [200, 201, 204, 400, 401, 403, 404, 405, 409, 413, 422])
def test_forbidden_statuses_are_never_replayed(status):
    assert is_failover_status(status) is False


def test_allowed_transport_failures_fail_over():
    assert is_failover_exception(aiohttp.ClientError("boom")) is True
    assert is_failover_exception(asyncio.TimeoutError()) is True


def test_unrelated_exceptions_do_not_fail_over():
    assert is_failover_exception(ValueError("bad payload")) is False
    assert is_failover_exception(KeyError("missing")) is False


def test_route_context_visits_each_backend_at_most_once():
    ctx = RouteContext.mint(max_attempts=2)
    assert ctx.request_id
    assert ctx.attempts == 0

    assert ctx.may_attempt("claude-code") is True
    ctx.record_attempt("claude-code")
    assert ctx.attempts == 1
    assert ctx.may_attempt("claude-code") is False

    assert ctx.may_attempt("openai-codex") is True
    ctx.record_attempt("openai-codex")
    assert ctx.attempts == 2
    # Bounded by the configured backend count, so a third backend is refused
    # even though it was never visited.
    assert ctx.may_attempt("some-third-backend") is False


def test_circuit_opens_after_threshold_and_blocks_further_attempts():
    clock = [1000.0]
    circuit = BackendCircuit(failure_threshold=3, cooldown_seconds=60, clock=lambda: clock[0])

    assert circuit.allows("claude-code") is True
    for _ in range(2):
        circuit.record_failure("claude-code")
    assert circuit.allows("claude-code") is True

    circuit.record_failure("claude-code")
    assert circuit.allows("claude-code") is False
    # An unrelated backend is unaffected.
    assert circuit.allows("openai-codex") is True


def test_circuit_success_resets_failure_count():
    clock = [1000.0]
    circuit = BackendCircuit(failure_threshold=2, cooldown_seconds=60, clock=lambda: clock[0])
    circuit.record_failure("claude-code")
    circuit.record_success("claude-code")
    circuit.record_failure("claude-code")
    assert circuit.allows("claude-code") is True


def test_circuit_honors_upstream_retry_deadline_over_default_cooldown():
    """A validated reset deadline wins, so we never probe before the quota returns."""
    clock = [1000.0]
    circuit = BackendCircuit(failure_threshold=1, cooldown_seconds=5, clock=lambda: clock[0])
    circuit.record_failure("claude-code", retry_after_seconds=300)
    assert circuit.allows("claude-code") is False

    clock[0] = 1000.0 + 10  # past the default cooldown, inside the upstream deadline
    assert circuit.allows("claude-code") is False

    clock[0] = 1000.0 + 301
    assert circuit.allows("claude-code") is True


def test_only_one_half_open_probe_is_admitted_at_a_time():
    clock = [1000.0]
    circuit = BackendCircuit(failure_threshold=1, cooldown_seconds=30, clock=lambda: clock[0])
    circuit.record_failure("claude-code")
    clock[0] += 31

    assert circuit.allows("claude-code") is True   # this caller is the probe
    assert circuit.allows("claude-code") is False  # concurrent callers are refused
    assert circuit.allows("claude-code") is False

    circuit.record_success("claude-code")
    assert circuit.allows("claude-code") is True   # closed again, unrestricted


def test_circuit_counters_are_race_safe_under_concurrent_failures():
    circuit = BackendCircuit(failure_threshold=10_000, cooldown_seconds=60)
    barrier = threading.Barrier(8)

    def hammer():
        barrier.wait()
        for _ in range(250):
            circuit.record_failure("claude-code")

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert circuit.failure_count("claude-code") == 8 * 250


def test_open_circuit_admits_no_probe_storm_under_concurrency():
    """An open breaker must admit at most one probe no matter how many race in."""
    clock = [1000.0]
    circuit = BackendCircuit(failure_threshold=1, cooldown_seconds=30, clock=lambda: clock[0])
    circuit.record_failure("claude-code")
    clock[0] += 31

    admitted: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def probe():
        barrier.wait()
        allowed = circuit.allows("claude-code")
        with lock:
            admitted.append(allowed)

    threads = [threading.Thread(target=probe) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(admitted) == 1

