import os
import tempfile
import unittest
from unittest.mock import patch

from fs42.sequence_io import SequenceIO
from fs42.sequence_api import SequenceAPI
from fs42.sequence import NamedSequence


class TestSequenceGroupNeverPlayedFirst(unittest.TestCase):
    """
    Regression test for PHA-2951 (Problem B): random.choice() over a group's
    children is memoryless, so with enough members it's entirely possible
    for one (e.g. American Dad! in Prime Time 3's "sitcoms" group, Space
    Ghost Coast to Coast in [swim]'s "adult_swim" group) to go many
    rotations without ever being picked, showing up as zero scheduled
    blocks even though nothing is actually broken.

    _choose_next_child_sequence must prefer a child that has never had a
    turn (current_index == 0) over children that have already played at
    least one episode, so every group member gets a first turn before any
    member gets a second one.
    """

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        patcher = patch("fs42.sequence_io.StationManager")
        mock_station_manager = patcher.start()
        mock_station_manager.return_value.server_conf = {"db_path": self.tmp_db.name}
        self.addCleanup(patcher.stop)

        self.station_name = "TESTSTATION"
        self.sequence_name = "sitcoms_seq"
        self.parent_tag = "sitcoms"

        self.sio = SequenceIO()

        # Two children already rotated through (nonzero current_index), one
        # never played yet -- mirrors the real Prime Time 3 state where
        # Scrubs/That's 70s Show had progress but American Dad! sat at 0.
        self.played_a = f"{self.parent_tag}/Played Show A"
        self.played_b = f"{self.parent_tag}/Played Show B"
        self.never_played = f"{self.parent_tag}/Never Played Show"

        for tag, index in [(self.played_a, 50), (self.played_b, 80), (self.never_played, 0)]:
            ns = NamedSequence(
                self.station_name,
                self.sequence_name,
                tag,
                0.0,
                1.0,
                index,
                [f"{tag}/ep{i}.mkv" for i in range(100)],
                True,
            )
            self.sio.put_sequence(self.station_name, ns)

        self.sio.set_active_sequence(
            self.station_name, self.sequence_name, self.parent_tag, self.played_a
        )

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def test_never_played_child_is_chosen_over_already_rotated_children(self):
        station_config = {"network_name": self.station_name}

        for _ in range(20):
            chosen = SequenceAPI._choose_next_child_sequence(
                station_config,
                self.sequence_name,
                self.parent_tag,
                current_tag_path=self.played_a,
            )
            self.assertEqual(
                chosen,
                self.never_played,
                "the never-played child must be preferred over children "
                "that have already had at least one turn",
            )


if __name__ == "__main__":
    unittest.main()
