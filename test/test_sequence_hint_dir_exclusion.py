import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out heavy / native deps BEFORE any fs42 import, same pattern as
# test_sequence_index_journal_atomicity.py / test_catalog_id_preservation.py.
# ---------------------------------------------------------------------------
_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.sequence_api import SequenceAPI  # noqa: E402
from fs42.sequence_io import SequenceIO  # noqa: E402


class TestSequenceHintDirExclusion(unittest.TestCase):
    """
    Regression test for PHA-3347: PHA-2784 pruned Specials/Extras/date-hint
    dirs from _find_show_dirs's walk, so random_show never scans one as its
    own bogus mini-sequence. That walk only decides which directories count
    as a show - it never filtered the file list a show (or a plain per-tag
    sequence) is actually populated from. A "December" or "December 12 -
    December 25" hint dir living under a show's own directory, or under a
    plain (non-random_show) tag directly, still got every file under it
    enumerated straight into the flat pool via MediaProcessor._rfind_media.

    Because NamedSequence.populate sorts entries alphabetically by path, and
    a hint dir like "December" sorts before "Season 1", the duplicates don't
    just inflate the episode count - they shift every real episode's index
    later. Wraparound then resumes mid-series instead of at idx0, skipping
    everything before it forever (same forward-skip shape as PHA-3279/3210,
    but in the normal -m/-w build path, not reset_sequences).
    """

    STATION = "Prime Time 3"

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.addCleanup(lambda: os.path.exists(self.tmp_db.name) and os.unlink(self.tmp_db.name))

        self.content_dir = tempfile.mkdtemp()

        io_patcher = patch("fs42.sequence_io.StationManager")
        self.addCleanup(io_patcher.stop)
        station_manager = io_patcher.start()
        station_manager.return_value.server_conf = {"db_path": self.tmp_db.name}

        # dummy files aren't real media - keep _filter_unprobeable a no-op so
        # this test is only exercising the hint-dir exclusion, per
        # test_sequence_excludes_unprobeable_files.py's PHA-2951 coverage.
        duration_patcher = patch(
            "fs42.sequence_api.MediaProcessor._get_duration",
            return_value=(1400.0, None),
        )
        self.addCleanup(duration_patcher.stop)
        duration_patcher.start()

        self.station_config = {
            "network_name": self.STATION,
            "content_dir": self.content_dir,
            "clip_shows": [],
        }

    def _touch(self, *rel_parts):
        path = os.path.join(self.content_dir, *rel_parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\x00")
        return path

    def _build(self, tag, sequence_name="weekday_slot"):
        slot = {"sequence": sequence_name}
        SequenceAPI._build_sequence(self.station_config, tag, slot)
        return SequenceIO().get_sequence(self.STATION, sequence_name, tag)

    def test_month_hint_duplicates_are_excluded_from_the_plain_tag_pool(self):
        real = [
            self._touch("king_of_the_hill", "Season 1", f"S01E{n:02d}.mkv")
            for n in range(1, 6)
        ]
        # "December" symlink-duplicate copies of two Season 1 episodes -
        # sorts alphabetically before "Season 1", so if included these would
        # occupy idx0-1 and shift every real episode's index by two.
        self._touch("king_of_the_hill", "December", "S01E01.mkv")
        self._touch("king_of_the_hill", "December", "S01E02.mkv")

        seq = self._build("king_of_the_hill")

        self.assertEqual(len(seq.episodes), 5)
        pool_paths = {e.fpath for e in seq.episodes}
        self.assertEqual(pool_paths, set(real))
        self.assertTrue(all("December" not in p for p in pool_paths))

    def test_date_range_hint_duplicates_are_excluded_from_the_plain_tag_pool(self):
        real = [self._touch("archer", "Season 10", f"S10E{n:02d}.mkv") for n in range(1, 4)]
        self._touch("archer", "December 12 - December 25", "S10E01.mkv")

        seq = self._build("archer")

        self.assertEqual(len(seq.episodes), 3)
        self.assertEqual({e.fpath for e in seq.episodes}, set(real))

    def test_wraparound_lands_on_idx0_not_mid_series(self):
        for n in range(1, 6):
            self._touch("king_of_the_hill", "Season 1", f"S01E{n:02d}.mkv")
        self._touch("king_of_the_hill", "December", "S01E01.mkv")
        self._touch("king_of_the_hill", "December", "S01E02.mkv")

        seq = self._build("king_of_the_hill")

        self.assertEqual(seq.end_index, 5)
        self.assertTrue(all("December" not in e.fpath for e in seq.episodes))

    def test_random_show_per_show_pool_excludes_hint_dirs_too(self):
        real = [
            self._touch("sitcoms", "Scrubs", "Season 1", f"S01E{n:02d}.mkv")
            for n in range(1, 4)
        ]
        self._touch("sitcoms", "Scrubs", "December", "S01E01.mkv")

        slot = {"sequence": "sitcom_rotation", "sequence_strategy": "random_show"}
        SequenceAPI._build_sequence(self.station_config, "sitcoms", slot)
        seq = SequenceIO().get_sequence(self.STATION, "sitcom_rotation", "sitcoms/Scrubs")

        self.assertEqual(len(seq.episodes), 3)
        self.assertEqual({e.fpath for e in seq.episodes}, set(real))


if __name__ == "__main__":
    unittest.main()
