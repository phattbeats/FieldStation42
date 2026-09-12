import unittest

from fs42.sequence import NamedSequence, sequence_sort_key


def _ordered(file_list, current_index=0):
    ns = NamedSequence(
        station_name="TREK",
        sequence_name="trek_marathon",
        tag_path="star_trek",
        start_perc=0.0,
        end_perc=1.0,
        current_index=current_index,
        file_list=file_list,
        initialized=True,
    )
    return [entry.fpath for entry in ns.episodes]


class TestMultiSeriesSequencePool(unittest.TestCase):
    """
    Regression test for PHA-3413: one tag folder can hold more than one series.
    CoreyVision's TREK channel points a single sequence ("trek_marathon") at a
    293-file "star_trek" pool holding TOS, TNG and DS9 side by side.

    sequence_sort_key returned (season, episode) with no series component, so
    all three series' S01E11 collided on one key and the pool interleaved
    TOS -> TNG -> DS9 -> TOS every episode. The live schedule had aired 55 TOS
    episodes in clean order and sat at current_index 55; the next build would
    have handed back "S01E20 - Court Martial" instead of "S03E01 - Spock's
    Brain" and ping-ponged between the three series from there.
    """

    # mirrors the real CoreyVision pool: three series, two naming conventions,
    # in the same folder
    TOS = [f"Star Trek - S01E{ep:02d} - T{ep} Bluray-1080p.mkv" for ep in (10, 11, 20)]
    TNG = [
        f"Star Trek- The Next Generation - S01E{ep:02d} - T{ep} HDTV-720p.mp4"
        for ep in (10, 11, 20)
    ]
    DS9 = [f"Star Trek Deep Space Nine - 1x{ep:02d} - T{ep}.mkv" for ep in (10, 11, 20)]

    def test_each_series_stays_contiguous(self):
        ordered = _ordered(self.DS9 + self.TOS + self.TNG)

        # every file of a series must sit in one unbroken run -- the defect was
        # that they alternated one episode at a time
        for series in (self.TOS, self.TNG, self.DS9):
            positions = sorted(ordered.index(f) for f in series)
            self.assertEqual(
                positions,
                list(range(positions[0], positions[0] + len(series))),
                f"series is not contiguous in {ordered}",
            )

    def test_episodes_within_a_series_stay_in_season_episode_order(self):
        ordered = _ordered(self.DS9 + self.TOS + self.TNG)

        for series in (self.TOS, self.TNG, self.DS9):
            self.assertEqual(
                [f for f in ordered if f in series],
                series,
                "episodes within a series are out of season/episode order",
            )

    def test_cursor_mid_series_does_not_jump_to_another_series(self):
        """
        The customer-visible failure: TREK had aired through TOS S02E26 and the
        cursor sat just past it. episodes[current_index] must still be the next
        TOS episode, not the first episode of a different series.
        """
        tos = [f"Star Trek - S0{s}E{ep:02d} - T.mkv" for s in (1, 2) for ep in (1, 2)]
        tng = [f"Star Trek- The Next Generation - S0{s}E{ep:02d} - T.mp4" for s in (1, 2) for ep in (1, 2)]

        ordered = _ordered(tng + tos, current_index=2)

        # cursor 2 = third TOS entry, because TOS sorts as one block ahead of TNG
        self.assertEqual(ordered[:4], tos)
        self.assertEqual(ordered[2], "Star Trek - S02E01 - T.mkv")

    def test_year_disambiguator_does_not_split_one_series(self):
        """
        "Scrubs - S08E01" and "Scrubs (2001) - S01E01" are two rips of the SAME
        show. A naive title component would treat them as two series and air all
        of S08 before restarting at S01.
        """
        ordered = _ordered(
            [
                "Scrubs - S08E01 - My Jerks Bluray-1080p.mkv",
                "Scrubs (2001) - S01E01 - My First Day (480p DVD x265 Panda).mkv",
            ]
        )

        self.assertEqual(
            ordered,
            [
                "Scrubs (2001) - S01E01 - My First Day (480p DVD x265 Panda).mkv",
                "Scrubs - S08E01 - My Jerks Bluray-1080p.mkv",
            ],
        )

    def test_seasonal_hint_directory_does_not_reorder_a_show(self):
        """
        The series component is read from the filename, never the directory.
        Seasonal hint folders ("December/") sort before "Season 1/" on a
        path-ordered key, which would open the show on its Christmas episode.
        """
        ordered = _ordered(
            [
                "/catalog/pt/sitcoms/American Dad!/Season 1/American Dad! - S01E01 - Pilot.mkv",
                "/catalog/pt/sitcoms/American Dad!/December/American Dad! - S03E09 - Christmas.mkv",
                "/catalog/pt/sitcoms/American Dad!/Season 2/American Dad! - S02E01 - Title.mkv",
            ]
        )

        self.assertEqual(
            [f.rsplit("/", 1)[1] for f in ordered],
            [
                "American Dad! - S01E01 - Pilot.mkv",
                "American Dad! - S02E01 - Title.mkv",
                "American Dad! - S03E09 - Christmas.mkv",
            ],
        )

    def test_separator_style_does_not_split_one_series(self):
        """
        "Star Trek- The Next Generation" and "Star.Trek.The.Next.Generation"
        are the same series; punctuation must not fracture the group.
        """
        a = sequence_sort_key("Star Trek- The Next Generation - S01E01 - T.mp4")
        b = sequence_sort_key("Star.Trek.The.Next.Generation.S01E02.T.mkv")
        self.assertEqual(a[1], b[1])

    def test_fallback_branch_is_unchanged(self):
        """
        PHA-2742 guard: files with no parseable season/episode still use the
        numeric-aware natural key over the FULL path, and still sort after
        parsed entries.
        """
        self.assertEqual(
            _ordered(
                [
                    "Some Show - Episode 10 - Title.mkv",
                    "Some Show - Episode 2 - Title.mkv",
                    "Some Show - Episode 1 - Title.mkv",
                ]
            ),
            [
                "Some Show - Episode 1 - Title.mkv",
                "Some Show - Episode 2 - Title.mkv",
                "Some Show - Episode 10 - Title.mkv",
            ],
        )
        self.assertEqual(sequence_sort_key("Some Show - Episode 1.mkv")[0], 1)
        self.assertEqual(sequence_sort_key("Some Show - S01E01.mkv")[0], 0)


if __name__ == "__main__":
    unittest.main()
