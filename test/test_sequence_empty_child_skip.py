import os
import tempfile
import unittest
from unittest.mock import patch

from fs42.sequence_io import SequenceIO
from fs42.sequence_api import SequenceAPI
from fs42.sequence import NamedSequence


class TestSequenceEmptyChildSkip(unittest.TestCase):
    """
    Regression test for PHA-3417: one empty child named_sequence must not break
    wraparound for its whole group.

    Found in the field: two named_sequence rows held zero sequence_entries and
    pointed at directories that no longer existed (one show folder renamed, one
    tag that never had a counterpart on disk). When any sibling under the same
    parent tag reached the end of its pool, the group hand-off could pick the
    empty child, log "contains no episodes", and get_next_in_sequence returned
    None -- so the slot silently dropped for EVERY show in the group, not just
    the empty one. Measured: 7 of 78 sequences failed to wrap.
    """

    PARENT = "comedy"
    EMPTY_CHILD = "comedy/Renamed Show"

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        patcher = patch("fs42.sequence_io.StationManager")
        mock_station_manager = patcher.start()
        mock_station_manager.return_value.server_conf = {"db_path": self.tmp_db.name}
        self.addCleanup(patcher.stop)

        self.content_dir = tempfile.mkdtemp()
        self.addCleanup(self._rm_content_dir)

        self.station_config = {
            "network_name": "GRIDSTATION",
            "content_dir": self.content_dir,
        }
        self.sequence_name = "prime_time_grid"

        self.sio = SequenceIO()

        self.populated = [
            "comedy/Show A",
            "comedy/Show B",
            "comedy/Show C",
        ]

        for tag in self.populated:
            os.makedirs(os.path.join(self.content_dir, tag), exist_ok=True)
            self.sio.put_sequence(
                "GRIDSTATION",
                NamedSequence(
                    "GRIDSTATION",
                    self.sequence_name,
                    tag,
                    0,
                    1,
                    0,
                    [f"{tag}/ep0.mp4", f"{tag}/ep1.mp4"],
                    True,
                ),
            )

        # the poisoned row: zero entries, and no directory on disk
        self.sio.put_sequence(
            "GRIDSTATION",
            NamedSequence(
                "GRIDSTATION",
                self.sequence_name,
                self.EMPTY_CHILD,
                0,
                1,
                0,
                [],
                True,
            ),
        )

    def _rm_content_dir(self):
        for root, dirs, files in os.walk(self.content_dir, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.content_dir)

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def _park_at_end(self, tag):
        self.sio.set_active_sequence(
            "GRIDSTATION", self.sequence_name, self.PARENT, tag
        )
        self.sio.update_current_index("GRIDSTATION", self.sequence_name, tag, 2)

    def test_every_populated_sibling_wraps_with_an_empty_child_present(self):
        for tag in self.populated:
            with self.subTest(tag=tag):
                self._park_at_end(tag)

                entry = SequenceAPI.get_next_in_sequence(
                    self.station_config, self.sequence_name, self.PARENT
                )

                self.assertIsNotNone(
                    entry,
                    f"{tag} handed back nothing on wraparound -- the empty "
                    f"child {self.EMPTY_CHILD} poisoned the group hand-off",
                )

    def test_empty_child_is_never_chosen_by_the_group_handoff(self):
        for _ in range(60):
            chosen = SequenceAPI._choose_next_child_sequence(
                self.station_config,
                self.sequence_name,
                self.PARENT,
                "comedy/Show A",
            )
            self.assertNotEqual(chosen, self.EMPTY_CHILD)

    def test_empty_child_is_never_chosen_as_the_initial_active_child(self):
        for _ in range(60):
            # an active pointer that is not a child of the group forces the
            # re-pick branch, which is where the empty child used to be
            # reachable
            self.sio.set_active_sequence(
                "GRIDSTATION", self.sequence_name, self.PARENT, "comedy/Gone"
            )
            chosen = SequenceAPI._get_active_child_sequence(
                self.station_config, self.sequence_name, self.PARENT
            )
            self.assertNotEqual(chosen, self.EMPTY_CHILD)

    def test_only_child_is_still_returned_even_when_empty(self):
        """
        The skip is a preference, not a hard filter: a group whose only child
        is empty must still round-trip through the existing "allow it" fallback
        rather than crash or return None from _choose_next_child_sequence.
        """
        for tag in self.populated:
            self.sio.delete_sequence("GRIDSTATION", self.sequence_name, tag)

        chosen = SequenceAPI._choose_next_child_sequence(
            self.station_config, self.sequence_name, self.PARENT
        )

        self.assertEqual(chosen, self.EMPTY_CHILD)

    def test_scan_prunes_the_orphaned_empty_row(self):
        SequenceAPI._prune_orphaned_empty_sequences(self.station_config)

        remaining = [
            seq.tag_path
            for seq in self.sio.get_all_sequences_for_station("GRIDSTATION")
        ]

        self.assertNotIn(self.EMPTY_CHILD, remaining)
        for tag in self.populated:
            self.assertIn(tag, remaining)

    def test_scan_keeps_an_empty_row_whose_directory_still_exists(self):
        """
        A pool emptied by a bad transfer or an unmounted drive is transient
        state, not a stale row. Deleting it would throw away the show's cursor.
        """
        os.makedirs(os.path.join(self.content_dir, self.EMPTY_CHILD))

        SequenceAPI._prune_orphaned_empty_sequences(self.station_config)

        remaining = [
            seq.tag_path
            for seq in self.sio.get_all_sequences_for_station("GRIDSTATION")
        ]

        self.assertIn(self.EMPTY_CHILD, remaining)

    def test_prune_keeps_populated_rows_when_their_directory_is_missing(self):
        """
        Positive fixture for the other half of the AND: a row with entries
        whose directory is absent (drive not mounted mid-scan) must survive.
        """
        for tag in self.populated:
            os.rmdir(os.path.join(self.content_dir, tag))

        SequenceAPI._prune_orphaned_empty_sequences(self.station_config)

        remaining = [
            seq.tag_path
            for seq in self.sio.get_all_sequences_for_station("GRIDSTATION")
        ]

        for tag in self.populated:
            self.assertIn(tag, remaining)


if __name__ == "__main__":
    unittest.main()
