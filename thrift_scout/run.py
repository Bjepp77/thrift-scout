from __future__ import annotations

import logging
from pathlib import Path

from thrift_scout.api import ShopGoodwillAPI
from thrift_scout.config import Config, load_config
from thrift_scout.email_report import (
    render_empty_report, render_error_report, render_report, send_email,
)
from thrift_scout.matcher import match_item, match_username
from thrift_scout.store import Store

log = logging.getLogger(__name__)

_ITEM_URL = "https://shopgoodwill.com/item/{}"
_MAX_BID_CHECKS = 25


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


def _check_active_bids(api: ShopGoodwillAPI, errors: list[str]) -> list[dict]:
    """Fetch active bids, preferring the dedicated endpoint over favorites."""

    # ── Try dedicated "My Bids" endpoint first ──
    print("[bids] Looking for active bids...")
    my_bids = api.get_my_bids()
    if my_bids:
        return _bids_from_direct(my_bids, api)

    # ── Fallback: scan favorites + item detail ──
    print("[bids] Falling back to watchlist scan...")
    return _bids_from_favorites(api, errors)


def _bids_from_direct(items: list[dict], api: ShopGoodwillAPI) -> list[dict]:
    """Build bid records from the dedicated bids endpoint data."""
    bids: list[dict] = []
    for item in items:
        item_id = item.get("itemId")
        if not item_id:
            continue

        current_price = float(item.get("currentPrice") or 0)
        max_bid_raw = item.get("maxBidAmount")
        my_max_bid = float(max_bid_raw) if max_bid_raw is not None else None

        # quantityWon=1 → winning, 0 → outbid.  Also cross-check with price.
        qty_won = item.get("quantityWon")
        if my_max_bid is not None:
            winning = my_max_bid >= current_price
        elif qty_won is not None:
            winning = qty_won > 0
        else:
            winning = None

        num_bids = item.get("numBids")
        if num_bids is None:
            num_bids = 0

        # imageURL is empty in this endpoint — build from imageServer.
        image_url = item.get("imageURL") or ""
        if not image_url:
            server = item.get("imageServer", "")
            if server:
                image_url = f"{server}{item_id}_1_tn.jpg"

        bids.append({
            "item_id": item_id,
            "title": item.get("title", ""),
            "current_price": current_price,
            "my_max_bid": my_max_bid,
            "num_bids": num_bids,
            "end_time": item.get("endTime", ""),
            "time_remaining": item.get("remainingTime") or "",
            "image_url": image_url,
            "url": _ITEM_URL.format(item_id),
            "winning": winning,
        })

    bids.sort(key=lambda x: x["end_time"])
    winning_count = sum(1 for b in bids if b.get("winning"))
    print(f"[bids] {len(bids)} active bid{'s' if len(bids) != 1 else ''} "
          f"({winning_count} winning)")
    return bids


def _bids_from_favorites(api: ShopGoodwillAPI, errors: list[str]) -> list[dict]:
    """Fallback: scan watchlisted items for ones the user has bid on."""
    favorites = api.get_favorites("open")
    if not favorites:
        print("[bids] No open watchlist items.")
        return []

    total = len(favorites)
    cap = min(total, _MAX_BID_CHECKS)
    if total > _MAX_BID_CHECKS:
        print(f"[bids] {total} open items (checking first {cap})...")
    else:
        print(f"[bids] {total} open items — checking bid status...")
    bids: list[dict] = []
    checked = 0

    for fav in favorites:
        if checked >= _MAX_BID_CHECKS:
            break
        item_id = fav.get("itemId")
        if not item_id:
            continue

        detail = api.get_item_detail(item_id)
        if not detail:
            continue
        checked += 1

        bid_summary = (detail.get("bidHistory") or {}).get("bidSummary") or []

        # Prefer explicit auth-aware fields the API may return when logged in.
        is_bidder = detail.get("isBidder")
        is_high_bidder = detail.get("isHighBidder")

        if is_bidder is not None:
            if not is_bidder:
                continue
            winning = bool(is_high_bidder) if is_high_bidder is not None else None
        else:
            user_bid = any(
                match_username(api._username, b.get("bidderName", ""))
                for b in bid_summary
            )
            if not user_bid:
                continue
            winning = match_username(
                api._username, bid_summary[0].get("bidderName", "")
            ) if bid_summary else None

        num_bids = detail.get("numberOfBids")
        if num_bids is None:
            num_bids = detail.get("numBids")
        if num_bids is None:
            num_bids = len(bid_summary)

        bids.append({
            "item_id": item_id,
            "title": detail.get("title") or fav.get("title", ""),
            "current_price": float(
                detail.get("currentPrice") or detail.get("minimumBid") or 0
            ),
            "my_max_bid": None,
            "num_bids": num_bids,
            "end_time": detail.get("endTime") or fav.get("endTime", ""),
            "time_remaining": detail.get("remainingTime") or "",
            "image_url": (
                detail.get("imageURL")
                or detail.get("mainImageUrl")
                or detail.get("largeImageUrl")
                or ""
            ),
            "url": _ITEM_URL.format(item_id),
            "winning": winning,
        })

    bids.sort(key=lambda x: x["end_time"])
    winning_count = sum(1 for b in bids if b.get("winning"))
    print(f"[bids] {len(bids)} active bid{'s' if len(bids) != 1 else ''} "
          f"({winning_count} winning)")
    return bids


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

        # ── Auth (shared ShopGoodwill account — needed for bids + watchlist) ──
        authenticated = False
        if config.sgw_username and config.sgw_password:
            print("[auth] Logging in...")
            try:
                authenticated = api.ensure_auth(
                    config.sgw_username, config.sgw_password,
                )
                print(f"[auth] {'OK' if authenticated else 'FAILED'}")
                if not authenticated:
                    errors.append("Auth failed — check SGW credentials.")
            except Exception as exc:
                print(f"[auth] Error: {exc}")
                errors.append(f"Auth error: {exc}")
        else:
            print("[auth] No credentials configured — skipping")

        # ── Phase 0: check active bids ──
        active_bids: list[dict] = []
        if authenticated:
            try:
                active_bids = _check_active_bids(api, errors)
            except Exception as exc:
                log.warning("Bid check failed: %s", exc)
                errors.append(f"Bid check error: {exc}")

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
            if matches or active_bids:
                html = render_report(matches, active_bids=active_bids)
                parts = []
                if p_new:
                    parts.append(f"{p_new} new item{'s' if p_new != 1 else ''}")
                if active_bids:
                    parts.append(f"{len(active_bids)} active bid{'s' if len(active_bids) != 1 else ''}")
                subject = f"Thrift Scout: {' + '.join(parts)}"
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
        if all_watchlist_ids and authenticated:
            print(f"[watchlist] Adding {len(all_watchlist_ids)} items...")
            for iid in all_watchlist_ids:
                if api.add_to_watchlist(iid):
                    watchlisted += 1
                else:
                    errors.append(f"Watchlist failed: {iid}")

        store.log_run(total_found, total_new, watchlisted, errors)
        store.purge_old()
        print(f"\n[done] Found={total_found}  New={total_new}  "
              f"Watchlisted={watchlisted}  Errors={len(errors)}")
