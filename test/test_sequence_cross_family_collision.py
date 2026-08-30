import os
import tempfile
import unittest
from unittest.mock import patch

from fs42.sequence_io import SequenceIO
from fs42.sequence_api import SequenceAPI
from fs42.sequence import NamedSequence


class TestSequenceCrossFamilyCollision(unittest.TestCase):
    """
    Regression test for PHA-2833: two independent named_sequence families
    (e.g. "toonami_anime_daytime" and "toonami_anime_primetime") that both
    build their child tag_paths from the same base content tag ("anime")
    end up with literally identical child tag_path strings ("anime/Naruto").

    _choose_next_child_sequence excluded candidates using a station-wide
    (not sequence_name-scoped) active-sequence lookup, so once the primetime
    family marked "anime/Naruto" active, the daytime family's own round-robin
    would see it as already active too -- even though it belongs to a
    completely different sequence_name -- and could exhaust every real
    candidate down to a false "only option" fallback.
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
        self.sio = SequenceIO()

        # Two unrelated families sharing the same base tag "anime", each with
        # the same two shows as children -- mirrors two dayparts scanning the
        # same content_dir tag.
        self.shows = ["anime/Naruto", "anime/Bebop"]
        for seq_name in ("toonami_anime_daytime", "toonami_anime_primetime"):
            for child_tag in self.shows:
                ns = NamedSequence(
                    "TOONAMI",
                    seq_name,
                    child_tag,
                    0,
                    1,
                    0,
                    [f"{child_tag}/ep0.mp4"],
                    True,
                )
                self.sio.put_sequence("TOONAMI", ns)

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def test_unrelated_family_active_pick_does_not_starve_sibling_family(self):
        # primetime family locks onto "anime/Naruto"
        self.sio.set_active_sequence(
            "TOONAMI", "toonami_anime_primetime", "anime", "anime/Naruto"
        )

        # daytime family is completing its "anime/Bebop" run and must be able
        # to pick "anime/Naruto" as its next child -- it's a distinct
        # (sequence_name, tag_path) pair in daytime's own namespace, unrelated
        # to primetime's choice.
        next_child = SequenceAPI._choose_next_child_sequence(
            self.station_config,
            "toonami_anime_daytime",
            "anime",
            current_tag_path="anime/Bebop",
        )

        self.assertEqual(
            next_child,
            "anime/Naruto",
            "daytime family's candidate was wrongly excluded by an unrelated "
            "family's active-sequence state",
        )


if __name__ == "__main__":
    unittest.main()
