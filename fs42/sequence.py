import math
import random
import re


def _natural_sort_key(fpath):
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", fpath)]


_SXXEYY_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
# Anchored to a filename-boundary context (surrounded by non-digit separators such as
# " - " / " " / "_") so it doesn't false-positive-match a resolution string like "1920x1080".
# Season capped at 1-50, episode at 1-999 to further guard against accidental matches.
_NNXNN_RE = re.compile(r"(?:^|[\s\-_])(\d{1,2})x(\d{1,3})(?:[\s\-_.]|$)")


def sequence_sort_key(fpath):
    """
    Sort key for named-sequence episode ordering.

    Parses season/episode information out of common naming conventions so that
    files are ordered by (season, episode) instead of raw lexical order:
      - "Show - S03E09 - Title.mkv"
      - "Show - 10x08 - Title.mkv"

    Mixing both naming conventions for the same show (PHA-2951) previously
    produced two separately natural-sorted blocks concatenated together,
    because the plain natural sort diverges on the literal "s" vs digit
    character right after the split point.

    When neither pattern is found, falls back to the original numeric-aware
    natural sort key (PHA-2742) so shows using plain sequential numbering
    (e.g. "Episode 2", "Episode 10") with no season/episode info still sort
    correctly and don't regress.

    The two branches return differently-shaped tuples: (0, season, episode)
    for parsed season/episode entries and (1,) + tuple(natural_key) for the
    fallback. Because the first element (0 vs 1) always differs between the
    two branches, Python's tuple comparison short-circuits on that element
    before it would ever need to compare the heterogeneous tails against
    each other, so entries of both shapes can be safely sorted together.
    """
    match = _SXXEYY_RE.search(fpath)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        return (0, season, episode)

    match = _NNXNN_RE.search(fpath)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        if 1 <= season <= 50 and 1 <= episode <= 999:
            return (0, season, episode)

    return (1,) + tuple(_natural_sort_key(fpath))


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
        initialized: bool = False
    ):
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
        self.episodes = sorted(self.episodes, key=lambda entry: sequence_sort_key(entry.fpath))

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
