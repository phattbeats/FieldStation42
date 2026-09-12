import math
import os
import random
import re


def _natural_sort_key(fpath):
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", fpath)]


_SXXEYY_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
# Anchored to a filename-boundary context (surrounded by non-digit separators such as
# " - " / " " / "_") so it doesn't false-positive-match a resolution string like "1920x1080".
# Season capped at 1-50, episode at 1-999 to further guard against accidental matches.
_NNXNN_RE = re.compile(r"(?:^|[\s\-_])(\d{1,2})x(\d{1,3})(?:[\s\-_.]|$)")

# A "(1999)"-style year disambiguator is part of the release naming convention,
# not part of the series identity: the same show can be filed as both
# "Scrubs - S08E01 - ..." and "Scrubs (2001) - S08E01 - ...". Dropping it keeps
# two rips of one series in a single progression instead of splitting them into
# two back-to-back runs of the same show.
_TITLE_YEAR_RE = re.compile(r"\((?:19|20)\d{2}\)")
# Collapse every run of non-alphanumerics to one space so the separator style
# ("Star Trek- The Next Generation" vs "Star.Trek.The.Next.Generation") does not
# fracture one series into two.
_TITLE_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


def _series_title(basename, match):
    """
    Normalized series identity: the filename text that precedes the
    season/episode marker.

    "Star Trek Deep Space Nine - 1x10 - Move Along Home.mkv" -> "star trek deep space nine"
    "Star Trek- The Next Generation - S01E10 - Hide and Q.mp4" -> "star trek the next generation"
    """
    title = _TITLE_YEAR_RE.sub(" ", basename[: match.start()])
    return _TITLE_SEPARATOR_RE.sub(" ", title.lower()).strip()


def normalize_series_order(series_order):
    """
    Turn a station conf's "sequence_series_order" list into a series -> rank map
    (PHA-3414), or None when the sequence does not declare one.

    Entries are normalized exactly the way a filename's series identity is, so
    the conf can be written the readable way ("Star Trek: The Next Generation")
    and still match "Star Trek- The Next Generation - S01E10 - Hide and Q.mp4".

    Returning None -- not an empty map -- for an absent/empty list is what keeps
    the blast radius at zero: sequence_sort_key emits a constant rank in that
    case, so every pool that does not declare the key sorts exactly as it did
    before this change.
    """
    if not series_order:
        return None
    ranks = {}
    for rank, name in enumerate(series_order):
        title = _TITLE_SEPARATOR_RE.sub(" ", _TITLE_YEAR_RE.sub(" ", str(name).lower())).strip()
        # first listing wins, so a duplicated entry can't silently demote a series
        if title and title not in ranks:
            ranks[title] = rank
    return ranks or None


def sequence_sort_key(fpath, series_ranks=None):
    """
    Sort key for named-sequence episode ordering.

    Parses series/season/episode information out of common naming conventions
    so that files are ordered by (series, season, episode) instead of raw
    lexical order:
      - "Show - S03E09 - Title.mkv"
      - "Show - 10x08 - Title.mkv"

    Mixing both naming conventions for the same show (PHA-2951) previously
    produced two separately natural-sorted blocks concatenated together,
    because the plain natural sort diverges on the literal "s" vs digit
    character right after the split point.

    The series component (PHA-3413) exists because one tag folder can hold more
    than one series -- "star_trek" holds TOS, TNG and DS9 side by side. With a
    (season, episode) key alone, all three series' S01E11 collide on one key and
    the pool interleaves TOS -> TNG -> DS9 -> TOS every single episode instead
    of playing one series through. The docs describe stock ordering as "all
    videos are collected recursively and sorted by full path", which keeps a
    multi-series folder grouped; this key is a deviation from that (it has to
    be, for PHA-2951/PHA-2742), so it has to carry the grouping itself.

    The series identity is taken from the BASENAME, not the directory: the
    directory is unreliable here because seasonal hint folders ("December/",
    "October 27 - October 31/") sit alongside "Season 1/" under the same show
    and a path-ordered key would air the Christmas episodes first.

    When neither pattern is found, falls back to the original numeric-aware
    natural sort key over the full path (PHA-2742) so shows using plain
    sequential numbering (e.g. "Episode 2", "Episode 10") with no
    season/episode info still sort correctly and don't regress.

    The rank component (PHA-3414) is how a station declares which series should
    run second. The series component above orders series by normalized title,
    which is alphabetical -- "star trek deep space nine" sorts ahead of "star
    trek the next generation", so a TOS/TNG/DS9 pool airs TOS -> DS9 -> TNG
    while the catalog's hand-numbered folders say TOS -> TNG -> DS9. The engine
    deliberately never reads that folder numbering (see the note on hint dirs
    above), so the intended run order has to be stated somewhere the engine can
    see it: the optional "sequence_series_order" list in the station conf.

    series_ranks is the normalized map from that list (see
    normalize_series_order). When it is None -- which is the case for every
    sequence that does not declare the key -- the rank is the constant 0, so the
    key degrades to exactly the PHA-3413 (0, series, season, episode) ordering
    and no existing pool moves. A series absent from a declared list ranks after
    every listed one, ordered among its peers by title as before.

    The two branches return differently-shaped tuples: (0, rank, series, season,
    episode) for parsed season/episode entries and (1,) + tuple(natural_key)
    for the fallback. Because the first element (0 vs 1) always differs between
    the two branches, Python's tuple comparison short-circuits on that element
    before it would ever need to compare the heterogeneous tails against each
    other, so entries of both shapes can be safely sorted together.
    """
    basename = os.path.basename(fpath)

    match = _SXXEYY_RE.search(basename)
    if not match:
        candidate = _NNXNN_RE.search(basename)
        if candidate:
            season, episode = int(candidate.group(1)), int(candidate.group(2))
            if 1 <= season <= 50 and 1 <= episode <= 999:
                match = candidate

    if match is None:
        return (1,) + tuple(_natural_sort_key(fpath))

    series = _series_title(basename, match)
    season, episode = int(match.group(1)), int(match.group(2))
    if series_ranks is None:
        rank = 0
    else:
        rank = series_ranks.get(series, len(series_ranks))
    return (0, rank, series, season, episode)


class SequenceEntry:
    def __init__(self, fpath):
        self.fpath = str(fpath)

    def __str__(self):
        return f"SequenceEntry(fpath={self.fpath})"


class NamedSequence:
    def __init__(
        self,
        station_name: str,
        sequence_name: str,
        tag_path: str,
        start_perc: float,
        end_perc: float,
        current_index: int,
        file_list: list[str],
        initialized: bool = False,
        series_order: list[str] | None = None,
    ):
        # PHA-3414: optional declared run order for a multi-series pool. Kept as
        # a trailing keyword-defaulted argument so every existing positional
        # construction site (sequence_io, sequence_api, the test suite) is
        # unchanged and defaults to the pre-3414 ordering.
        self.series_order = series_order
        self._series_ranks = normalize_series_order(series_order)
        self.station_name = station_name
        self.sequence_name = sequence_name
        self.tag_path = tag_path
        self.start_perc = start_perc
        self.end_perc = end_perc
        self.current_index = current_index
        self.initialized = initialized
        self.episodes = []  # Initialize episodes as an empty list
        self.start_index = 0
        self.end_index = 0
        self.populate(file_list)  # Populate episodes with the provided file list



    def __str__(self):
        return f"NamedSequence(station={self.station_name}, sequence={self.sequence_name}, tag={self.tag_path}, start={self.start_perc}, end={self.end_perc}, index={self.current_index})"

    def populate(self, file_list):
        self.episodes = []  # Reset the episodes list
        for file in file_list:
            entry = SequenceEntry(file)
            self.episodes.append(entry)

        # sort by season/episode when parseable (PHA-2951), falling back to natural
        # (numeric-aware) order so e.g. "Episode 2" sorts before "Episode 10" (PHA-2742).
        # A plain natural/string sort put "10x08"-named files before all "S03E09"-named
        # files for the same show regardless of actual season/episode, scrambling order.
        # A declared series order (PHA-3414) ranks series ahead of the title
        # comparison; without one self._series_ranks is None and the key is
        # byte-for-byte the PHA-3413 ordering.
        self.episodes = sorted(
            self.episodes, key=lambda entry: sequence_sort_key(entry.fpath, self._series_ranks)
        )

        self.end_index = math.floor(self.end_perc * (len(self.episodes)))

        if self.start_perc < 0 and not self.initialized:
            self.start_index = 0
            self.current_index = random.randrange(self.start_index,self.end_index)
            self.initialized = True
        elif self.start_perc >= 0 and not self.initialized:
            self.start_index = math.floor(self.start_perc * (len(self.episodes)))
            self.current_index = self.start_index
            self.initialized = True

    def get_series_length(self):
        return len(self._episodes)
