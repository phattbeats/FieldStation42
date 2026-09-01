import unittest

from fs42.sequence import NamedSequence, sequence_sort_key


class TestNaturalSortKeyHandlesMixedSxxeyyAndNnxnnNaming(unittest.TestCase):
    """
    Regression test for PHA-2951: a show whose episode files mix the
    "NNxNN" (season x episode) and "SxxEyy" naming conventions must still
    sort into overall season/episode order, not into two separately
    natural-sorted blocks concatenated together.

    Before the fix, _natural_sort_key split on digit runs and compared the
    leftover string chunks lexically. "...dad! - " (NNxNN) sorts before
    "...dad! - s" (SxxEyy) at the very first differing character, so every
    NNxNN file sorted before every SxxEyy file regardless of actual season
    or episode number.
    """

    def test_natural_sort_key_handles_mixed_sxxeyy_and_nnxnn_naming(self):
        file_list = [
            "American Dad! - S03E09 - Title A.mkv",
            "American Dad! - 10x08 - Title B.mkv",
            "American Dad! - S01E01 - Title C.mkv",
            "American Dad! - 01x02 - Title D.mkv",
            "American Dad! - S10E07 - Title E.mkv",
            "American Dad! - 03x10 - Title F.mkv",
        ]

        ns = NamedSequence(
            station_name="prime_time_3",
            sequence_name="american_dad",
            tag_path="sitcoms/American Dad!",
            start_perc=0.0,
            end_perc=1.0,
            current_index=0,
            file_list=file_list,
        )

        ordered = [entry.fpath for entry in ns.episodes]

        expected = sorted(file_list, key=sequence_sort_key)

        self.assertEqual(ordered, expected)

        # Explicitly assert true season/episode order (not just self-consistency
        # with the sort key), so a broken key that still self-sorts can't pass.
        self.assertEqual(
            ordered,
            [
                "American Dad! - S01E01 - Title C.mkv",
                "American Dad! - 01x02 - Title D.mkv",
                "American Dad! - S03E09 - Title A.mkv",
                "American Dad! - 03x10 - Title F.mkv",
                "American Dad! - S10E07 - Title E.mkv",
                "American Dad! - 10x08 - Title B.mkv",
            ],
        )

    def test_natural_sort_key_falls_back_for_plain_numeric_episode_naming(self):
        """
        PHA-2742 regression guard: shows with no season/episode info at all,
        just sequential numbering, must still sort numerically ("Episode 2"
        before "Episode 10"), not lexically ("Episode 10" before "Episode 2").
        """
        file_list = [
            "Some Show - Episode 10 - Title.mkv",
            "Some Show - Episode 2 - Title.mkv",
            "Some Show - Episode 1 - Title.mkv",
        ]

        ns = NamedSequence(
            station_name="prime_time_3",
            sequence_name="some_show",
            tag_path="sitcoms/Some Show",
            start_perc=0.0,
            end_perc=1.0,
            current_index=0,
            file_list=file_list,
        )

        ordered = [entry.fpath for entry in ns.episodes]

        self.assertEqual(
            ordered,
            [
                "Some Show - Episode 1 - Title.mkv",
                "Some Show - Episode 2 - Title.mkv",
                "Some Show - Episode 10 - Title.mkv",
            ],
        )

    def test_nnxnn_does_not_false_positive_on_resolution_strings(self):
        """
        A "1920x1080" resolution token in a filename must not be mistaken
        for a season/episode NNxNN marker.
        """
        key = sequence_sort_key("Some Show - S02E05 - 1920x1080 - Title.mkv")
        self.assertEqual(key, (0, 2, 5))


if __name__ == "__main__":
    unittest.main()
