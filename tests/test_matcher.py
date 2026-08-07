"""Unit tests for the matching engine and bid-detection helpers."""
from thrift_scout.config import Target
from thrift_scout.matcher import check_exclusions, match_brand, match_item, match_size, match_username

# ── Brand ──

def test_brand_exact():
    assert match_brand("Patagonia Better Sweater Fleece", ["Patagonia"]) == "Patagonia"

def test_brand_case_insensitive():
    assert match_brand("patagonia zip jacket", ["Patagonia"]) == "Patagonia"

def test_brand_alias():
    assert match_brand("Doc Martens 1460 Boot Sz 11", ["Dr. Martens", "Doc Martens"]) == "Doc Martens"

def test_brand_miss():
    assert match_brand("Nike Air Force 1", ["Patagonia"]) is None

# ── Size ──

def test_size_xl():
    assert match_size("Patagonia Fleece Jacket Mens XL", ["XL", "X-Large"]) is not None

def test_size_alias_expansion():
    assert match_size("Patagonia Extra Large Fleece", ["XL"]) is not None

def test_size_no_false_positive_in_word():
    assert match_size("Excellent Condition Jacket", ["L"]) is None

def test_size_shoe_number():
    assert match_size("Doc Martens 1460 Boot Size 11 Black", ["11"]) is not None

def test_size_shoe_no_false_positive():
    assert match_size("Nike Air Max 110 Sneaker", ["11"]) is None

def test_size_waist():
    assert match_size("Lululemon ABC Pant 36 Obsidian", ["36", "W36"]) is not None

def test_size_fractional():
    assert match_size("Blundstone Chelsea Boot 11.5", ["11.5"]) is not None

def test_size_no_requirement():
    assert match_size("MoonSwatch Mission to Mars", []) == ""

# ── Exclusions ──

def test_exclusion_hit():
    assert check_exclusions("Kids Patagonia Fleece XL Youth", ["kids", "youth"]) == "kids"

def test_exclusion_miss():
    assert check_exclusions("Patagonia Fleece XL Mens", ["kids", "youth"]) is None

def test_exclusion_no_substring_false_positive():
    """'mens' must not match inside 'womens', 'men's' must not match inside 'women's'."""
    assert check_exclusions("Vuori Womens Jogger Medium", ["mens"]) is None
    assert check_exclusions("Vuori Women's Jogger M", ["men's"]) is None

# ── Full match_item ──

_PAT = Target(brand="Patagonia", aliases=["Patagonia"], sizes=["XL", "X-Large", "Extra Large"],
              gender="mens", exclude=["kids", "youth", "toddler", "girls", "boys", "damaged", "stained"])
_MW = Target(brand="Omega x Swatch", aliases=["Omega x Swatch", "Omega Swatch", "MoonSwatch", "Moon Swatch"],
             sizes=[], match_mode="keyword_pair", exclude=["strap only", "band only", "box only", "replica", "fake"])

def test_match_patagonia():
    r = match_item({"title": "Patagonia Better Sweater Fleece Jacket Mens XL Blue", "itemId": 1}, _PAT)
    assert r and r["brand_matched"] == "Patagonia" and r["match_mode"] == "brand_size"

def test_match_excludes_kids():
    assert match_item({"title": "Patagonia Kids Fleece XL Youth", "itemId": 2}, _PAT) is None

def test_match_wrong_size():
    assert match_item({"title": "Patagonia Better Sweater Fleece Mens Small", "itemId": 3}, _PAT) is None

def test_match_keyword_pair():
    r = match_item({"title": "Omega Swatch MoonSwatch Mission to Mars", "itemId": 4}, _MW)
    assert r and r["match_mode"] == "keyword_pair"

def test_match_keyword_pair_excluded():
    assert match_item({"title": "MoonSwatch Strap Only Replacement Band", "itemId": 5}, _MW) is None

def test_match_gender_filter():
    assert match_item({"title": "Patagonia Women's Fleece XL", "itemId": 8}, _PAT) is None

def test_price_cap():
    t = Target(brand="Test", aliases=["Test"], sizes=[], max_price=25.0)
    assert match_item({"title": "Test Item", "currentPrice": 30.0, "itemId": 6}, t) is None
    assert match_item({"title": "Test Item", "currentPrice": 20.0, "itemId": 7}, t) is not None


# ── Username obfuscation matching ──

def test_username_match_exact():
    assert match_username("brandonjeppson7", "brandonjeppson7")

def test_username_match_obfuscated_prefix_suffix():
    # "bran****on7" → prefix "bran", suffix "on7"
    assert match_username("brandonjeppson7", "bran****on7")

def test_username_match_single_char_prefix():
    # "b****7" → prefix "b", suffix "7"
    assert match_username("brandonjeppson7", "b****7")

def test_username_match_case_insensitive():
    assert match_username("BrandonJeppson7", "bran****on7")

def test_username_no_match_wrong_prefix():
    assert not match_username("brandonjeppson7", "zran****on7")

def test_username_no_match_wrong_suffix():
    assert not match_username("brandonjeppson7", "bran****xyz")

def test_username_no_match_no_asterisks():
    assert not match_username("brandonjeppson7", "otherperson")

def test_username_no_match_empty():
    assert not match_username("", "b****n")
    assert not match_username("brandon", "")

def test_username_no_match_short_username():
    # Username too short to contain both prefix and suffix
    assert not match_username("ab", "abc****xyz")


# ── Decimal-adjacency regression (2026-08-06) ──
#
# A numeric size used to be guarded with (?<!\d)...(?!\d). A "." is not a
# digit, so size "5" matched the tail of "Size 6.5" and size "9" matched the
# head of "9.5Y". On the kids-shoe targets that produced 64 wrong matches out
# of 77 in two days, most of which were auto-added to a real ShopGoodwill
# watchlist. Titles below are taken verbatim from those bad matches.

def test_size_5_does_not_match_other_half_sizes():
    for title in ["Shoes Size 6.5", "Sneakers Size 4.5", "Shoes Size 2.5",
                  "Shoes Size 3.5", "Nike Shoes Sz 7.5", "Running Shoes Size 12.5 M"]:
        assert match_size(title, ["5"]) is None, title

def test_size_9_does_not_match_half_or_child_sizes():
    assert match_size("Sneakers Sz 9.5Y", ["9"]) is None
    assert match_size("Basketball Shoes Size 19", ["9"]) is None

def test_size_still_matches_its_own_decimal():
    assert match_size("Blundstone Boots Size 11.5", ["11.5"]) == "11.5"
    assert match_size("Vuori Trainer EU 44 Olive", ["44"]) == "44"

def test_size_does_not_match_longer_number():
    assert match_size("Shoes Size 15", ["5"]) is None
    assert match_size("Waist 36.5 Pants", ["36"]) is None


# ── Kids sizes require a youth marker ──

_GIRLS_5 = Target(brand="Nike Girls Shoes", aliases=["Nike"],
                  sizes=["5Y", "Youth 5", "Big Kid 5", "Little Kid 5"],
                  exclude=["boys", "mens", "womens", "ladies", "lot", "bundle"])
_BOYS_9 = Target(brand="Jordan Boys Shoes", aliases=["Jordan"],
                 sizes=["9Y", "Youth 9", "Big Kid 9", "Little Kid 9"],
                 exclude=["girls", "mens", "womens", "ladies", "lot", "bundle"])


def test_girls_5_accepts_real_youth_five():
    assert match_item(
        {"title": "Nike Air Force 1 High Triple Black Girls Shoes Size 5Y"}, _GIRLS_5)


def test_girls_5_rejects_real_world_false_positives():
    for title in [
        "Adidas Girls Continental 80 Red Lace-Up Low Top Sneaker Shoes Size 6.5",
        "Nike Girls Air Force 1 'white Aquarius Blue Size 2.5 Shoes",
        "Girls Multi Color Nike Shoes Sz 7.5",
        "Girls' Kids' Jordan 12 Retro Floral (GS) White/Black+ Shoes Sneakers Size 5.5Y",
    ]:
        assert match_item({"title": title}, _GIRLS_5) is None, title


def test_boys_9_rejects_model_numbers_and_child_widths():
    # "Air Jordan 9" is a model name, and 9C is a toddler width, not youth 9.
    for title in [
        "Nike Boys Air Jordan 9 302359-103 White Blue Black Basketball Shoes Size 4Y",
        "Nike Baby Boys Air Jordan Zion 1 DC2023-004 Blue Black Sneakers Shoes Size 9C",
        "Nike Boys Air Jordan Flight Club 91 Black Purple Lace-Up Sneakers Shoes Sz 9.5Y",
    ]:
        assert match_item({"title": title}, _BOYS_9) is None, title


def test_boys_9_accepts_real_youth_nine():
    assert match_item({"title": "Jordan Boys Big Kid 9 Sneakers"}, _BOYS_9)
