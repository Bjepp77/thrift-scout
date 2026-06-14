from __future__ import annotations

import re
from functools import lru_cache

from thrift_scout.config import Target

SIZE_ALIASES: dict[str, list[str]] = {
    "XS": ["XS", "X-Small", "Extra Small", "X Small", "XSM"],
    "S": ["S", "Small", "SM", "SML"],
    "M": ["M", "Medium", "Med", "MD"],
    "L": ["L", "Large", "LG", "LRG"],
    "XL": ["XL", "X-Large", "Extra Large", "X Large", "XLG", "1XL", "1X"],
    "XXL": ["XXL", "XX-Large", "2XL", "2X", "XX Large"],
    "XXXL": ["XXXL", "XXX-Large", "3XL", "3X", "XXX Large"],
}

# Precomputed reverse map: "EXTRA LARGE" → {"XL","X-Large","Extra Large",...}
_REV: dict[str, set[str]] = {}
for _aliases in SIZE_ALIASES.values():
    _s = set(_aliases)
    for _a in _aliases:
        _REV[_a.upper()] = _s

_GENDER_EXCL = {
    "mens": ["women's", "womens", "women"],
    "womens": ["men's", "mens"],
}


@lru_cache(maxsize=256)
def _size_re(s: str) -> re.Pattern[str]:
    e = re.escape(s)
    if re.match(r"^\d+([./]\d+)?$", s):
        return re.compile(rf"(?<!\d){e}(?!\d)", re.I)
    if re.match(r"^[A-Za-z]{1,3}$", s):
        return re.compile(rf"(?<![A-Za-z]){e}(?![A-Za-z])", re.I)
    return re.compile(rf"\b{e}\b", re.I)


def _expand_sizes(sizes: list[str]) -> list[str]:
    out: set[str] = set(sizes)
    for s in sizes:
        if hit := _REV.get(s.upper().strip()):
            out.update(hit)
    return list(out)


def match_brand(title: str, aliases: list[str]) -> str | None:
    tl = title.lower()
    return next((a for a in aliases if a.lower() in tl), None)


def match_size(title: str, sizes: list[str]) -> str | None:
    if not sizes:
        return ""
    return next((s for s in _expand_sizes(sizes) if _size_re(s).search(title)), None)


def check_exclusions(title: str, exclusions: list[str]) -> str | None:
    tl = title.lower()
    return next((e for e in exclusions if e.lower() in tl), None)


def match_item(item: dict, target: Target) -> dict[str, str] | None:
    title = item.get("title", "")
    excl = target.exclude + _GENDER_EXCL.get(target.gender.lower().strip(), [])

    if check_exclusions(title, excl):
        return None

    if target.max_price is not None:
        try:
            if float(item.get("currentPrice") or item.get("minimumBid") or 0) > target.max_price:
                return None
        except (TypeError, ValueError):
            pass

    brand = match_brand(title, target.aliases)
    if not brand:
        return None

    if target.match_mode == "keyword_pair":
        return {"brand_matched": brand, "size_matched": "", "match_mode": "keyword_pair"}

    size = match_size(title, target.sizes)
    if size is None:
        return None
    return {"brand_matched": brand, "size_matched": size, "match_mode": "brand_size"}
