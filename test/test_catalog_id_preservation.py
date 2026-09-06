import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out heavy / native deps BEFORE any fs42 import, same pattern as
# test_sequence_index_journal_atomicity.py.
# ---------------------------------------------------------------------------
_ffmpeg_stub = MagicMock()
_ffmpeg_stub.probe = MagicMock()
sys.modules.setdefault("ffmpeg", _ffmpeg_stub)

_moviepy_stub = MagicMock()
sys.modules.setdefault("moviepy", _moviepy_stub)
sys.modules.setdefault("moviepy.editor", _moviepy_stub)

from fs42.catalog_api import CatalogAPI  # noqa: E402
from fs42.catalog_entry import CatalogEntry  # noqa: E402
from fs42.catalog_io import CatalogIO  # noqa: E402


class TestCatalogIdPreservation(unittest.TestCase):
    """
    Regression test for PHA-3225: liquid_blocks.content_json holds a catalog
    row id, so a catalog write that reissues ids silently orphans every block
    already scheduled against the old ids - the block resolves to content=None,
    which is unplayable, and used to crash LiquidManager.reset_sequences.

    set_entries() used to delete the station's rows and re-INSERT them, and
    catalog_entries.id is AUTOINCREMENT, so *every* id churned on *every*
    write - including the write reset_sequences itself does at the end. This
    keeps the id of any entry that survives the write, keyed on the same
    UNIQUE(station, tag, path) identity the table already enforces.
    """

    STATION = "Toon Town"

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        patcher = patch("fs42.catalog_io.StationManager")
        self.addCleanup(patcher.stop)
        station_manager = patcher.start()
        station_manager.return_value.server_conf = {"db_path": self.tmp_db.name}
        self.addCleanup(lambda: os.path.exists(self.tmp_db.name) and os.unlink(self.tmp_db.name))

    def _entry(self, show, episode, tag=None):
        return CatalogEntry(f"/catalog/toon_town/cartoons/{show}/{episode}.mkv", 1400.0, tag or f"cartoons/{show}")

    def _write_toonami(self, entries):
        CatalogAPI.set_entries({"network_name": "TOONAMI"}, entries)

    def _write(self, entries, station=None):
        CatalogAPI.set_entries({"network_name": station or self.STATION}, entries)

    def _ids(self):
        return {(e.tag, e.path): e.dbid for e in CatalogIO().get_catalog_entries(self.STATION)}

    def test_rewriting_an_unchanged_catalog_keeps_every_id(self):
        # this is what `station_42.py -x` does: load the catalog, write it back
        self._write([self._entry("Flapjack", f"S01E{n:02d}") for n in range(1, 6)])
        before = self._ids()

        self._write(CatalogIO().get_catalog_entries(self.STATION))

        self.assertEqual(before, self._ids())
        self.assertEqual(len(before), 5)

    def test_a_rescan_that_knows_no_dbids_still_keeps_them(self):
        # a rebuild scans from disk, so the entries it writes have dbid None
        self._write([self._entry("Flapjack", f"S01E{n:02d}") for n in range(1, 6)])
        before = self._ids()

        rescanned = [self._entry("Flapjack", f"S01E{n:02d}") for n in range(1, 6)]
        self.assertTrue(all(e.dbid is None for e in rescanned))
        self._write(rescanned)

        self.assertEqual(before, self._ids())

    def test_write_stamps_the_dbid_onto_the_in_memory_entry(self):
        # put_liquid_blocks stores block.content.dbid - a None there writes a
        # literal `null` content_json, which is an orphaned block at birth
        rescanned = [self._entry("Flapjack", "S01E01")]
        self._write(rescanned)

        self.assertIsNotNone(rescanned[0].dbid)
        self.assertEqual(self._ids()[(rescanned[0].tag, rescanned[0].path)], rescanned[0].dbid)

    def test_removed_content_frees_its_id_without_handing_it_to_someone_else(self):
        entries = [self._entry("Flapjack", f"S01E{n:02d}") for n in range(1, 6)]
        self._write(entries)
        before = self._ids()
        gone = entries[2]

        survivors = [e for e in entries if e is not gone]
        newcomer = self._entry("Toriko", "S01E01")
        self._write(survivors + [newcomer])

        after = self._ids()
        self.assertNotIn((gone.tag, gone.path), after)
        for e in survivors:
            self.assertEqual(after[(e.tag, e.path)], before[(e.tag, e.path)])
        # the freed id must not be recycled - a stale block ref pointing at it
        # would silently resolve to the wrong show
        self.assertNotEqual(after[(newcomer.tag, newcomer.path)], before[(gone.tag, gone.path)])
        self.assertGreater(after[(newcomer.tag, newcomer.path)], max(before.values()))

    def test_other_stations_are_untouched(self):
        io = CatalogIO()
        self._write([self._entry("Flapjack", "S01E01")])
        self._write_toonami([self._entry("Toriko", "S01E01")])
        toonami_before = {(e.tag, e.path): e.dbid for e in io.get_catalog_entries("TOONAMI")}

        self._write([self._entry("Flapjack", "S01E01")])

        self.assertEqual(toonami_before, {(e.tag, e.path): e.dbid for e in io.get_catalog_entries("TOONAMI")})


if __name__ == "__main__":
    unittest.main()
