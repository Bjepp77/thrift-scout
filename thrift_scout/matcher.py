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
    "mens": ["women's", "womens", "women", "ladies", "lady's", "girls"],
    # Bare "men" was missing, so "vuori ... jacket men size M" landed in a
    # womens digest. It is safe to add: exclusions match on word boundaries,
    # and the "men" inside "women" has no boundary before it.
    "womens": ["men's", "mens", "men", "boys"],
}

# Multi-item lots are never what a size-specific target is looking for, and
# sellers write them a dozen ways: "Lot of 3", "3 Item Lot|", "Lot Watches",
# "7pc ... Lot". Bare "lot" catches them all and is safe because exclusions
# match on word boundaries, so "Camelot" and "pilot" do not trip it. Bare
# "set" is deliberately absent: a two-piece set can be a legitimate find.
_LOT_EXCL = ["lot", "bundle"]


@lru_cache(maxsize=256)
def _size_re(s: str) -> re.Pattern[str]:
    e = re.escape(s)
    if re.match(r"^[A-Za-z]{1,3}$", s):
        # Block an adjacent digit as well as an adjacent letter. A leading
        # digit is what separates XL from 2XL and 3XL, and SIZE_ALIASES
        # already treats those as XXL and XXXL — the guard just never
        # enforced it, so every XL target quietly collected 2XL and 3XL.
        return re.compile(rf"(?<![A-Za-z\d]){e}(?![A-Za-z\d])", re.I)
    if any(c.isdigit() for c in s):
        # Guard against a decimal point as well as a digit on either side.
        # A bare \d guard is not enough: "." is not a digit, so size "5"
        # happily matched the tail of "Size 6.5" / "4.5" / "2.5" / "7.5",
        # and size "9" matched the head of "9.5Y". That single gap produced
        # 64 wrong matches out of 77 on the kids-shoe targets in two days.
        return re.compile(rf"(?<![\d.]){e}(?![\d.])", re.I)
    return re.compile(rf"\b{e}\b", re.I)


@lru_cache(maxsize=64)
def _expand_sizes(sizes: tuple[str, ...]) -> tuple[str, ...]:
    out: set[str] = set(sizes)
    for s in sizes:
        if hit := _REV.get(s.upper().strip()):
            out.update(hit)
    return tuple(out)


_WS = re.compile(r"\s+")


def norm_title(title: str) -> str:
    """Canonical form of a listing title, used to spot the same thing twice.

    ShopGoodwill relists unsold lots on a weekly cycle and each relist gets a
    fresh itemId, so id-only dedup reports the same item as new every week.
    Roughly a quarter of everything stored had been reported before under a
    different id.
    """
    return _WS.sub(" ", title.strip().lower())


@lru_cache(maxsize=256)
def _alias_re(a: str) -> re.Pattern[str]:
    # A plain substring test let "Mathey-Tissot" satisfy the "Tissot" target,
    # and \b would not have helped: a hyphen is a word boundary. Require that
    # neither side is a word character *or* a hyphen, so hyphenated compound
    # brands stay distinct while "TISSOT 534657" still matches.
    return re.compile(rf"(?<![\w-]){re.escape(a)}(?![\w-])", re.I)


def match_brand(title: str, aliases: list[str]) -> str | None:
    return next((a for a in aliases if _alias_re(a).search(title)), None)


def match_size(title: str, sizes: list[str]) -> str | None:
    if not sizes:
        return ""
    return next((s for s in _expand_sizes(tuple(sizes)) if _size_re(s).search(title)), None)


def check_exclusions(title: str, exclusions: list[str]) -> str | None:
    tl = title.lower()
    return next(
        (e for e in exclusions
         if re.search(rf'\b{re.escape(e.lower())}\b', tl)),
        None,
    )


def match_username(username: str, obfuscated_name: str) -> bool:
    """Check if an obfuscated bidder name (e.g. 'bran****son') matches a username.

    ShopGoodwill masks the middle of usernames with asterisks, preserving
    a visible prefix and suffix.  We extract those and compare against the
    full username — far fewer false positives than single-char matching.
    """
    if not username or not obfuscated_name:
        return False
    u = username.lower()
    o = obfuscated_name.lower().strip()
    if u == o:
        return True
    if o.count("*") < 3:
        return False
    star_start = o.index("*")
    star_end = o.rindex("*")
    prefix = o[:star_start]
    suffix = o[star_end + 1:]
    if not prefix or not suffix:
        return False
    return (
        u.startswith(prefix)
        and u.endswith(suffix)
        and len(u) >= len(prefix) + len(suffix)
    )


def match_item(item: dict, target: Target) -> dict[str, str] | None:
    title = item.get("title", "")
    excl = (target.exclude
            + _GENDER_EXCL.get(target.gender.lower().strip(), [])
            + _LOT_EXCL)

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

    if target.match_mode in ("keyword_pair", "brand_only"):
        return {"brand_matched": brand, "size_matched": "", "match_mode": target.match_mode}

    size = match_size(title, target.sizes)
    if size is None:
        return None
    return {"brand_matched": brand, "size_matched": size, "match_mode": "brand_size"}
