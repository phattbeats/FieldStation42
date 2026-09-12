import unittest

from fs42.sequence import NamedSequence, normalize_series_order, sequence_sort_key


TOS = [
    "/c/trek/star_trek/01 - TOS/Star Trek - S01E01 - The Man Trap.mkv",
    "/c/trek/star_trek/01 - TOS/Star Trek - S03E24 - Turnabout Intruder.mkv",
]
TNG = [
    "/c/trek/star_trek/09 - TNG/Star Trek- The Next Generation - S01E01 - Encounter at Farpoint.mp4",
    "/c/trek/star_trek/09 - TNG/Star Trek- The Next Generation - S07E25 - All Good Things.mp4",
]
DS9 = [
    "/c/trek/star_trek/11 - DS9/Star Trek Deep Space Nine - 1x01 - Emissary.mkv",
    "/c/trek/star_trek/11 - DS9/Star Trek Deep Space Nine - 7x26 - What You Leave Behind.mkv",
]
VOYAGER = ["/c/trek/star_trek/Star.Trek.Voyager.S05E23.Relativity.mkv"]
MOVIE = ["/c/trek/star_trek/14 - Insurrection (1998)/Star Trek Insurrection 1998 Bluray-1080p.mp4"]

POOL = TOS + TNG + DS9 + VOYAGER + MOVIE

TREK_ORDER = [
    "Star Trek",
    "Star Trek: The Next Generation",
    "Star Trek: Deep Space Nine",
]


def _series_of(fpath):
    key = sequence_sort_key(fpath)
    return key[2] if key[0] == 0 else "FALLBACK"


def _ordered(file_list, series_order=None):
    ns = NamedSequence(
        station_name="TREK",
        sequence_name="trek_marathon",
        tag_path="star_trek",
        start_perc=0.0,
        end_perc=1.0,
        current_index=0,
        file_list=file_list,
        initialized=True,
        series_order=series_order,
    )
    return [entry.fpath for entry in ns.episodes]


def _series_runs(fpaths):
    """Collapse an ordered pool down to the series it runs, in order."""
    runs = []
    for fpath in fpaths:
        series = _series_of(fpath)
        if not runs or runs[-1] != series:
            runs.append(series)
    return runs


class TestDeclaredSeriesOrder(unittest.TestCase):
    """
    PHA-3414. PHA-3413 grouped a multi-series pool by series, but ordered the
    series by normalized title, which is alphabetical: "star trek deep space
    nine" sorts ahead of "star trek the next generation", so CoreyVision's TREK
    channel airs TOS -> DS9 -> TNG while the catalog's hand-numbered folders
    (01 - TOS, 09 - TNG, 11 - DS9) say TOS -> TNG -> DS9. The engine
    deliberately never reads the folder numbering (seasonal hint dirs sit next
    to "Season 1/"), so the intended order is declared in the station conf.
    """

    def test_default_is_still_alphabetical(self):
        """No declared order -> unchanged PHA-3413 behaviour."""
        self.assertEqual(
            _series_runs(_ordered(POOL)),
            [
                "star trek",
                "star trek deep space nine",
                "star trek the next generation",
                "star trek voyager",
                "FALLBACK",
            ],
        )

    def test_declared_order_is_honoured(self):
        self.assertEqual(
            _series_runs(_ordered(POOL, TREK_ORDER)),
            [
                "star trek",
                "star trek the next generation",
                "star trek deep space nine",
                "star trek voyager",
                "FALLBACK",
            ],
        )

    def test_declared_order_does_not_disturb_episode_order(self):
        """Ranking series must not reorder episodes inside a series."""
        ordered = _ordered(POOL, TREK_ORDER)
        self.assertEqual(ordered[:2], TOS)
        self.assertEqual(ordered[2:4], TNG)
        self.assertEqual(ordered[4:6], DS9)

    def test_unlisted_series_run_after_listed_ones(self):
        """Voyager is absent from the list, so it sorts behind all three."""
        ordered = _ordered(POOL, TREK_ORDER)
        self.assertEqual(ordered[6], VOYAGER[0])

    def test_fallback_entries_still_sort_last(self):
        """PHA-2742's fallback branch is untouched by the rank component."""
        self.assertEqual(_ordered(POOL, TREK_ORDER)[-1], MOVIE[0])

    def test_conf_titles_are_matched_loosely(self):
        """Colons, dots and a year suffix in the conf must still match."""
        for written in (
            ["Star Trek", "star.trek.the.next.generation", "STAR TREK - DEEP SPACE NINE"],
            ["Star Trek (1966)", "Star Trek: The Next Generation (1987)", "Star Trek: Deep Space Nine (1993)"],
        ):
            with self.subTest(written=written):
                self.assertEqual(
                    _series_runs(_ordered(POOL, written))[:3],
                    ["star trek", "star trek the next generation", "star trek deep space nine"],
                )

    def test_empty_or_absent_order_is_none(self):
        """An empty list must behave as "not declared", not as "rank nothing"."""
        for value in (None, [], ["", "   "]):
            with self.subTest(value=value):
                self.assertIsNone(normalize_series_order(value))
                self.assertEqual(_ordered(POOL, value), _ordered(POOL))

    def test_duplicate_entry_keeps_first_position(self):
        ranks = normalize_series_order(["Star Trek", "Star Trek: Voyager", "star trek"])
        self.assertEqual(ranks["star trek"], 0)

    def test_key_shape_is_stable_without_a_declared_order(self):
        self.assertEqual(
            sequence_sort_key("Some Show - S02E05 - Title.mkv"),
            (0, 0, "some show", 2, 5),
        )

    def test_declared_order_only_moves_pools_that_declare_it(self):
        """
        The blast-radius guarantee: a sequence with no declared order sorts
        byte-for-byte the way it did before, whatever any other station declares.
        """
        other_pool = [
            "/c/scifi/shows/Babylon 5 - S01E02 - Soul Hunter.mkv",
            "/c/scifi/shows/Babylon 5 - S01E01 - Midnight on the Firing Line.mkv",
            "/c/scifi/shows/Quantum Leap - 2x01 - Honeymoon Express.mkv",
        ]
        self.assertEqual(_ordered(other_pool, None), sorted(other_pool, key=sequence_sort_key))


if __name__ == "__main__":
    unittest.main()
