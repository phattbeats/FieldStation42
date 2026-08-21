"""Tests for PHA-2263 — schedule agent must tick on a wall-clock timer.

Before the fix, ``LiveScheduleAgent.tick()`` was only called from inside
the field player's ``while True`` main loop, which blocks inside
``show_guide()`` / ``play_slot()`` until a channel change. An unattended
appliance parked on any non-changing channel would get exactly one tick
(at process start) and then never again. This suite verifies:

* ``schedule_agent_ticker`` ticks on a wall-clock interval.
* The loop respects a stop event (no busy-wait, no leak).
* Tick errors do not kill the loop.
* ``LiveScheduleAgent.tick()`` is safe to call from multiple threads
  (the dedicated ``_tick_lock`` prevents double-spawn under concurrent
  callers).
"""

import threading
import time
import pytest

from fs42.schedule_agent_ticker import schedule_agent_ticker, DEFAULT_TICK_SECONDS
from fs42.live_schedule_agent import LiveScheduleAgent


class _FakeAgent:
    """Counts tick() calls and the timestamp of each. No FS42 imports."""

    def __init__(self):
        self.tick_count = 0
        self.ticks = []
        self.lock = threading.Lock()
        self.raise_on_tick = None

    def tick(self):
        with self.lock:
            self.tick_count += 1
            self.ticks.append(time.monotonic())
            if self.raise_on_tick is not None:
                exc = self.raise_on_tick
                self.raise_on_tick = None
                raise exc


def test_ticker_fires_initial_tick_immediately():
    """One tick fires immediately at start."""
    agent = _FakeAgent()
    stop = threading.Event()

    thread = threading.Thread(
        target=schedule_agent_ticker, args=(agent, stop), daemon=True
    )
    thread.start()
    try:
        # The initial tick should have fired before we even get here.
        deadline = time.monotonic() + 1.0
        while agent.tick_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert agent.tick_count >= 1, "expected initial tick to fire immediately"
    finally:
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive(), "ticker thread did not exit cleanly"


def test_ticker_fires_on_interval():
    """After ~3 intervals at 50ms cadence we get at least 2 extra ticks."""
    agent = _FakeAgent()
    stop = threading.Event()

    thread = threading.Thread(
        target=schedule_agent_ticker,
        args=(agent, stop),
        kwargs={"tick_seconds": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        # Wait for the initial tick.
        deadline = time.monotonic() + 1.0
        while agent.tick_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        baseline = agent.tick_count
        time.sleep(0.2)
        assert agent.tick_count >= baseline + 2, (
            f"expected at least 2 additional ticks over 0.2s at 0.05s "
            f"interval, got {agent.tick_count - baseline}"
        )
    finally:
        stop.set()
        thread.join(timeout=2)


def test_ticker_swallows_tick_errors():
    """A single tick raising must not kill the loop."""
    agent = _FakeAgent()
    agent.raise_on_tick = RuntimeError("simulated worker crash")
    stop = threading.Event()

    thread = threading.Thread(
        target=schedule_agent_ticker,
        args=(agent, stop),
        kwargs={"tick_seconds": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        # The first tick (initial) will raise. The loop should survive
        # and fire subsequent ticks. Wait long enough for >= 2 ticks
        # after the initial one.
        deadline = time.monotonic() + 2.0
        while agent.tick_count < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert agent.tick_count >= 3, (
            f"loop died after first tick raised; expected at least 3 ticks "
            f"but only saw {agent.tick_count}"
        )
    finally:
        stop.set()
        thread.join(timeout=2)


def test_ticker_stops_quickly():
    """stop_event.set() must unblock the loop within ~1 interval."""
    agent = _FakeAgent()
    stop = threading.Event()

    thread = threading.Thread(
        target=schedule_agent_ticker,
        args=(agent, stop),
        kwargs={"tick_seconds": 5.0},
        daemon=True,
    )
    thread.start()
    # Wait for the initial tick.
    time.sleep(0.05)

    t0 = time.monotonic()
    stop.set()
    thread.join(timeout=2)
    elapsed = time.monotonic() - t0

    assert not thread.is_alive(), "stop_event.set() did not unblock the loop"
    assert elapsed < 2.0, f"stop took {elapsed:.2f}s, expected < 2.0s"


def test_default_tick_seconds_is_60():
    """The default cadence is 60s — caught here so a refactor can't silently regress it."""
    assert DEFAULT_TICK_SECONDS == 60


def test_live_schedule_agent_tick_is_thread_safe():
    """Two concurrent tick() callers cannot both enter the unlocked body.

    We monkey-patch ``_tick_unlocked`` and assert the second caller
    blocks until the first releases the lock.
    """
    conf = {"amount_to_add": "day", "trigger_add_at": "day"}
    agent = LiveScheduleAgent(conf, lock=None)

    enter_log = []
    held = threading.Event()
    release = threading.Event()

    def fake_unlocked():
        enter_log.append(threading.current_thread().name)
        if len(enter_log) == 1:
            held.set()
            # Block while holding the lock so the second caller must wait.
            assert release.wait(timeout=2), "second caller never entered"
        return False

    agent._tick_unlocked = fake_unlocked

    results = {}

    def caller(name):
        results[name] = agent.tick()

    t1 = threading.Thread(target=caller, args=("t1",), name="t1", daemon=True)
    t1.start()
    assert held.wait(timeout=2), "first caller did not enter _tick_unlocked"

    t2 = threading.Thread(target=caller, args=("t2",), name="t2", daemon=True)
    t2.start()
    # Give t2 time to start and BLOCK on the lock (it should NOT be in
    # enter_log yet).
    time.sleep(0.2)
    assert len(enter_log) == 1, (
        f"second caller entered _tick_unlocked while first held the lock; "
        f"enter_log={enter_log}"
    )

    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert not t1.is_alive() and not t2.is_alive()
    assert enter_log == ["t1", "t2"], (
        f"expected serialised entry [t1, t2], got {enter_log}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
