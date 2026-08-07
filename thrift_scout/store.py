from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from thrift_scout.matcher import norm_title as _norm

log = logging.getLogger(__name__)
_PURGE_DAYS = 30


class Store:
    """Persistent store backed by Supabase (PostgREST)."""

    def __init__(self) -> None:
        url = os.environ.get("SUPABASE_URL", "")
        # Prefer the service key. This is a server-side cron job with no browser
        # client, so there is no reason to authenticate as `anon` — a role whose
        # whole security model assumes RLS constrains it. The service key
        # bypasses RLS, which lets `public` be denied outright: a leaked anon key
        # then grants nothing. ANON is kept as a fallback so the secret can be
        # swapped in GitHub without a flag-day deploy.
        key = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and one of SUPABASE_SERVICE_KEY / SUPABASE_ANON_KEY must be set"
            )
        self.using_service_key = bool(os.environ.get("SUPABASE_SERVICE_KEY"))
        # Never log the key itself, only which one is in play.
        log.info("Supabase auth: %s", "service key" if self.using_service_key else "anon key")
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

    def get_seen(self, profile: str) -> tuple[set[int], set[str]]:
        """Return the ids and the titles already reported to this profile.

        Titles matter because ShopGoodwill relists unsold lots weekly under a
        new itemId, so an id-only check reports the same thing as new again.
        Both come from one request; splitting them would double the round trip.
        """
        # Explicit limit — PostgREST silently truncates at 1000 rows by
        # default, which would cause false "new" items as the DB grows.
        resp = self._client.get(
            f"{self._base}/seen_items",
            params={"select": "item_id,title", "profile": f"eq.{profile}", "limit": "100000"},
        )
        resp.raise_for_status()
        rows = resp.json()
        return (
            {r["item_id"] for r in rows},
            {_norm(r["title"]) for r in rows if r.get("title")},
        )

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

    def log_near_misses(self, rows: list[dict]) -> None:
        """Record listings a target almost took, for recall review."""
        if not rows:
            return
        try:
            resp = self._client.post(f"{self._base}/near_misses", json=rows)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Near-miss log failed (non-critical): %s", exc)

    def purge_old(self, days: int = _PURGE_DAYS) -> None:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            resp = self._client.delete(
                f"{self._base}/seen_items",
                params={"first_seen": f"lt.{cutoff}"},
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Purge failed (non-critical): %s", exc)

    def log_run(self, found: int, new: int, watchlisted: int, errors: list[str]) -> None:
        try:
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
        except Exception as exc:
            log.warning("Run log failed (non-critical): %s", exc)
