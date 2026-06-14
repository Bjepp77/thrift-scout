from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)
_PURGE_DAYS = 30


class Store:
    """Persistent store backed by Supabase (PostgREST)."""

    def __init__(self) -> None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_ANON_KEY", "")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set")
        self._base = f"{url.rstrip('/')}/rest/v1"
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        self._client = httpx.Client(headers=self._headers, timeout=30.0)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def get_seen_ids(self, profile: str) -> set[int]:
        resp = self._client.get(
            f"{self._base}/seen_items",
            params={"select": "item_id", "profile": f"eq.{profile}"},
        )
        resp.raise_for_status()
        return {r["item_id"] for r in resp.json()}

    def mark_batch_seen(self, profile: str, items: list[dict]) -> None:
        if not items:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "item_id": i["item_id"],
                "profile": profile,
                "title": i["title"],
                "brand": i["brand"],
                "first_seen": now,
                "reported": True,
            }
            for i in items
        ]
        resp = self._client.post(
            f"{self._base}/seen_items",
            json=rows,
            headers={**self._headers, "Prefer": "return=minimal,resolution=ignore-duplicates"},
        )
        resp.raise_for_status()

    def purge_old(self, days: int = _PURGE_DAYS) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        resp = self._client.delete(
            f"{self._base}/seen_items",
            params={"first_seen": f"lt.{cutoff}"},
        )
        resp.raise_for_status()

    def log_run(self, found: int, new: int, watchlisted: int, errors: list[str]) -> None:
        resp = self._client.post(
            f"{self._base}/run_log",
            json={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "items_found": found,
                "items_new": new,
                "items_watchlisted": watchlisted,
                "errors": errors,
            },
        )
        resp.raise_for_status()
