import json
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.media_processor import MediaProcessor  # noqa: E402


class TestCatalogProbeFailures(unittest.TestCase):
    """
    Regression tests for PHA-2197: the catalog scan dropped any file whose
    duration probe failed, and did it silently.

    The old code was:

        if duration <= 0.0:
            try:
                video_clip = VideoFileClip(fname)
                duration = video_clip.duration
                video_clip.close()
            except Exception:
                pass          # <- the only clue about why, thrown away

    That bare except swallowed the MoviePy exception, so the operator only ever
    saw "Could not get duration for <file>" with no cause - indistinguishable
    from a corrupt file, a missing codec, or a hung probe. On top of that,
    ffmpeg-python's probe() runs Popen.communicate() with no timeout, so one
    wedged ffprobe stalled the whole catalog build.
    """

    def test_moviepy_failure_reason_is_preserved(self):
        """The MoviePy exception must survive into the skip reason."""
        fname = "Show/S01E02 - Corrupt.mkv"
        skipped = []

        with patch.object(
            MediaProcessor,
            "_get_duration",
            return_value=(-1, "header damaged"),
        ), patch.object(
            MediaProcessor,
            "_moviepy_duration",
            return_value=(-1.0, "IOError: creation of filter failed"),
        ):
            result = MediaProcessor.process_one(fname, "premium", [], skipped=skipped)

        self.assertIsNone(result)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["path"], fname)
        self.assertIn("header damaged", skipped[0]["reason"])
        self.assertIn("creation of filter failed", skipped[0]["reason"])

    def test_moviepy_duration_helper_reports_the_exception(self):
        """The MoviePy probe returns the exception instead of hiding it."""
        boom = RuntimeError("creation of filter failed")

        with patch("fs42.media_processor.VideoFileClip", side_effect=boom):
            duration, hint = MediaProcessor._moviepy_duration("bad.mkv")

        self.assertEqual(duration, -1.0)
        self.assertIn("RuntimeError", hint)
        self.assertIn("creation of filter failed", hint)

    def test_moviepy_clip_is_always_closed(self):
        """A clip whose .duration raises must still be closed.

        The old code only closed the clip on the happy path, leaking the
        underlying ffmpeg process for every file it skipped.
        """
        closed = {"called": False}

        class ExplodingClip:
            def __init__(self, fname):
                self.fname = fname

            @property
            def duration(self):
                raise RuntimeError("boom")

            def close(self):
                closed["called"] = True

        with patch("fs42.media_processor.VideoFileClip", side_effect=lambda f: ExplodingClip(f)):
            duration, hint = MediaProcessor._moviepy_duration("bad.mkv")

        self.assertEqual(duration, -1.0)
        self.assertTrue(closed["called"], "clip was never closed")

    def test_ffprobe_is_bounded_by_a_timeout(self):
        """A hung ffprobe must not hang the catalog build."""
        with patch(
            "fs42.media_processor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=10),
        ):
            duration, hint = MediaProcessor._get_duration("hung.mkv")

        self.assertEqual(duration, -1)
        self.assertIn("timed out", hint)

    def test_ffprobe_timeout_is_configurable(self):
        with patch.dict("os.environ", {"FS42_FFPROBE_TIMEOUT_SECONDS": "2.5"}):
            self.assertEqual(MediaProcessor._ffprobe_timeout(), 2.5)
        with patch.dict("os.environ", {"FS42_FFPROBE_TIMEOUT_SECONDS": "not-a-number"}):
            self.assertGreater(MediaProcessor._ffprobe_timeout(), 0)

    def test_stream_duration_of_na_falls_through_to_container_duration(self):
        """A stream duration of "N/A" must not cost us a playable file.

        float("N/A") raises, and the old code let that exception escape to the
        generic handler, which returned -1 and dropped the file even though
        ffprobe had a perfectly good container duration.
        """
        payload = {
            "streams": [{"duration": "N/A"}],
            "format": {"duration": "1200.000000"},
        }
        completed = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=0, stdout=json.dumps(payload), stderr=""
        )

        with patch("fs42.media_processor.subprocess.run", return_value=completed):
            duration, hint = MediaProcessor._get_duration("na.mkv")

        self.assertEqual(duration, 1200.0)
        self.assertIsNone(hint)

    def test_probe_failure_returns_an_actionable_reason(self):
        """ffprobe stderr must be translated into something an operator can act on."""
        completed = subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=1,
            stdout="",
            stderr="[NULL @ 0x55] Invalid data found when processing input\n",
        )

        with patch("fs42.media_processor.subprocess.run", return_value=completed):
            duration, hint = MediaProcessor._get_duration("broken.mkv")

        self.assertEqual(duration, -1)
        self.assertIsNotNone(hint)
        self.assertIn("header is damaged", hint)

    def test_process_media_collects_every_skipped_file(self):
        """The whole tag's skip list is collected, not just the first file."""
        files = ["Show/S01E01 - Good.mkv", "Show/S01E02 - Bad.mkv"]
        skipped = []

        def fake_process_one(fname, tag, hints, fluid=None, content_type="feature", skipped_acc=None):
            if "Bad" in fname:
                if skipped_acc is not None:
                    skipped_acc.append({"path": fname, "reason": "header damaged"})
                return None
            return MagicMock()

        with patch.object(MediaProcessor, "process_one", side_effect=fake_process_one):
            clips = MediaProcessor._process_media(files, "premium", skipped=skipped)

        self.assertEqual(len(clips), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("Bad", skipped[0]["path"])


if __name__ == "__main__":
    unittest.main()
