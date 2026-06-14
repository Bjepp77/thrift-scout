from __future__ import annotations

import logging
from pathlib import Path

from thrift_scout.api import ShopGoodwillAPI
from thrift_scout.config import load_config
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


def run(config_path: str = "config.yaml", preview_html: str | None = None) -> None:
    config = load_config(config_path)
    errors: list[str] = []
    all_matches: dict[str, list[dict]] = {}
    total_found = total_new = watchlisted = 0

    with ShopGoodwillAPI(config.request_delay_min, config.request_delay_max) as api, \
         Store() as store:

        seen_db = store.get_seen_ids()

        for target in config.targets:
            print(f"[search] {target.brand}...")
            try:
                terms = target.aliases if target.match_mode == "keyword_pair" else [target.brand]
                dedup: set[int] = set()
                hits: list[dict] = []

                for term in terms:
                    for item in api.search_all_pages(
                        term, target.category or 0, config.page_size, config.max_pages,
                    ):
                        iid = item.get("itemId")
                        if not iid or iid in dedup:
                            continue
                        dedup.add(iid)
                        total_found += 1
                        if iid in seen_db:
                            continue
                        if info := match_item(item, target):
                            hits.append(_record(item, target.brand, info))

                if hits:
                    hits.sort(key=lambda x: x["end_time"])
                    all_matches[target.brand] = hits
                    total_new += len(hits)
                print(f"  -> {len(hits)} new matches")
            except Exception as exc:
                msg = f"Error searching {target.brand}: {exc}"
                errors.append(msg)
                log.error(msg, exc_info=True)

        # Watchlist
        if all_matches and config.sgw_username and config.sgw_password:
            print("[auth] Authenticating...")
            try:
                if api.ensure_auth(config.sgw_username, config.sgw_password):
                    for items in all_matches.values():
                        for it in items:
                            if api.add_to_watchlist(it["item_id"]):
                                watchlisted += 1
                            else:
                                errors.append(f"Watchlist failed: {it['item_id']}")
                else:
                    errors.append("Auth failed — check credentials or re-login manually.")
            except Exception as exc:
                errors.append(f"Auth/watchlist error: {exc}")

        # Persist
        for brand, items in all_matches.items():
            store.mark_batch_seen(
                [{"item_id": i["item_id"], "title": i["title"], "brand": brand} for i in items]
            )

        # Email / preview
        html, subject = None, ""
        if all_matches:
            html = render_report(all_matches)
            subject = f"Thrift Scout: {total_new} new item{'s' if total_new != 1 else ''} found"
        elif errors:
            html = render_error_report(errors)
            subject = "Thrift Scout: Errors during scan"
        elif config.send_empty_email:
            html = render_empty_report()
            subject = "Thrift Scout: Nothing new today"

        if html and preview_html:
            Path(preview_html).write_text(html)
            print(f"[preview] Saved to {preview_html}")
        elif html:
            ok = send_email(subject, html, config)
            print(f"[email] {'Sent' if ok else 'Failed'} -> {config.email_recipient}")

        store.log_run(total_found, total_new, watchlisted, errors)
        store.purge_old()
        print(f"\n[done] Found={total_found}  New={total_new}  "
              f"Watchlisted={watchlisted}  Errors={len(errors)}")
