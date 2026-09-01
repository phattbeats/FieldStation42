import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out heavy / native deps BEFORE any fs42 import, same pattern as
# test_exclusion.py, so importing fs42.media_processor doesn't abort.
# ---------------------------------------------------------------------------
_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.catalog_entry import CatalogEntry          # noqa: E402
from fs42.liquid_schedule import LiquidSchedule       # noqa: E402
from fs42.sequence_io import SequenceIO               # noqa: E402
from fs42.sequence import NamedSequence               # noqa: E402


class TestSequenceOrphanEntrySkip(unittest.TestCase):
    """
    Regression test for PHA-2951 (Problem A): a named sequence's on-disk
    episode list is built by scanning the filesystem directly
    (MediaProcessor._rfind_media), while the catalog is built separately and
    silently drops any file that fails duration probing
    (MediaProcessor.process_one). When one of those "orphaned" episodes came
    up next in the sequence, LiquidSchedule._fill used to treat it as
    MatchingContentNotFound for the whole slot, which (a) filled the slot
    from the unrelated fallback_tag pool instead of the sequence, and (b)
    permanently lost that one episode's turn forever, because
    SequenceAPI.get_next_in_sequence had already advanced current_index past
    it -- producing the exact "index N -> N+2, N+1 never plays" symptom
    observed for American Dad! on [swim] (5 pairs, e.g. 67->69).

    _fill must now keep stepping forward *within the same sequence* past
    orphaned entries, so real, catalog-resolvable episodes are never skipped
    just because an unrelated episode earlier in line has no catalog entry.
    """

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        patcher = patch("fs42.sequence_io.StationManager")
        mock_station_manager = patcher.start()
        mock_station_manager.return_value.server_conf = {"db_path": self.tmp_db.name}
        self.addCleanup(patcher.stop)

        self.station_name = "TESTSTATION"
        self.sequence_name = "test_seq"
        self.tag_path = "shows/Test Show"

        self.episodes = [
            f"{self.tag_path}/ep0 - S01E01.mkv",
            f"{self.tag_path}/ep1 - S01E02.mkv",  # orphaned: no catalog entry
            f"{self.tag_path}/ep2 - S01E03.mkv",
        ]

        sio = SequenceIO()
        ns = NamedSequence(
            self.station_name,
            self.sequence_name,
            self.tag_path,
            0.0,
            1.0,
            0,
            self.episodes,
            True,
        )
        sio.put_sequence(self.station_name, ns)

        self.conf = {
            "network_name": self.station_name,
            "content_dir": "/content",
            "clip_shows": {},
            "break_strategy": "standard",
            "schedule_increment": 30,
        }

        # bypass LiquidSchedule.__init__ (it hits the real DB via ShowCatalog
        # and LiquidAPI.get_blocks) and wire up just what _fill touches.
        self.schedule = object.__new__(LiquidSchedule)
        self.schedule.conf = self.conf
        import logging
        self.schedule._l = logging.getLogger("test_liquid")

        catalog_paths = {self.episodes[0], self.episodes[2]}  # episode[1] missing

        def fake_entry_by_fpath(fpath):
            if fpath in catalog_paths:
                return CatalogEntry(fpath, 1800.0, "shows")
            return None

        self.schedule.catalog = MagicMock()
        self.schedule.catalog.entry_by_fpath.side_effect = fake_entry_by_fpath

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def test_orphaned_episode_is_skipped_without_losing_the_next_real_one(self):
        slot_config = {"sequence": self.sequence_name}
        current_mark = datetime.datetime(2026, 9, 1, 20, 0, 0)

        block1, _ = self.schedule._fill(slot_config, self.tag_path, current_mark)
        self.assertEqual(block1.content.path, self.episodes[0])

        # episode[1] has no catalog entry -- _fill must transparently step
        # past it and land on episode[2], the next real episode in order,
        # rather than raising MatchingContentNotFound / falling back.
        block2, _ = self.schedule._fill(slot_config, self.tag_path, current_mark)
        self.assertEqual(block2.content.path, self.episodes[2])


if __name__ == "__main__":
    unittest.main()
