import os
import tempfile
import unittest
from unittest.mock import patch

from fs42.sequence_io import SequenceIO
from fs42.sequence_api import SequenceAPI
from fs42.sequence import NamedSequence


class TestSequenceGroupIndexPreserved(unittest.TestCase):
    """
    Regression test for PHA-3588 (Finding 2 of PHA-3510): the group hand-off
    in get_next_in_sequence used to unconditionally zero the incoming child's
    current_index, discarding real progress for a child that lost active
    status before finishing its own cycle (e.g. via
    _get_active_child_sequence's stale-active-pointer fallback, which resumes
    a child without touching current_index).

    A child mid-window (0 <= current_index < end_index) must resume from
    where it left off. A child that has actually finished its window
    (current_index >= end_index) must still reset to start_index, exactly
    like before the fix -- this half is the case
    test_sequence_group_never_played_first.py and the existing hand-off
    tests already depend on.
    """

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        patcher = patch("fs42.sequence_io.StationManager")
        mock_station_manager = patcher.start()
        mock_station_manager.return_value.server_conf = {"db_path": self.tmp_db.name}
        self.addCleanup(patcher.stop)

        self.station_name = "TESTSTATION"
        self.sequence_name = "grp_seq"
        self.parent_tag = "grp"
        self.sio = SequenceIO()

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def _put_child(self, name, current_index):
        tag = f"{self.parent_tag}/{name}"
        ns = NamedSequence(
            self.station_name,
            self.sequence_name,
            tag,
            0.0,
            1.0,
            current_index,
            [f"{tag}/ep{i}.mkv" for i in range(100)],
            True,
        )
        self.sio.put_sequence(self.station_name, ns)
        return tag

    def test_resumable_progress_is_preserved_on_handoff(self):
        # Active child "A" has already reached its own end_index, so this
        # call must trigger the group hand-off immediately.
        active_tag = self._put_child("A", 100)
        # Target child "B" was left mid-window at index 50 by an earlier
        # activation that ended via the stale-pointer fallback, not by
        # finishing its own cycle.
        target_tag = self._put_child("B", 50)

        self.sio.set_active_sequence(
            self.station_name, self.sequence_name, self.parent_tag, active_tag
        )

        station_config = {"network_name": self.station_name}
        entry = SequenceAPI.get_next_in_sequence(
            station_config, self.sequence_name, self.parent_tag
        )

        self.assertIsNotNone(entry)
        self.assertTrue(entry.fpath.startswith(target_tag + "/ep50"))

        resumed = self.sio.get_sequence(self.station_name, self.sequence_name, target_tag)
        self.assertEqual(
            resumed.current_index,
            51,
            "resumable progress (index 50) must be preserved and advanced by "
            "exactly one, not discarded back to 0",
        )

    def test_finished_child_still_resets_to_start_on_handoff(self):
        active_tag = self._put_child("A", 100)
        # Target child "C" already fully completed its own window last time
        # it was active (current_index == end_index == 100).
        target_tag = self._put_child("C", 100)

        self.sio.set_active_sequence(
            self.station_name, self.sequence_name, self.parent_tag, active_tag
        )

        station_config = {"network_name": self.station_name}
        entry = SequenceAPI.get_next_in_sequence(
            station_config, self.sequence_name, self.parent_tag
        )

        self.assertIsNotNone(entry)
        self.assertTrue(
            entry.fpath.startswith(target_tag + "/ep0"),
            "a child that already finished its window must still restart at "
            "start_index, exactly as before the fix",
        )

        resumed = self.sio.get_sequence(self.station_name, self.sequence_name, target_tag)
        self.assertEqual(resumed.current_index, 1)


if __name__ == "__main__":
    unittest.main()
