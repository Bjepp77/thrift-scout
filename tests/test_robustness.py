"""Guards for the failure modes that make an unattended job go quiet.

Each test here corresponds to a way this scanner previously could have lost
data or reported success while degraded.
"""
import pytest

from thrift_scout.api import ShopGoodwillAPI
from thrift_scout.config import Target
from thrift_scout.email_report import prepend_error_banner, render_error_report
from thrift_scout.matcher import match_item


# ── Config validation: silent over-matching ──

def test_brand_size_without_sizes_is_rejected():
    # This used to match every listing carrying the brand, with no warning.
    with pytest.raises(ValueError, match="requires sizes"):
        Target(brand="Patagonia", aliases=["Patagonia"])


def test_brand_only_is_the_explicit_opt_in():
    t = Target(brand="Sendero Hat", aliases=["Sendero"], match_mode="brand_only")
    assert match_item({"title": "Sendero Hat Olive One Size"}, t)


def test_empty_aliases_is_rejected():
    with pytest.raises(ValueError, match="aliases must not be empty"):
        Target(brand="Ghost", aliases=[], sizes=["XL"])


def test_unknown_match_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown match_mode"):
        Target(brand="Ghost", aliases=["Ghost"], sizes=["XL"], match_mode="typo")


# ── Pagination: a missing itemCount must not cap the search at one page ──

class _FakeAPI(ShopGoodwillAPI):
    def __init__(self, pages):
        self._pages = pages
        self.calls = 0

    def _delay(self, quick=False):
        pass

    def search(self, keyword, category_id=0, page=1, page_size=40):
        self.calls += 1
        return self._pages[page - 1] if page - 1 < len(self._pages) else {}


def _page(n, item_count=None, page_size=40):
    res = {"items": [{"itemId": i} for i in range(n)]}
    if item_count is not None:
        res["itemCount"] = item_count
    return {"searchResults": res}


def test_pagination_continues_when_item_count_is_absent():
    api = _FakeAPI([_page(40), _page(40), _page(5)])
    items = api.search_all_pages("x", page_size=40, max_pages=5)
    assert len(items) == 85
    assert api.calls == 3          # stopped on the short page, not page 1


def test_pagination_stops_on_a_trustworthy_item_count():
    api = _FakeAPI([_page(40, item_count=40)])
    assert len(api.search_all_pages("x", page_size=40, max_pages=5)) == 40
    assert api.calls == 1


def test_pagination_respects_max_pages():
    api = _FakeAPI([_page(40) for _ in range(10)])
    assert len(api.search_all_pages("x", page_size=40, max_pages=3)) == 120
    assert api.calls == 3


# ── Error surfacing ──

def test_error_banner_is_injected_after_body_tag():
    html = "<html><body style='x'><div>report</div></body></html>"
    out = prepend_error_banner(html, ["Search error (KUHL): timeout"])
    assert "results may be incomplete" in out
    assert out.index("Search error") < out.index("<div>report</div>")
    assert out.startswith("<html><body")


def test_error_banner_is_a_noop_without_errors():
    html = "<html><body></body></html>"
    assert prepend_error_banner(html, []) == html


def test_error_text_is_escaped():
    out = render_error_report(["boom <script>alert(1)</script> & <b>"])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_error_banner_escapes_too():
    out = prepend_error_banner("<html><body></body></html>", ["<img src=x>"])
    assert "<img src=x>" not in out
    assert "&lt;img" in out
