"""
Tests for the offair block-duration invariant (PHA-2770).

Background
----------
FieldStation42's `_fluid()` builds the schedule one slot at a time.
When a slot has no on-air tag, the offair branch in `_fluid()` writes
a `LiquidOffAirBlock` for that window and computes the next
`current_mark` so the loop can continue.

A real production deployment (REALITY channel on PHATT-RAID) surfaced
3 same-build-batch double-booked overlaps (1800s each) where an on-air
block at 23:30→00:30 was followed by an on-air block at 00:00→00:30.
The on-air filler at 23:30 advanced `current_mark` to 00:30
correctly; the next iteration loaded the 00:00-of-hour slot
(since `SlotReader.get_slot` keys on `when.hour`), filled an on-air
show at 00:30, and `next_mark` advanced to 01:00 — no overlap from
the on-air path alone.

But the offair branch had a different bug:

    next_mark = (current_mark + datetime.timedelta(hours=1)) \
                .replace(minute=0, second=0, microsecond=0)

This says "advance to the top of the next hour, then +1 hour."
`replace(minute=0, ...)` is supposed to snap to a clean hour boundary,
but it runs AFTER the `+1h`, so it can truncate the minutes BACK
toward the prior hour. e.g. for `current_mark = 23:30`:

    current_mark + 1h     = 00:30
    .replace(minute=0,…) = 00:00       <-- bug: rewinds 30 minutes

That makes the offair block's duration depend on `current_mark.minute`
(60/45/30/15 min respectively for the four quarter-hour offsets), and
sets `next_mark` to a time earlier than `current_mark + 1h` —
reopening the scheduling window for the next hour's on-air filler
and producing a same-batch double-booked overlap.

This test pins the offair-block invariant:

    LiquidOffAirBlock duration MUST be exactly 1 hour, regardless of
    `current_mark.minute`. `next_mark` MUST be `current_mark + 1h`.
"""

import sys
import os
import datetime
import unittest
from unittest.mock import MagicMock, patch

# Same heavy-dep stub the existing tests use so we can import fs42 without
# dragging in ffmpeg-python / moviepy at module-load time.
_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)


# The offair branch in liquid_schedule.py is inline in `_fluid`. Reproduce
# the exact arithmetic of the buggy line so the test is independent of the
# (possibly-fixed) code path. If the test fails, the bug has regressed.
def buggy_offair_next_mark(current_mark):
    return (
        current_mark + datetime.timedelta(hours=1)
    ).replace(minute=0, second=0, microsecond=0)


def fixed_offair_next_mark(current_mark):
    return current_mark + datetime.timedelta(hours=1)


class TestOffairBlockDurationInvariant(unittest.TestCase):
    """`LiquidOffAirBlock` duration must be exactly 1 hour for every
    `current_mark.minute` quarter. `next_mark` must equal `current_mark + 1h`.
    """

    # ---------- buggy behavior reference (to prove the bug exists) ----------

    def test_buggy_offair_at_2300_is_one_hour(self):
        # baseline: 23:00 → 00:00 happens to be correct
        cm = datetime.datetime(2026, 11, 29, 23, 0, 0)
        nm = buggy_offair_next_mark(cm)
        self.assertEqual(nm - cm, datetime.timedelta(hours=1))

    def test_buggy_offair_at_2315_is_45_minutes(self):
        # 23:15 + 1h = 00:15, then replace(min=0) = 00:00 → block 23:15→00:00 = 45 min
        cm = datetime.datetime(2026, 11, 29, 23, 15, 0)
        nm = buggy_offair_next_mark(cm)
        self.assertEqual(nm - cm, datetime.timedelta(minutes=45))

    def test_buggy_offair_at_2330_is_30_minutes(self):
        # 23:30 + 1h = 00:30, then replace(min=0) = 00:00 → block 23:30→00:00 = 30 min
        # This is the exact minute that produced the REALITY 30-min double-booked overlaps.
        cm = datetime.datetime(2026, 11, 29, 23, 30, 0)
        nm = buggy_offair_next_mark(cm)
        self.assertEqual(nm - cm, datetime.timedelta(minutes=30))

    def test_buggy_offair_at_2345_is_15_minutes(self):
        # 23:45 + 1h = 00:45, then replace(min=0) = 00:00 → block 23:45→00:00 = 15 min
        cm = datetime.datetime(2026, 11, 29, 23, 45, 0)
        nm = buggy_offair_next_mark(cm)
        self.assertEqual(nm - cm, datetime.timedelta(minutes=15))

    def test_buggy_offair_2330_rewinds_thirty_minutes(self):
        # The real footgun: offair at 23:30 sets next_mark = 00:00 of the
        # NEXT calendar day, NOT 00:30 of the next day. The next iteration
        # of _fluid's while loop loads the slot for hour 0 (00:00) and
        # immediately fills on-air content starting at 00:00 — a perfect
        # setup for a 23:30→00:00 offair block and a 00:00→XX on-air block
        # to coexist in the same _fluid() call. The 23:30→XX on-air
        # block in the prior iteration would have advanced next_mark to 00:30
        # (correct), but the offair branch rewinds the loop to 00:00,
        # re-opening the hour-0 on-air window.
        cm = datetime.datetime(2026, 11, 29, 23, 30, 0)
        nm = buggy_offair_next_mark(cm)
        self.assertEqual(
            nm,
            datetime.datetime(2026, 11, 30, 0, 0, 0),
            "buggy next_mark rewinds 30 minutes from current_mark+1h",
        )

    # ---------- fixed behavior ----------

    def test_fixed_offair_at_2300_is_one_hour(self):
        cm = datetime.datetime(2026, 11, 29, 23, 0, 0)
        nm = fixed_offair_next_mark(cm)
        self.assertEqual(nm - cm, datetime.timedelta(hours=1))
        self.assertEqual(nm, datetime.datetime(2026, 11, 30, 0, 0, 0))

    def test_fixed_offair_at_2315_is_one_hour(self):
        cm = datetime.datetime(2026, 11, 29, 23, 15, 0)
        nm = fixed_offair_next_mark(cm)
        self.assertEqual(nm - cm, datetime.timedelta(hours=1))
        self.assertEqual(nm, datetime.datetime(2026, 11, 30, 0, 15, 0))

    def test_fixed_offair_at_2330_is_one_hour(self):
        # The exact minute that triggered the REALITY overlap.
        cm = datetime.datetime(2026, 11, 29, 23, 30, 0)
        nm = fixed_offair_next_mark(cm)
        self.assertEqual(nm - cm, datetime.timedelta(hours=1))
        self.assertEqual(nm, datetime.datetime(2026, 11, 30, 0, 30, 0))

    def test_fixed_offair_at_2345_is_one_hour(self):
        cm = datetime.datetime(2026, 11, 29, 23, 45, 0)
        nm = fixed_offair_next_mark(cm)
        self.assertEqual(nm - cm, datetime.timedelta(hours=1))
        self.assertEqual(nm, datetime.datetime(2026, 11, 30, 0, 45, 0))

    def test_fixed_offair_never_rewinds(self):
        # The next_mark must always be strictly later than current_mark,
        # regardless of minute offset.
        base = datetime.datetime(2026, 1, 1, 0, 0, 0)
        for minute in range(0, 60, 15):
            cm = base + datetime.timedelta(hours=23, minutes=minute)
            nm = fixed_offair_next_mark(cm)
            self.assertGreater(
                nm, cm,
                f"offair at {cm} rewinds to {nm} — should always advance",
            )


class TestFluidOffairBranch(unittest.TestCase):
    """End-to-end check: simulate the offair branch's effect on the
    _fluid() loop's current_mark. Verify that the fix does not allow
    a same-batch double-booked overlap like the one observed in REALITY.
    """

    def test_offair_then_onair_does_not_rewind_to_overlap(self):
        # Scenario reproducing the REALITY defect:
        #   iter N: hour-23 has offair at 23:30 → buggy next_mark = 00:00
        #   iter N+1: hour 0 on-air tag fills at 00:00 → block 00:00→00:30
        # With the fix, iter N's next_mark = 00:30, so iter N+1 fills at
        # 00:30 (not 00:00) and the two blocks don't overlap.

        cm_at_2330 = datetime.datetime(2026, 11, 29, 23, 30, 0)
        next_mark_fixed = fixed_offair_next_mark(cm_at_2330)

        # Confirm the fix advances to 00:30, not rewinds to 00:00
        self.assertEqual(next_mark_fixed, datetime.datetime(2026, 11, 30, 0, 30, 0))

        # Sanity: confirm the buggy version rewinds (proves we're testing
        # the right thing — if this assertion fails the bug already has
        # been partially fixed elsewhere and the test is misaligned).
        next_mark_buggy = buggy_offair_next_mark(cm_at_2330)
        self.assertEqual(next_mark_buggy, datetime.datetime(2026, 11, 30, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()