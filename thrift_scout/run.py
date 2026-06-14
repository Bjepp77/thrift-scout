from __future__ import annotations

import logging
from pathlib import Path

from thrift_scout.api import ShopGoodwillAPI
from thrift_scout.config import Config, load_config
from thrift_scout.email_report import (
    render_empty_report, render_error_report, render_report, send_email,
)
from thrift_scout.matcher import match_item
from thrift_scout.store import Store

log = logging.getLogger(__name__)

_ITEM_URL = "https://shopgoodwill.com/item/{}"


def _record(item: dict, brand: str, info: dict) -> dict:
    iid = item.get("itemId")
    return {
        "item_id": iid,
        "title": item.get("title", ""),
        "current_price": float(item.get("currentPrice") or item.get("minimumBid") or 0),
        "num_bids": item.get("numBids") or item.get("numberOfBids") or 0,
        "end_time": item.get("endTime", ""),
        "time_remaining": item.get("remainingTime", ""),
        "image_url": item.get("imageURL") or item.get("mainImageUrl") or "",
        "url": _ITEM_URL.format(iid),
        "brand": brand,
        "brand_matched": info["brand_matched"],
        "size_matched": info.get("size_matched", ""),
    }


def _alert_all(config: Config, errors: list[str]) -> None:
    """Best-effort error email to every profile."""
    html = render_error_report(errors)
    for p in config.profiles:
        try:
            send_email("Thrift Scout: Fatal Error", html, config, p.email)
        except Exception:
            pass


def run(config_path: str = "config.yaml", preview_html: str | None = None) -> None:
    config = load_config(config_path)
    try:
        _execute(config, preview_html)
    except Exception as exc:
        log.critical("Fatal error: %s", exc, exc_info=True)
        _alert_all(config, [f"Fatal — {type(exc).__name__}: {exc}"])
        raise


def _execute(config: Config, preview_html: str | None) -> None:
    errors: list[str] = []
    total_found = total_new = watchlisted = 0

    with ShopGoodwillAPI(config.request_delay_min, config.request_delay_max) as api, \
         Store() as store:

        # ── Phase 1: search once per unique (term, category) across ALL profiles ──
        search_keys: dict[tuple[str, int], None] = {}
        for profile in config.profiles:
            for target in profile.targets:
                terms = target.aliases if target.match_mode == "keyword_pair" else [target.brand]
                for t in terms:
                    search_keys[(t, target.category or 0)] = None

        cache: dict[tuple[str, int], list[dict]] = {}
        for term, cat in search_keys:
            print(f"[search] {term}...")
            try:
                cache[(term, cat)] = api.search_all_pages(
                    term, cat, config.page_size, config.max_pages,
                )
                print(f"  -> {len(cache[(term, cat)])} results")
            except Exception as exc:
                msg = f"Search error ({term}): {exc}"
                errors.append(msg)
                log.error(msg, exc_info=True)
                cache[(term, cat)] = []

        # ── Phase 2: fork results per profile ──
        all_watchlist_ids: set[int] = set()

        for profile in config.profiles:
            # Graceful dedup: if Supabase is unreachable, treat all items
            # as new rather than crashing — user sees duplicates instead
            # of missing items entirely.
            try:
                seen_db = store.get_seen_ids(profile.name)
            except Exception as exc:
                log.warning("Dedup unavailable for %s: %s", profile.name, exc)
                errors.append(f"Dedup unavailable for {profile.name} — all items treated as new")
                seen_db = set()

            matches: dict[str, list[dict]] = {}
            p_found = p_new = 0

            for target in profile.targets:
                terms = target.aliases if target.match_mode == "keyword_pair" else [target.brand]
                dedup: set[int] = set()
                hits: list[dict] = []

                for t in terms:
                    for item in cache.get((t, target.category or 0), []):
                        iid = item.get("itemId")
                        if not iid or iid in dedup:
                            continue
                        dedup.add(iid)
                        p_found += 1
                        if iid in seen_db:
                            continue
                        if info := match_item(item, target):
                            hits.append(_record(item, target.brand, info))

                if hits:
                    hits.sort(key=lambda x: x["end_time"])
                    matches[target.brand] = hits
                    p_new += len(hits)
                    all_watchlist_ids.update(h["item_id"] for h in hits)

            total_found += p_found
            total_new += p_new

            # Persist seen items — graceful on failure.
            for brand, items in matches.items():
                try:
                    store.mark_batch_seen(
                        profile.name,
                        [{"item_id": i["item_id"], "title": i["title"], "brand": brand} for i in items],
                    )
                except Exception as exc:
                    log.warning("Could not persist seen items for %s/%s: %s", profile.name, brand, exc)
                    errors.append(f"Seen-items save failed for {profile.name}/{brand}")

            # Compose + send email for this profile.
            html, subject = None, ""
            if matches:
                html = render_report(matches)
                subject = f"Thrift Scout: {p_new} new item{'s' if p_new != 1 else ''} found"
            elif errors:
                html = render_error_report(errors)
                subject = "Thrift Scout: Errors during scan"
            elif config.send_empty_email:
                html = render_empty_report()
                subject = "Thrift Scout: Nothing new today"

            if html and preview_html:
                out = f"{preview_html}.{profile.name}.html"
                Path(out).write_text(html)
                print(f"[{profile.name}] Preview -> {out}")
            elif html:
                ok = send_email(subject, html, config, profile.email)
                print(f"[{profile.name}] Email {'sent' if ok else 'FAILED'} -> {profile.email}")
            else:
                print(f"[{profile.name}] Nothing to send.")

        # ── Phase 3: watchlist (shared ShopGoodwill account) ──
        if all_watchlist_ids and config.sgw_username and config.sgw_password:
            print(f"[auth] Watchlisting {len(all_watchlist_ids)} items...")
            try:
                if api.ensure_auth(config.sgw_username, config.sgw_password):
                    for iid in all_watchlist_ids:
                        if api.add_to_watchlist(iid):
                            watchlisted += 1
                        else:
                            errors.append(f"Watchlist failed: {iid}")
                else:
                    errors.append("Auth failed — check credentials.")
            except Exception as exc:
                errors.append(f"Auth/watchlist error: {exc}")

        store.log_run(total_found, total_new, watchlisted, errors)
        store.purge_old()
        print(f"\n[done] Found={total_found}  New={total_new}  "
              f"Watchlisted={watchlisted}  Errors={len(errors)}")
