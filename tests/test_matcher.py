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
