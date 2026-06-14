from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

_PURGE_DAYS = 30
_INIT_SQL = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_id INTEGER PRIMARY KEY, title TEXT, brand TEXT,
    first_seen TEXT, reported INTEGER DEFAULT 0
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
        self.conn.executescript(_INIT_SQL)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.conn.close()

    def get_seen_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT item_id FROM seen_items")}

    def mark_batch_seen(self, items: list[dict]) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_items (item_id,title,brand,first_seen,reported) "
            "VALUES (?,?,?,?,1)",
            [(i["item_id"], i["title"], i["brand"], now) for i in items],
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
