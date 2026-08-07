from __future__ import annotations

import logging
from pathlib import Path

from thrift_scout.api import ShopGoodwillAPI
from thrift_scout.config import Config, load_config
from thrift_scout.email_report import (
    prepend_error_banner, render_empty_report, render_error_report, render_report,
    send_email,
)
from thrift_scout.matcher import evaluate, match_username, norm_title
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
        "num_bids": item.get("numBids") if item.get("numBids") is not None
                    else (item.get("numberOfBids") if item.get("numberOfBids") is not None else 0),
        "end_time": item.get("endTime", ""),
        "time_remaining": item.get("remainingTime", ""),
        "image_url": item.get("imageURL") or item.get("mainImageUrl") or "",
        "url": _ITEM_URL.format(iid),
        "brand": brand,
        "brand_matched": info["brand_matched"],
        "size_matched": info.get("size_matched", ""),
    }


_REQUIRED_ITEM_FIELDS = ("itemId", "title", "endTime")


def _cap_matches(matches: dict[str, list[dict]], cap: int) -> tuple[dict[str, list[dict]], int]:
    """Trim a digest to the `cap` most urgent items, keeping brand grouping.

    Returns the trimmed mapping and how many were held back. Only what is
    returned here gets emailed, persisted and watchlisted, so anything held
    back stays unseen and reappears in the next scan rather than being lost.
    """
    total = sum(len(v) for v in matches.values())
    if cap <= 0 or total <= cap:
        return matches, 0
    flat = [(brand, item) for brand, items in matches.items() for item in items]
    flat.sort(key=lambda bi: bi[1]["end_time"])   # soonest to end is most urgent
    kept: dict[str, list[dict]] = {}
    for brand, item in flat[:cap]:
        kept.setdefault(brand, []).append(item)
    return kept, total - cap


def _contract_errors(cache: dict, search_count: int) -> list[str]:
    """Detect a ShopGoodwill response-shape change instead of reporting silence.

    Every API test in this repo uses fakes, so a renamed field would leave the
    suite green while the scan quietly matched nothing. With `send_empty_email`
    on, that failure looks exactly like a normal quiet day and could run for
    weeks unnoticed. These checks make it loud.
    """
    problems: list[str] = []
    total_items = sum(len(v) for v in cache.values())
    if search_count and total_items == 0:
        problems.append(
            f"No results from any of {search_count} searches. A normal scan returns "
            f"roughly a thousand. Likely an API contract change, a block, or auth loss."
        )
        return problems
    sample = next((v[0] for v in cache.values() if v), None)
    if sample is not None:
        missing = [f for f in _REQUIRED_ITEM_FIELDS if f not in sample]
        if missing:
            problems.append(
                f"Search results are missing expected field(s) {', '.join(missing)}. "
                f"ShopGoodwill's response shape has probably changed."
            )
    return problems


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
    near_misses: list[dict] = []
    total_found = total_new = watchlisted = 0

    with ShopGoodwillAPI(config.request_delay_min, config.request_delay_max) as api, \
         Store() as store:

        # ── Auth (shared ShopGoodwill account — needed for bids + watchlist) ──
        authenticated = False
        if config.sgw_username and config.sgw_password:
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

        # ── Phase 1b: does the API still look like the API? ──
        for problem in _contract_errors(cache, len(search_keys)):
            print(f"[contract] {problem}")
            log.error(problem)
            errors.append(problem)

        # ── Phase 2: fork results per profile ──
        all_watchlist_ids: set[int] = set()

        for profile in config.profiles:
            # Graceful dedup: if Supabase is unreachable, treat all items
            # as new rather than crashing — user sees duplicates instead
            # of missing items entirely.
            try:
                seen_db, seen_titles = store.get_seen(profile.name)
            except Exception as exc:
                # Fail closed. Treating everything as new used to be the
                # fallback, but a scan sees ~1000 items, so that is a
                # thousand-row email rather than "a few duplicates" — and once
                # delivered they would all be marked seen and suppressed
                # forever. Skipping the profile costs one day and loses nothing.
                log.warning("Dedup unavailable for %s: %s", profile.name, exc)
                msg = (f"Dedup unavailable for {profile.name} ({type(exc).__name__}). "
                       f"Skipped this profile rather than sending every listing as new. "
                       f"Nothing was marked seen, so the next scan picks up where this left off.")
                errors.append(msg)
                print(f"[{profile.name}] SKIPPED — dedup unavailable")
                if not preview_html:
                    send_email("Thrift Scout: Scan skipped (dedup unavailable)",
                               render_error_report([msg]), config, profile.email)
                continue

            matches: dict[str, list[dict]] = {}
            p_found = p_new = 0
            # Profile-wide, so one listing cannot be reported twice in the same
            # digest. `dedup` below resets per target, which let an item that
            # matched two overlapping targets (a Jordan shoe matches both the
            # Nike and the Jordan target) appear under both brand headings.
            run_ids: set[int] = set()
            run_titles: set[str] = set()

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
                        if iid in seen_db or iid in run_ids:
                            continue
                        nt = norm_title(item.get("title", ""))
                        if nt and (nt in seen_titles or nt in run_titles):
                            continue
                        info, why = evaluate(item, target)
                        if info:
                            run_ids.add(iid)
                            if nt:
                                run_titles.add(nt)
                            hits.append(_record(item, target.brand, info))
                        elif why != "brand" and len(near_misses) < config.max_near_misses:
                            # Brand matched but something else rejected it. This
                            # is the only record of what the matcher throws away,
                            # and the only way to catch a rule that is too tight.
                            near_misses.append({
                                "profile": profile.name,
                                "target": target.brand,
                                "item_id": iid,
                                "title": item.get("title", "")[:500],
                                "reason": why,
                            })

                if hits:
                    hits.sort(key=lambda x: x["end_time"])
                    matches[target.brand] = hits
                    p_new += len(hits)

            # Honour the configured digest cap. It was declared in config and
            # never read, so a 93-item flood went out under a 50-item setting.
            matches, deferred = _cap_matches(matches, config.max_items_per_email)
            if deferred:
                p_new -= deferred
                print(f"[{profile.name}] Capped at {config.max_items_per_email}; "
                      f"{deferred} held for the next scan.")

            total_found += p_found
            total_new += p_new

            # Compose + send email for this profile.
            # Bids are tied to the SGW account — only show to the first profile.
            # Identity, not equality: two profiles with identical field values
            # compare equal as pydantic models and would both claim the bids.
            profile_bids = active_bids if profile is config.profiles[0] else []
            brands_summary = ", ".join(f"{b}({len(v)})" for b, v in matches.items()) or "none"
            print(f"[{profile.name}] Matches: {brands_summary} | Bids: {len(profile_bids)}")
            html, subject = None, ""
            if matches or profile_bids:
                html = render_report(matches, active_bids=profile_bids)
                # Errors used to be reported only when there was nothing else to
                # say, so a run where half the searches failed but a few items
                # matched looked completely healthy. Degraded is not success.
                if errors:
                    html = prepend_error_banner(html, errors)
                if deferred:
                    html = prepend_error_banner(html, [
                        f"Showing the {config.max_items_per_email} soonest-ending items. "
                        f"{deferred} more matched and are held for the next scan; "
                        f"they have not been marked seen."
                    ])
                parts = []
                if p_new:
                    parts.append(f"{p_new} new item{'s' if p_new != 1 else ''}")
                if profile_bids:
                    parts.append(f"{len(profile_bids)} active bid{'s' if len(profile_bids) != 1 else ''}")
                subject = f"Thrift Scout: {' + '.join(parts)}"
                if errors:
                    subject += f" ({len(errors)} error{'s' if len(errors) != 1 else ''})"
            elif errors:
                html = render_error_report(errors)
                subject = "Thrift Scout: Errors during scan"
            elif config.send_empty_email:
                html = render_empty_report()
                subject = "Thrift Scout: Nothing new today"

            delivered = False
            if html and preview_html:
                out = f"{preview_html}.{profile.name}.html"
                Path(out).write_text(html)
                print(f"[{profile.name}] Preview -> {out}")
            elif html:
                delivered = send_email(subject, html, config, profile.email)
                print(f"[{profile.name}] Email {'sent' if delivered else 'FAILED'} -> {profile.email}")
                if not delivered:
                    errors.append(f"Email delivery failed for {profile.name}")
            else:
                print(f"[{profile.name}] Nothing to send.")

            # Persist only what was actually delivered. Marking items seen
            # before the send meant a failed SMTP call buried them forever:
            # they were recorded as reported, so the next run skipped them and
            # the finds were never seen by anyone. Preview runs never persist.
            if matches and delivered:
                for brand, items in matches.items():
                    try:
                        store.mark_batch_seen(
                            profile.name,
                            [{"item_id": i["item_id"], "title": i["title"], "brand": brand}
                             for i in items],
                        )
                    except Exception as exc:
                        log.warning("Could not persist seen items for %s/%s: %s",
                                    profile.name, brand, exc)
                        errors.append(f"Seen-items save failed for {profile.name}/{brand}")
                all_watchlist_ids.update(
                    i["item_id"] for items in matches.values() for i in items
                )
            elif matches and not preview_html:
                print(f"[{profile.name}] Not persisting {p_new} item(s) — "
                      f"undelivered, so they stay eligible for the next run.")

        # ── Phase 3: watchlist (shared ShopGoodwill account) ──
        if all_watchlist_ids and authenticated:
            print(f"[watchlist] Adding {len(all_watchlist_ids)} items...")
            for iid in all_watchlist_ids:
                if api.add_to_watchlist(iid):
                    watchlisted += 1
                else:
                    errors.append(f"Watchlist failed: {iid}")

        store.log_run(total_found, total_new, watchlisted, errors)
        store.log_near_misses(near_misses)
        # 0 means never forget. Purging is what made a relist look new.
        if config.seen_retention_days > 0:
            store.purge_old(config.seen_retention_days)
        print(f"\n[done] Found={total_found}  New={total_new}  "
              f"Watchlisted={watchlisted}  NearMiss={len(near_misses)}  "
              f"Errors={len(errors)}")
