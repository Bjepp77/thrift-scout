from __future__ import annotations

import base64
import logging
import random
import re
import time
import urllib.parse
from typing import Any

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

log = logging.getLogger(__name__)

_BASE = "https://buyerapi.shopgoodwill.com/api"
_KEY = b"6696D2E6F042FEC4D6E3F32AD541143B"
_IV = b"0000000000000000"

# Rotated per session — avoids a single static fingerprint across runs.
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
]

# Headers a real browser sends on XHR/fetch to a same-site API.
_BROWSER_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://shopgoodwill.com",
    "Referer": "https://shopgoodwill.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# Minimal diff from API defaults — every field the endpoint expects.
_SEARCH_DEFAULTS: dict[str, Any] = {
    "searchText": "", "selectedGroup": "", "selectedCategoryIds": "",
    "selectedSellerIds": "", "lowPrice": 0, "highPrice": 999999,
    "searchBuyNowOnly": "", "searchPickupOnly": False,
    "searchNoPickupOnly": False, "searchOneCentShippingOnly": False,
    "searchDescriptions": False, "searchClosedAuctions": False,
    "closedAuctionEndingDate": "01/01/1970", "closedAuctionDaysBack": "7",
    "searchCanadaShipping": False, "searchInternationalShippingOnly": False,
    "sortColumn": "1", "page": 1, "pageSize": 40, "sortDescending": False,
    "savedSearchId": 0, "useBuyerPrefs": True, "searchUSOnlyShipping": False,
    "categoryLevelNo": "1", "categoryLevel": 1, "categoryId": 0,
    "partNumber": "", "catIds": "", "isSize": False,
    "isWeddingCatagory": False, "isMultipleCategoryIds": False,
    "isFromHeaderMenuTab": False, "layout": "grid", "isFromHomePage": False,
}


class ShopGoodwillAPI:
    def __init__(self, delay_min: float = 2.0, delay_max: float = 5.0):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self._token: str | None = None
        self._username: str = ""
        self._client = httpx.Client(
            headers={"User-Agent": random.choice(_UAS), **_BROWSER_HEADERS},
            timeout=30.0,
            follow_redirects=True,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._client.close()

    def _delay(self, quick: bool = False) -> None:
        if quick:
            # Lighter delay for simple GETs (item detail, favorites).
            time.sleep(random.uniform(0.5, 1.5))
            return
        # Triangular distribution: most delays near the low end, occasional
        # longer pauses — mimics real browsing cadence better than uniform.
        time.sleep(random.triangular(self.delay_min, self.delay_max + 1.0, self.delay_min))

    @staticmethod
    def _encrypt(val: str) -> str:
        ct = AES.new(_KEY, AES.MODE_CBC, _IV).encrypt(pad(val.encode(), 16))
        return urllib.parse.quote(base64.b64encode(ct))

    def _get_app_version(self) -> str:
        """Extract the current appVersion from the main.js bundle hash."""
        try:
            resp = self._client.get("https://shopgoodwill.com/signin")
            if m := re.search(r'main\.([a-f0-9]+)\.js', resp.text):
                return m.group(1)
        except httpx.HTTPError:
            pass
        return "e249b8153dbb84bc"  # fallback

    def login(self, username: str, password: str) -> bool:
        app_version = self._get_app_version()
        self._delay()
        resp = self._client.post(f"{_BASE}/SignIn/Login", json={
            "browser": self._client.headers.get("User-Agent", ""),
            "remember": False,
            "appVersion": app_version,
            "userName": self._encrypt(username),
            "password": self._encrypt(password),
        })
        data = resp.json()
        if tok := data.get("accessToken"):
            self._token = tok
            self._client.headers["Authorization"] = f"Bearer {tok}"
            log.info("Login OK")
            return True
        log.warning("Login failed: %s", data.get("message", "unknown"))
        return False

    def ensure_auth(self, username: str, password: str) -> bool:
        self._username = username
        if self._token:
            try:
                if self._client.post(f"{_BASE}/SaveSearches/GetSaveSearches").status_code != 401:
                    return True
            except httpx.HTTPError:
                pass
        return self.login(username, password)

    def search(self, keyword: str, category_id: int = 0,
               page: int = 1, page_size: int = 40) -> dict[str, Any]:
        body = {**_SEARCH_DEFAULTS, "searchText": keyword.replace('"', ""),
                "page": page, "pageSize": page_size}
        if category_id:
            body.update(categoryId=category_id, selectedCategoryIds=str(category_id))
        self._delay()
        for attempt in range(3):
            try:
                resp = self._client.post(f"{_BASE}/Search/ItemListing", json=body)
                resp.raise_for_status()
                return resp.json()
            except Exception:
                if attempt == 2:
                    raise
                time.sleep((2 ** attempt) + random.random())
        return {}

    def search_all_pages(self, keyword: str, category_id: int = 0,
                         page_size: int = 40, max_pages: int = 5) -> list[dict]:
        items: list[dict] = []
        for pg in range(1, max_pages + 1):
            res = self.search(keyword, category_id, pg, page_size).get("searchResults", {})
            batch = res.get("items", [])
            if not batch:
                break
            items.extend(batch)
            if len(items) >= res.get("itemCount", 0):
                break
        return items

    def add_to_watchlist(self, item_id: int) -> bool:
        if not self._token:
            return False
        for attempt in range(3):
            self._delay(quick=True)
            try:
                return self._client.get(f"{_BASE}/Favorite/AddToFavorite",
                                        params={"itemId": item_id}).status_code == 200
            except Exception as e:
                if attempt == 2:
                    log.warning("Watchlist failed %d: %s", item_id, e)
                    return False
                time.sleep((2 ** attempt) + random.random())
        return False

    def get_my_bids(self, days_back: int = 180) -> list[dict]:
        """Fetch the user's active bid list ("Auctions In Progress")."""
        if not self._token:
            return []
        for attempt in range(3):
            self._delay(quick=True)
            try:
                resp = self._client.get(
                    f"{_BASE}/Auctions/GetOpenAuctions",
                    params={"daysBack": days_back},
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict):
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        return inner.get("auctionItems", [])
                    if isinstance(inner, list):
                        return inner
                if isinstance(data, list):
                    return data
                return []
            except Exception as e:
                if attempt == 2:
                    log.warning("Open auctions fetch failed: %s", e)
                    return []
                time.sleep((2 ** attempt) + random.random())
        return []

    def get_favorites(self, status: str = "open") -> list[dict]:
        """Fetch watchlisted items.  status: open | close | all"""
        if not self._token:
            return []
        for attempt in range(3):
            self._delay(quick=True)
            try:
                resp = self._client.post(
                    f"{_BASE}/Favorite/GetAllFavoriteItemsByType",
                    params={"Type": status},
                    json={},
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", data) if isinstance(data, dict) else data
            except Exception as e:
                if attempt == 2:
                    log.warning("Favorites fetch failed after 3 attempts: %s", e)
                    return []
                time.sleep((2 ** attempt) + random.random())
        return []

    def get_item_detail(self, item_id: int) -> dict[str, Any]:
        """Fetch full item detail including bid history."""
        for attempt in range(3):
            self._delay(quick=True)
            try:
                resp = self._client.get(
                    f"{_BASE}/itemDetail/GetItemDetailModelByItemId/{item_id}",
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt == 2:
                    log.warning("Item detail failed for %d: %s", item_id, e)
                    return {}
                time.sleep((2 ** attempt) + random.random())
        return {}
