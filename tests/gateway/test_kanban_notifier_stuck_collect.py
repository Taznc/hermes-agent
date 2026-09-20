"""A stuck kanban notifier collect must not be able to stop dispatch.

Behaviour contracts for the 2026-09-19 wedge: one `_notifier_collect` call ran
for 6.4h on a threadpool worker while the gateway stayed systemd-active and the
embedded dispatcher stopped ticking entirely.

Root cause of the WEDGE itself was GIL monopolization by that thread (a frozen
interpreter renders identically to a healthy one in a py-spy dump), not pool
starvation. But the notifier loop had no bound of any kind: it awaited
`asyncio.to_thread(_notifier_collect, ...)` with no timeout, so once a collect
stopped returning, the notifier loop was wedged for the life of the process and
every later tick was lost.

These are the contracts that make a slow/stuck collect survivable. They are
about the LOOP's behaviour, not about how the collect is implemented, so they
stay valid if the collect is rewritten.

Contract (b) is the one that rules out the obvious wrong fix. Cancelling an
`asyncio.to_thread` future does NOT stop the underlying thread — a bare
`asyncio.wait_for` would therefore leak one permanently-spinning thread per
tick and exhaust the pool within minutes, which is strictly worse than the bug
it "fixes".
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


class _StallingCollect:
    """Stands in for `_notifier_collect`: the first call blocks until released."""

    def __init__(self, *, release_value=None):
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.released = asyncio.Event()
        self._release_value = release_value if release_value is not None else []
        self._loop = None

    def __call__(self, *args, **kwargs):
        # Runs on a worker thread (via asyncio.to_thread).
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.calls == 1:
                # Block the worker thread exactly the way the wedged collect did.
                fut = asyncio.run_coroutine_threadsafe(self.released.wait(), self._loop)
                fut.result()
                return self._release_value
            return []
        finally:
            self.concurrent -= 1


def _make_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: object()}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = None
    return runner


async def _drive_notifier(monkeypatch, runner, collect, *, ticks, timeout=0.05):
    """Run the real notifier loop for `ticks` ticks with time compressed."""
    from gateway import kanban_watchers as kw

    collect._loop = asyncio.get_running_loop()
    monkeypatch.setattr(kw, "_notifier_collect", collect)
    # raising=False so these patches do not themselves fail on a build lacking the
    # bound — the contracts below must go red on the LOOP's behaviour (an
    # unbounded await never returns), not on a missing constant.
    monkeypatch.setattr(kw, "_NOTIFIER_COLLECT_TIMEOUT_FLOOR_S", timeout, raising=False)
    monkeypatch.setattr(kw, "_NOTIFIER_COLLECT_TIMEOUT_INTERVALS", 0, raising=False)

    real_sleep = asyncio.sleep
    ticked = 0

    async def fake_sleep(delay):
        nonlocal ticked
        if delay == 5:  # the loop's one-off startup delay
            return None
        ticked += 1
        if ticked >= ticks:
            runner._running = False
        # Yield REAL time so a bounded wait_for inside the loop can actually
        # expire; a bare `sleep(0)` would spin the tick counter to its limit
        # before the timeout ever fires and every contract below would pass
        # without exercising the stall.
        await real_sleep(timeout / 5)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        runner.__class__, "_sleep_between_ticks", lambda self, interval: fake_sleep(interval))

    await asyncio.wait_for(runner._kanban_notifier_watcher(interval=0.01), timeout=30)
    return ticked


@pytest.mark.asyncio
async def test_stuck_collect_does_not_wedge_the_notifier_loop(monkeypatch):
    """(a) The loop keeps ticking while a collect refuses to return.

    Pre-fix this hung forever on the first tick's bare `await to_thread(...)`.
    """
    collect = _StallingCollect()
    runner = _make_runner()
    try:
        ticks = await _drive_notifier(monkeypatch, runner, collect, ticks=4)
    finally:
        collect.released.set()

    assert ticks >= 4, "notifier loop stopped ticking while a collect was stuck"


@pytest.mark.asyncio
async def test_stuck_collect_never_starts_a_second_concurrent_collect(monkeypatch):
    """(b) No thread leak: a stalled collect is never re-submitted alongside itself.

    This is what rules out a bare `asyncio.wait_for`, which would cancel the
    future (without stopping its thread) and submit a fresh one every tick.
    """
    collect = _StallingCollect()
    runner = _make_runner()
    try:
        await _drive_notifier(monkeypatch, runner, collect, ticks=5)
    finally:
        collect.released.set()

    assert collect.max_concurrent == 1, (
        f"{collect.max_concurrent} collects ran concurrently — each stalled collect "
        "occupies a pool thread that cancellation cannot reclaim")
    assert collect.calls == 1, (
        f"stalled collect was re-submitted {collect.calls} times; one spinning thread "
        "per tick exhausts the executor")


@pytest.mark.asyncio
async def test_slow_collect_still_delivers_the_events_it_claimed(monkeypatch):
    """(c) A collect that outruns the bound is not abandoned.

    `claim_unseen_events_for_sub` advances the subscription cursor BEFORE
    delivery, so dropping a slow collect's result silently loses notifications
    that can never be re-claimed.
    """
    delivered = []
    delivery = {"sub": {"task_id": "t_slow"}, "events": ["e1"], "board": "b"}
    collect = _StallingCollect(release_value=[delivery])
    runner = _make_runner()

    from gateway import kanban_watchers as kw

    class _CapturingNotification:
        def __init__(self, runner, d, **kwargs):
            self._d = d

        async def deliver(self):
            delivered.append(self._d)

    monkeypatch.setattr(kw, "_KanbanNotification", _CapturingNotification)

    # Release only AFTER the loop has demonstrably given up waiting on this
    # collect at least once. Gating on the loop's own timeout log (rather than a
    # wall-clock sleep) is what makes the contract non-vacuous: the collect is
    # guaranteed to have outrun the bound before it returns its claimed events.
    timed_out = asyncio.Event()
    real_warning = kw.logger.warning

    def _watch_warning(msg, *args, **kwargs):
        if "has been running" in str(msg):
            runner._loop_ref.call_soon_threadsafe(timed_out.set)
        return real_warning(msg, *args, **kwargs)

    runner._loop_ref = asyncio.get_running_loop()
    monkeypatch.setattr(kw.logger, "warning", _watch_warning)

    async def _release_once_the_loop_gave_up():
        await asyncio.wait_for(timed_out.wait(), timeout=20)
        collect.released.set()

    releaser = asyncio.create_task(_release_once_the_loop_gave_up())
    try:
        await _drive_notifier(monkeypatch, runner, collect, ticks=60)
    finally:
        collect.released.set()
        releaser.cancel()

    assert timed_out.is_set(), (
        "the collect never outran the bound — this contract did not exercise a "
        "slow collect at all and proves nothing")
    assert delivered == [delivery], (
        "events claimed by the slow collect were dropped — the cursor already "
        "advanced past them, so they are unrecoverable")


@pytest.mark.asyncio
async def test_dispatcher_ticks_while_the_notifier_collect_is_stuck(monkeypatch):
    """(d) The dispatcher loop makes progress with a notifier collect wedged.

    The two loops share the event loop and the default executor, so this pins
    that neither the notifier's await nor its occupied worker can stall
    dispatch.
    """
    from gateway import kanban_watchers as kw

    collect = _StallingCollect()
    runner = _make_runner()
    collect._loop = asyncio.get_running_loop()
    monkeypatch.setattr(kw, "_notifier_collect", collect)
    monkeypatch.setattr(kw, "_NOTIFIER_COLLECT_TIMEOUT_FLOOR_S", 0.05, raising=False)
    monkeypatch.setattr(kw, "_NOTIFIER_COLLECT_TIMEOUT_INTERVALS", 0, raising=False)

    dispatch_ticks = 0

    async def _fake_dispatcher():
        nonlocal dispatch_ticks
        while runner._running:
            # Mirrors the real loop: blocking work offloaded to the same executor.
            await asyncio.to_thread(lambda: None)
            dispatch_ticks += 1
            await asyncio.sleep(0)

    real_sleep = asyncio.sleep
    ticked = 0

    async def fake_sleep(delay):
        nonlocal ticked
        if delay == 5:
            return None
        ticked += 1
        if ticked >= 5:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(
        runner.__class__, "_sleep_between_ticks", lambda self, interval: fake_sleep(interval))
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    dispatcher = asyncio.create_task(_fake_dispatcher())
    try:
        await asyncio.wait_for(runner._kanban_notifier_watcher(interval=0.01), timeout=10)
    finally:
        collect.released.set()
        runner._running = False
        await dispatcher

    assert dispatch_ticks > 0, "dispatcher made zero progress while the notifier was stuck"
