import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out heavy / native deps BEFORE any fs42 import, same pattern as
# test_sequence_orphan_skip.py, so importing fs42.sequence_io doesn't abort.
# ---------------------------------------------------------------------------
_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.sequence_io import SequenceIO   # noqa: E402
from fs42.sequence import NamedSequence   # noqa: E402


class TestSequenceIndexJournalAtomicity(unittest.TestCase):
    """
    Regression test for PHA-2952: get_next_in_sequence() commits
    current_index to disk on every call, but the liquid_blocks schedule that
    depends on those advances is only persisted once, at the very end of
    LiquidSchedule._fluid(). A weekly-rebuild killed/crashed mid-pass (as
    happened repeatedly during the PHA-2991 retry storm) used to leave
    current_index advanced with no corresponding schedule ever written,
    permanently desyncing the pointer from what actually aired.

    The fix (SequenceIO.snapshot_current_indexes /
    commit_index_journal / restore_from_index_journal) makes that pairing
    atomic: a build snapshots current_index before starting, and only clears
    the snapshot after the schedule it produced is durably persisted. If a
    build is interrupted before that point, the *next* build call finds the
    leftover snapshot and rolls current_index back to the pre-build values
    before proceeding -- so an interrupted pass net-changes nothing on disk,
    same as if it had never run.
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
            f"{self.tag_path}/ep1 - S01E02.mkv",
            f"{self.tag_path}/ep2 - S01E03.mkv",
        ]

        self.sio = SequenceIO()
        ns = NamedSequence(
            self.station_name, self.sequence_name, self.tag_path,
            0.0, 1.0, 5, self.episodes, True,
        )
        self.sio.put_sequence(self.station_name, ns)

    def tearDown(self):
        import os
        os.unlink(self.tmp_db.name)

    def _current_index(self):
        return self.sio.get_sequence(self.station_name, self.sequence_name, self.tag_path).current_index

    def _advance_index_like_get_next_in_sequence(self, new_index):
        # get_next_in_sequence() commits current_index synchronously, per
        # pick, independent of whether the caller's schedule build ever
        # finishes -- reproduce that by writing straight through put_sequence
        # (same table/column it updates) without touching the journal.
        ns = NamedSequence(
            self.station_name, self.sequence_name, self.tag_path,
            0.0, 1.0, new_index, self.episodes, True,
        )
        self.sio.put_sequence(self.station_name, ns)

    def test_killed_pass_leaves_current_index_unchanged_after_next_build_self_heals(self):
        pre_build_index = self._current_index()
        self.assertEqual(pre_build_index, 5)

        # a build starts: snapshot the baseline, then simulate several
        # get_next_in_sequence() picks advancing the pointer mid-pass...
        self.sio.snapshot_current_indexes(self.station_name)
        self._advance_index_like_get_next_in_sequence(12)
        self.assertEqual(self._current_index(), 12)

        # ...then the pass is killed (SIGTERM/SIGKILL) before
        # LiquidAPI.add_blocks()/commit_index_journal() ever runs. No
        # schedule for those picks was ever persisted.
        self.assertTrue(self.sio.has_pending_index_journal(self.station_name))

        # The NEXT build call for this station must self-heal: find the
        # leftover journal and roll current_index back to the pre-build
        # baseline before proceeding, exactly like LiquidSchedule._fluid()
        # does at the top of its loop.
        restored_count = self.sio.restore_from_index_journal(self.station_name)
        self.assertEqual(restored_count, 1)
        self.assertEqual(
            self._current_index(), pre_build_index,
            "current_index must be exactly back to its pre-build value after "
            "a killed pass is rolled back -- a killed rebuild must be a "
            "no-op on disk, not a partial pointer advance.",
        )
        self.assertFalse(self.sio.has_pending_index_journal(self.station_name))

    def test_successful_pass_commits_journal_and_keeps_the_advance(self):
        self.sio.snapshot_current_indexes(self.station_name)
        self._advance_index_like_get_next_in_sequence(9)

        # the pass completes normally: schedule gets persisted, then the
        # journal is cleared -- the advance is now "real" (backed by a
        # committed schedule) and must survive.
        self.sio.commit_index_journal(self.station_name)

        self.assertFalse(self.sio.has_pending_index_journal(self.station_name))
        self.assertEqual(self._current_index(), 9)


if __name__ == "__main__":
    unittest.main()
