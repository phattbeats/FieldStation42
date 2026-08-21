"""Wall-clock ticker for ``LiveScheduleAgent``.

Background
----------
FieldStation42's field player runs ``LiveScheduleAgent.tick()`` from
inside its blocking ``while True`` main loop, which only re-enters
when a channel-change completes. An unattended appliance parked on
any non-changing channel would get exactly one tick (at process
start) and then never another — silently failing to rebuild
schedules that ran out (see PHA-2263).

This module provides ``schedule_agent_ticker`` — a daemon-thread
driver that fires ``tick()`` on a real wall-clock cadence so the
agent runs unattended. It is intentionally tiny and side-effect-free
so it can be unit-tested in isolation without the FS42 dependency
graph.
"""

import logging
import threading


# How often the background thread calls LiveScheduleAgent.tick(). The
# agent itself enforces a 1-hour cooldown internally; this interval is
# purely the wall-clock cadence at which we ask. 60s gives the agent a
# chance to run ~hourly with low overhead, and shortens the worst-case
# lag between a channel going silent (e.g. playback crashed) and the
# schedule rebuild kicking in. Keep in sync with
# LiveScheduleAgent._check_interval.
DEFAULT_TICK_SECONDS = 60


def schedule_agent_ticker(schedule_agent, stop_event, tick_seconds=DEFAULT_TICK_SECONDS):
    """Background tick driver for ``schedule_agent``.

    Fires one tick immediately at start (so the first run does not
    have to wait the full interval), then every ``tick_seconds``
    until ``stop_event`` is set. Errors from ``tick()`` are logged and
    swallowed — a single failed tick must not kill the loop,
    otherwise we are back where we started.

    Parameters
    ----------
    schedule_agent : object
        Anything with a ``tick()`` method (in production, a
        ``LiveScheduleAgent`` instance).
    stop_event : threading.Event
        The caller controls shutdown by ``.set()``-ing this event.
    tick_seconds : int or float, optional
        Wall-clock interval between ticks. Default 60s.

    Returns
    -------
    None
        The function is intended to be the target of a
        ``threading.Thread``. It does not return until the stop event
        is set.
    """
    _l = logging.getLogger("ScheduleAgent.Ticker")
    _l.info(f"Schedule agent ticker started (interval={tick_seconds}s)")
    # Fire one tick immediately so the first run does not have to wait
    # the full interval for the startup-time check.
    try:
        schedule_agent.tick()
    except Exception as e:
        _l.exception(f"Initial schedule agent tick raised: {e}")
    while not stop_event.is_set():
        # Sleep in short slices via stop_event.wait() so SIGTERM-driven
        # stop_event.set() takes effect quickly even between long waits.
        if stop_event.wait(timeout=tick_seconds):
            break
        try:
            schedule_agent.tick()
        except Exception as e:
            _l.exception(f"Schedule agent tick raised: {e}")
    _l.info("Schedule agent ticker stopped")
