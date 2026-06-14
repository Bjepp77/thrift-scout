from __future__ import annotations

import base64
import logging
import random
import time
import urllib.parse
from typing import Any

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

log = logging.getLogger(__name__)

_BASE = "https://buyerapi.shopgoodwill.com/api"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
_KEY = b"6696D2E6F042FEC4D6E3F32AD541143B"
_IV = b"0000000000000000"

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
        self._client = httpx.Client(
            headers={"User-Agent": _UA}, timeout=30.0, follow_redirects=True,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._client.close()

    def _delay(self) -> None:
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    @staticmethod
    def _encrypt(val: str) -> str:
        ct = AES.new(_KEY, AES.MODE_CBC, _IV).encrypt(pad(val.encode(), 16))
        return urllib.parse.quote(base64.b64encode(ct))

    def login(self, username: str, password: str) -> bool:
        try:
            self._client.get("https://shopgoodwill.com/signin")
            self._delay()
        except httpx.HTTPError:
            pass
        data = self._client.post(f"{_BASE}/SignIn/Login", json={
            "browser": "firefox", "remember": False,
            "clientIpAddress": "0.0.0.4", "appVersion": "00099a1be3bb023ff17d",
            "username": self._encrypt(username),
            "password": self._encrypt(password),
        }).json()
        if tok := data.get("accessToken"):
            self._token = tok
            self._client.headers["Authorization"] = f"Bearer {tok}"
            log.info("Login OK")
            return True
        log.warning("Login failed: %s", data.get("message", "?"))
        return False

    def ensure_auth(self, username: str, password: str) -> bool:
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
            except httpx.HTTPError:
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
        try:
            self._delay()
            return self._client.get(f"{_BASE}/Favorite/AddToFavorite",
                                    params={"itemId": item_id}).status_code == 200
        except httpx.HTTPError as e:
            log.warning("Watchlist failed %d: %s", item_id, e)
            return False
