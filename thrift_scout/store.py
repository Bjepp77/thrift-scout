from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

_PURGE_DAYS = 30
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_id INTEGER, profile TEXT DEFAULT '',
    title TEXT, brand TEXT, first_seen TEXT,
    reported INTEGER DEFAULT 0,
    PRIMARY KEY (item_id, profile)
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
    items_found INTEGER, items_new INTEGER,
    items_watchlisted INTEGER, errors TEXT
);
"""


class Store:
    def __init__(self, db_path: str = "thrift_scout_data.db"):
        self.conn = sqlite3.connect(db_path)
        self._migrate()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.conn.close()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(seen_items)")}
        if not cols:
            # Fresh DB — create from scratch.
            self.conn.executescript(_SCHEMA_SQL)
        elif "profile" not in cols:
            # v1 → v2: add profile column, rebuild with composite PK.
            self.conn.executescript("""
                ALTER TABLE seen_items RENAME TO _seen_old;
                CREATE TABLE seen_items (
                    item_id INTEGER, profile TEXT DEFAULT '',
                    title TEXT, brand TEXT, first_seen TEXT,
                    reported INTEGER DEFAULT 0,
                    PRIMARY KEY (item_id, profile)
                );
                INSERT INTO seen_items (item_id,profile,title,brand,first_seen,reported)
                    SELECT item_id,'default',title,brand,first_seen,reported FROM _seen_old;
                DROP TABLE _seen_old;
            """)
            self.conn.commit()
        # Ensure run_log exists regardless.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS run_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,"
            "items_found INTEGER, items_new INTEGER,"
            "items_watchlisted INTEGER, errors TEXT)"
        )
        self.conn.commit()

    def get_seen_ids(self, profile: str) -> set[int]:
        return {r[0] for r in self.conn.execute(
            "SELECT item_id FROM seen_items WHERE profile = ?", (profile,)
        )}

    def mark_batch_seen(self, profile: str, items: list[dict]) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_items (item_id,profile,title,brand,first_seen,reported) "
            "VALUES (?,?,?,?,?,1)",
            [(i["item_id"], profile, i["title"], i["brand"], now) for i in items],
        )
        self.conn.commit()

    def purge_old(self, days: int = _PURGE_DAYS) -> None:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        self.conn.execute("DELETE FROM seen_items WHERE first_seen < ?", (cutoff,))
        self.conn.commit()

    def log_run(self, found: int, new: int, watchlisted: int, errors: list[str]) -> None:
        self.conn.execute(
            "INSERT INTO run_log (timestamp,items_found,items_new,items_watchlisted,errors) "
            "VALUES (?,?,?,?,?)",
            (datetime.utcnow().isoformat(), found, new, watchlisted, json.dumps(errors)),
        )
        self.conn.commit()
