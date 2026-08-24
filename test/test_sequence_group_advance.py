import os
import tempfile
import unittest
from unittest.mock import patch

from fs42.sequence_io import SequenceIO
from fs42.sequence_api import SequenceAPI
from fs42.sequence import NamedSequence


class TestSequenceGroupAdvance(unittest.TestCase):
    """
    Regression test for PHA-2561: a nested (two-segment) group tag_path,
    e.g. "midnight_run/Cowboy Bebop/Specials", must resolve back to the
    true group parent_tag ("midnight_run") when its sequence completes,
    not get stuck deriving a bogus intermediate parent ("midnight_run/Cowboy Bebop")
    via naive rsplit("/", 1) on the tag_path.
    """

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        patcher = patch(
            "fs42.sequence_io.StationManager",
        )
        mock_station_manager = patcher.start()
        mock_station_manager.return_value.server_conf = {"db_path": self.tmp_db.name}
        self.addCleanup(patcher.stop)

        self.station_config = {"network_name": "TOONAMI"}
        self.sequence_name = "midnight_run_marathon"

        self.sio = SequenceIO()

        self.groups = [
            "midnight_run/Cowboy Bebop",
            "midnight_run/Cowboy Bebop/Specials",
            "midnight_run/Neon Genesis Evangelion",
            "midnight_run/Neon Genesis Evangelion/Specials",
        ]

        for group_tag in self.groups:
            ns = NamedSequence(
                "TOONAMI",
                self.sequence_name,
                group_tag,
                0,
                1,
                0,
                [f"{group_tag}/ep0.mp4", f"{group_tag}/ep1.mp4"],
                True,
            )
            self.sio.put_sequence("TOONAMI", ns)

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def test_advances_past_nested_group_to_sibling(self):
        # Simulate the state reported in PHA-2561: the marathon is parked on the
        # nested "Cowboy Bebop/Specials" group, one episode away from completing it.
        self.sio.set_active_sequence(
            "TOONAMI", self.sequence_name, "midnight_run", "midnight_run/Cowboy Bebop/Specials"
        )
        self.sio.update_current_index(
            "TOONAMI", self.sequence_name, "midnight_run/Cowboy Bebop/Specials", 1
        )

        seen_tags = set()
        for _ in range(20):
            entry = SequenceAPI.get_next_in_sequence(
                self.station_config, self.sequence_name, "midnight_run"
            )
            self.assertIsNotNone(entry)
            for group_tag in self.groups:
                if entry.fpath.startswith(group_tag + "/"):
                    seen_tags.add(group_tag)
                    break

        self.assertIn(
            "midnight_run/Neon Genesis Evangelion",
            seen_tags,
            "sequence never advanced past the nested Cowboy Bebop groups to NGE",
        )

        # The group parent_tag key must stay "midnight_run", never regress to a
        # bogus nested parent like "midnight_run/Cowboy Bebop".
        active_parents = set(self.sio.get_all_active_sequences("TOONAMI"))
        self.assertTrue(active_parents.issubset(set(self.groups)))


if __name__ == "__main__":
    unittest.main()
