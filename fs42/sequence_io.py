import sqlite3
from contextlib import contextmanager

from fs42.station_manager import StationManager
from fs42.sequence import NamedSequence, normalize_series_order, sequence_sort_key


def _series_order_for(station_name):
    """
    The station conf's optional "sequence_series_order" (PHA-3414), or None.

    Sequences rebuilt from the database carry no conf of their own, so the
    declared run order has to be looked up by station name here -- the one place
    the DB rows are turned back into NamedSequence objects. A station with no
    conf loaded (or no declared order) yields None, which is the pre-3414
    ordering.
    """
    station_conf = StationManager().station_by_name(station_name)
    if not station_conf:
        return None
    return station_conf.get("sequence_series_order")


class SequenceIO:
    def __init__(self):
        self.db_path = StationManager().server_conf["db_path"]
        self._init_sequence_table()

    @contextmanager
    def _get_connection(self):
        connection = sqlite3.connect(self.db_path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_sequence_table(self):
        """
        Creates a database table to hold SeriesIndex records.
        Each record is associated with a series (text string).
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute("""CREATE TABLE IF NOT EXISTS named_sequence (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                station TEXT NOT NULL,
                                sequence_name TEXT NOT NULL,
                                tag_path TEXT NOT NULL,
                                start_perc REAL NOT NULL,
                                end_perc REAL NOT NULL,
                                current_index INTEGER NOT NULL,
                                initialized INTEGER NOT NULL DEFAULT 1,
                                UNIQUE(station, sequence_name, tag_path)
                            )""")
            try:
                cursor.execute("ALTER TABLE named_sequence ADD COLUMN initialized INTEGER NOT NULL DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE named_sequence ADD COLUMN parent_tag TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
                
            # now make a table to hold sequence entries
            cursor.execute("""CREATE TABLE IF NOT EXISTS sequence_entries (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                fpath TEXT NOT NULL,
                                sequence_index INTEGER NOT NULL,
                                named_sequence_id INTEGER NOT NULL,
                                FOREIGN KEY(named_sequence_id) REFERENCES named_sequence(id)
                            )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sequence_group_state (
                    station TEXT NOT NULL,
                    sequence_name TEXT NOT NULL,
                    parent_tag TEXT NOT NULL,
                    active_tag_path TEXT NOT NULL,
                    PRIMARY KEY (
                        station,
                        sequence_name,
                        parent_tag
                    )
                )
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sequence_entries_named_sequence
            ON sequence_entries(named_sequence_id)
            """)

            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sequence_entries_lookup
            ON sequence_entries(named_sequence_id, sequence_index)
            """)

            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_named_sequence_station
            ON named_sequence(station)
            """)
            
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_named_sequence_parent
            ON named_sequence(station, sequence_name, parent_tag)
            """)

            # PHA-2952: pre-build snapshot of current_index, used to make a
            # schedule build atomic with respect to sequence pointer advances.
            # get_next_in_sequence() commits current_index per-call (needed so
            # repeated picks of the same sequence within one build see prior
            # picks), but the resulting liquid_blocks schedule is only written
            # once, at the very end of _fluid(). A killed/retried weekly-rebuild
            # could previously leave pointers advanced with no corresponding
            # schedule ever committed, permanently scrambling playback order.
            # snapshot_current_indexes() is called before a build starts;
            # commit_index_journal() clears it once the schedule is durably
            # persisted; if a build never reaches that point, the next build
            # finds the leftover snapshot and restore_from_journal() rewinds
            # current_index back to the pre-build values before proceeding.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sequence_index_journal (
                    station TEXT NOT NULL,
                    sequence_name TEXT NOT NULL,
                    tag_path TEXT NOT NULL,
                    pre_build_index INTEGER NOT NULL,
                    PRIMARY KEY (station, sequence_name, tag_path)
                )
            """)
            cursor.close()
            connection.commit()

    def has_pending_index_journal(self, station_name: str) -> bool:
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT 1 FROM sequence_index_journal WHERE station = ? LIMIT 1",
                (station_name,),
            )
            return cursor.fetchone() is not None

    def restore_from_index_journal(self, station_name: str):
        """Roll current_index back to the values recorded by the last
        snapshot_current_indexes() call for this station, then clear the
        journal. Used to self-heal a station left in an inconsistent state
        by a build that was killed before commit_index_journal()."""
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT sequence_name, tag_path, pre_build_index FROM sequence_index_journal WHERE station = ?",
                (station_name,),
            )
            rows = cursor.fetchall()
            for sequence_name, tag_path, pre_build_index in rows:
                cursor.execute(
                    """UPDATE named_sequence
                                  SET current_index = ?
                                  WHERE station = ? AND sequence_name = ? AND tag_path = ?""",
                    (pre_build_index, station_name, sequence_name, tag_path),
                )
            cursor.execute("DELETE FROM sequence_index_journal WHERE station = ?", (station_name,))
            connection.commit()
            return len(rows)

    def snapshot_current_indexes(self, station_name: str):
        """Record current_index for every sequence on this station before a
        schedule build starts. Overwrites any prior snapshot for the station
        (a fresh build always defines the new pre-build baseline)."""
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute("DELETE FROM sequence_index_journal WHERE station = ?", (station_name,))
            cursor.execute(
                "SELECT sequence_name, tag_path, current_index FROM named_sequence WHERE station = ?",
                (station_name,),
            )
            rows = cursor.fetchall()
            for sequence_name, tag_path, current_index in rows:
                cursor.execute(
                    """INSERT INTO sequence_index_journal
                                  (station, sequence_name, tag_path, pre_build_index)
                                  VALUES (?, ?, ?, ?)""",
                    (station_name, sequence_name, tag_path, current_index),
                )
            connection.commit()

    def commit_index_journal(self, station_name: str):
        """Clear the pre-build snapshot once the schedule that depends on the
        advanced current_index values has been durably persisted."""
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute("DELETE FROM sequence_index_journal WHERE station = ?", (station_name,))
            connection.commit()

    def put_sequence(self, station_name: str, named_sequence):

        with self._get_connection() as connection:
            cursor = connection.cursor()

            cursor.execute(
                """
                INSERT OR REPLACE INTO named_sequence
                    (station, sequence_name, tag_path, start_perc, end_perc, current_index, initialized, parent_tag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station, sequence_name, tag_path)
                DO UPDATE SET
                    start_perc = excluded.start_perc,
                    end_perc = excluded.end_perc,
                    current_index = excluded.current_index,
                    parent_tag = excluded.parent_tag
                """,
                (
                    station_name,
                    named_sequence.sequence_name,
                    named_sequence.tag_path,
                    named_sequence.start_perc,
                    named_sequence.end_perc,
                    named_sequence.current_index,
                    int(named_sequence.initialized),                    
                    getattr(named_sequence, "parent_tag", None),
                ),
            )

            # stable ID lookup
            cursor.execute(
                """
                SELECT id FROM named_sequence
                WHERE station = ? AND sequence_name = ? AND tag_path = ?
                """,
                (station_name, named_sequence.sequence_name, named_sequence.tag_path),
            )

            row = cursor.fetchone()
            if not row:
                raise RuntimeError("UPSERT failed to return sequence id")

            named_sequence_id = row[0]

            # Replace entries
            cursor.execute(
                "DELETE FROM sequence_entries WHERE named_sequence_id = ?",
                (named_sequence_id,),
            )

            for index, entry in enumerate(named_sequence.episodes):
                cursor.execute(
                    """
                    INSERT INTO sequence_entries
                        (fpath, sequence_index, named_sequence_id)
                    VALUES (?, ?, ?)
                    """,
                    (entry.fpath, index, named_sequence_id),
                )

            connection.commit()

    def get_sequence(self, station_name: str, sequence_name: str, tag_path: str) -> NamedSequence:
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """SELECT id, start_perc, end_perc, current_index , initialized
                              FROM named_sequence 
                              WHERE station = ? AND sequence_name = ? AND tag_path = ?""",
                (station_name, sequence_name, tag_path),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            named_sequence_id, start_perc, end_perc, current_index, initialized = row

            # Now retrieve the sequence entries
            cursor.execute(
                """SELECT fpath FROM sequence_entries 
                              WHERE named_sequence_id = ? ORDER BY sequence_index""",
                (named_sequence_id,),
            )
            file_paths = [row[0] for row in cursor.fetchall()]

            ns = NamedSequence(station_name, sequence_name, tag_path, start_perc, end_perc, current_index, file_paths, bool(initialized), _series_order_for(station_name))
            
            if ns.initialized != bool(initialized):
                self.update_initialized(station_name, sequence_name, tag_path, ns.initialized)

            return ns

    def get_all_sequences_for_station(self, station_name: str) -> list[NamedSequence]:
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """SELECT id, sequence_name, tag_path, start_perc, end_perc, current_index, initialized
                              FROM named_sequence
                              WHERE station = ?""",
                (station_name,),
            )
            rows = cursor.fetchall()

            if not rows:
                return []

            sequences = []
            for row in rows:
                named_sequence_id, sequence_name, tag_path, start_perc, end_perc, current_index, initialized = row

                # Now retrieve the sequence entries for this sequence
                cursor.execute(
                    """SELECT fpath FROM sequence_entries
                                  WHERE named_sequence_id = ? ORDER BY sequence_index""",
                    (named_sequence_id,),
                )
                file_paths = [entry_row[0] for entry_row in cursor.fetchall()]

                ns = NamedSequence(station_name, sequence_name, tag_path, start_perc, end_perc, current_index, file_paths, bool(initialized), _series_order_for(station_name))
                if ns.initialized != bool(initialized):
                    self.update_initialized(station_name, sequence_name, tag_path, ns.initialized)
                sequences.append(ns)

            return sequences


    def delete_sequences_for_station(self, station_name: str):
        with self._get_connection() as connection:
            cursor = connection.cursor()
            # Delete all sequence entries for the station
            cursor.execute(
                """DELETE FROM sequence_entries 
                              WHERE named_sequence_id IN 
                              (SELECT id FROM named_sequence WHERE station = ?)""",
                (station_name,),
            )
            # Delete the named sequences for the station
            cursor.execute("""DELETE FROM named_sequence WHERE station = ?""", (station_name,))
            connection.commit()

    def delete_sequence(self,station_name,sequence_name,tag_path):
        with self._get_connection() as connection:
            cursor = connection.cursor()

            cursor.execute("""
                SELECT id
                FROM named_sequence
                WHERE station = ?
                  AND sequence_name = ?
                  AND tag_path = ?
            """, (
                station_name,
                sequence_name,
                tag_path
            ))

            row = cursor.fetchone()

            if not row:
                return

            sequence_id = row[0]

            cursor.execute(
                """
                DELETE FROM sequence_entries
                WHERE named_sequence_id = ?
                """,
                (sequence_id,)
            )

            cursor.execute(
                """
                DELETE FROM named_sequence
                WHERE id = ?
                """,
                (sequence_id,)
            )

            connection.commit()
    
    def update_current_index(self, station_name: str, sequence_name: str, tag_path: str, new_index: int):
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """UPDATE named_sequence
                              SET current_index = ?
                              WHERE station = ? AND sequence_name = ? AND tag_path = ?""",
                (new_index, station_name, sequence_name, tag_path),
            )
            cursor.close()
            connection.commit()

    def update_initialized(self, station_name: str, sequence_name: str, tag_path: str, value: bool):
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """UPDATE named_sequence
                              SET initialized = ?
                              WHERE station = ? AND sequence_name = ? AND tag_path = ?""",
                (int(value), station_name, sequence_name, tag_path),
            )
            cursor.close()
            connection.commit()

    def update_sequence_index_by_path(self, station_name: str, sequence_name: str, tag_path: str, episode_path: str):
        with self._get_connection() as connection:
            cursor = connection.cursor()
            # Get the sequence_index for the episode_path
            cursor.execute("""
                SELECT se.sequence_index 
                FROM sequence_entries se
                JOIN named_sequence ns ON se.named_sequence_id = ns.id
                WHERE ns.station = ? AND ns.sequence_name = ? AND ns.tag_path = ? AND se.fpath = ?
            """, (station_name, sequence_name, tag_path, episode_path))
            
            result = cursor.fetchone()
            if result:
                new_index = result[0]
                cursor.execute("""
                    UPDATE named_sequence 
                    SET current_index = ? 
                    WHERE station = ? AND sequence_name = ? AND tag_path = ?
                """, (new_index, station_name, sequence_name, tag_path))
                connection.commit()
                return True
            return False

    def update_sequence_entries(self, station_name: str, sequence_name: str, tag_path: str, file_list: list, current_file: str, fallback_index: int):
        """
        Replace sequence entries with a new file list while preserving playback position.
        - If current_file is still in the new list, current_index moves to its new sorted position.
        - If current_file is gone but fallback_index is still in bounds, keep it (different file is there now).
        - If fallback_index is out of bounds, reset to 0.
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()

            cursor.execute(
                "SELECT id FROM named_sequence WHERE station = ? AND sequence_name = ? AND tag_path = ?",
                (station_name, sequence_name, tag_path),
            )
            row = cursor.fetchone()
            if not row:
                return

            named_sequence_id = row[0]
            # Use the same season/episode-aware sort key as NamedSequence.populate (PHA-2951)
            # instead of a plain string sort, so re-scans don't scramble marathon order for
            # shows whose files mix "SxxEyy" and "NNxNN" naming conventions.
            # PHA-3414: rank with the station's declared series order too, so the
            # cursor remap below lands on the same episode the engine will play.
            series_ranks = normalize_series_order(_series_order_for(station_name))
            sorted_files = sorted((str(f) for f in file_list), key=lambda f: sequence_sort_key(f, series_ranks))

            import logging
            _l = logging.getLogger("SEQUENCE")
            if current_file and current_file in sorted_files:
                new_index = sorted_files.index(current_file)
                _l.debug(f"update_sequence_entries: current file still present, index {fallback_index} -> {new_index} ({sorted_files[new_index]})")
            elif fallback_index < len(sorted_files):
                new_index = fallback_index
                _l.debug(f"update_sequence_entries: current file removed, keeping index {new_index} (now points to {sorted_files[new_index]})")
            else:
                new_index = 0
                _l.debug(f"update_sequence_entries: index {fallback_index} out of bounds for {len(sorted_files)} files, resetting to 0")

            cursor.execute("DELETE FROM sequence_entries WHERE named_sequence_id = ?", (named_sequence_id,))

            for idx, fpath in enumerate(sorted_files):
                cursor.execute(
                    "INSERT INTO sequence_entries (fpath, sequence_index, named_sequence_id) VALUES (?, ?, ?)",
                    (fpath, idx, named_sequence_id),
                )

            cursor.execute(
                "UPDATE named_sequence SET current_index = ? WHERE id = ?",
                (new_index, named_sequence_id),
            )
            connection.commit()

    def clean_sequences(self):
        """
        Clean up sequences by removing entries that are no longer valid.
        This can be used to remove entries that have been deleted from the filesystem.
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """DELETE FROM sequence_entries
                        WHERE NOT EXISTS (
                            SELECT 1 FROM named_sequence
                            WHERE named_sequence.id = sequence_entries.named_sequence_id
                        );"""
            )
            connection.commit()
            cursor.close()

    def get_active_sequence(
        self,
        station_name,
        sequence_name,
        parent_tag
    ):
        with self._get_connection() as connection:
            cursor = connection.cursor()

            cursor.execute("""
                SELECT active_tag_path
                FROM sequence_group_state
                WHERE station = ?
                  AND sequence_name = ?
                  AND parent_tag = ?
            """, (
                station_name,
                sequence_name,
                parent_tag
            ))

            row = cursor.fetchone()

            return row[0] if row else None
            
    def set_active_sequence(
        self,
        station_name,
        sequence_name,
        parent_tag,
        active_tag_path
    ):
        with self._get_connection() as connection:
            cursor = connection.cursor()

            cursor.execute("""
                INSERT OR REPLACE INTO sequence_group_state
                (
                    station,
                    sequence_name,
                    parent_tag,
                    active_tag_path
                )
                VALUES (?, ?, ?, ?)
            """, (
                station_name,
                sequence_name,
                parent_tag,
                active_tag_path
            ))
            connection.commit()
            
    def get_all_active_sequences(
        self,
        station_name,
        sequence_name=None
    ):
        with self._get_connection() as connection:
            cursor = connection.cursor()

            if sequence_name is not None:
                cursor.execute("""
                    SELECT active_tag_path
                    FROM sequence_group_state
                    WHERE station = ?
                      AND sequence_name = ?
                """, (
                    station_name,
                    sequence_name,
                ))
            else:
                cursor.execute("""
                    SELECT active_tag_path
                    FROM sequence_group_state
                    WHERE station = ?
                """, (
                    station_name,
                ))

            return [
                r[0]
                for r in cursor.fetchall()
            ]
            
    def get_parent_tag_for_active(
        self,
        station_name,
        sequence_name,
        active_tag_path
    ):
        with self._get_connection() as connection:
            cursor = connection.cursor()

            cursor.execute("""
                SELECT parent_tag
                FROM sequence_group_state
                WHERE station = ?
                  AND sequence_name = ?
                  AND active_tag_path = ?
            """, (
                station_name,
                sequence_name,
                active_tag_path
            ))

            row = cursor.fetchone()

            return row[0] if row else None

    def get_child_sequences(
        self,
        station_name,
        sequence_name,
        parent_tag
    ):
        with self._get_connection() as connection:
            cursor = connection.cursor()

            cursor.execute("""
                SELECT tag_path
                FROM named_sequence
                WHERE station = ?
                  AND sequence_name = ?
                  AND tag_path LIKE ?
            """, (
                station_name,
                sequence_name,
                f"{parent_tag}/%"
            ))

            return [r[0] for r in cursor.fetchall()]