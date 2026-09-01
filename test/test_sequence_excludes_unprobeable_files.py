import sys
import unittest
from unittest.mock import MagicMock, patch

_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.sequence_api import SequenceAPI  # noqa: E402


class TestSequenceExcludesUnprobeableFiles(unittest.TestCase):
    """
    Regression test for PHA-2951: named-sequence file lists are scanned
    straight off the filesystem, independently of the catalog build, which
    separately (and silently) drops any file that fails ffprobe duration
    detection -- e.g. American Dad! S05E09, S05E12, S05E14, S07E06 on
    [swim]/Prime Time 3 are all real, corrupt ("header damaged" / "Missing
    VOL header") encodes confirmed via a direct ffprobe run. Left in
    sequence_entries, each one is permanently unplayable and forces a
    single-episode index skip in the aired schedule every time the rotation
    reaches it, forever.

    SequenceAPI._filter_unprobeable must drop any file whose duration can't
    be determined before it's ever written to sequence_entries, so the
    canonical order is built from only the episodes that can actually air.
    """

    def test_files_with_no_duration_are_excluded(self):
        good = "Show/S01E01 - Good.mkv"
        corrupt = "Show/S01E02 - Corrupt.mkv"

        def fake_get_duration(fname):
            if fname == corrupt:
                return -1, "header damaged"
            return 1800.0, None

        with patch(
            "fs42.sequence_api.MediaProcessor._get_duration",
            side_effect=fake_get_duration,
        ):
            result = SequenceAPI._filter_unprobeable([good, corrupt])

        self.assertEqual(result, [good])


if __name__ == "__main__":
    unittest.main()
