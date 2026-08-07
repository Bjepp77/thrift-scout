"""Digest cap, fail-closed dedup, and the API contract canary."""
import pytest

from thrift_scout import run as run_mod
from thrift_scout.run import _cap_matches, _contract_errors


def _item(iid, end):
    return {"item_id": iid, "title": f"item {iid}", "end_time": end}


# ── Digest cap ──

def test_cap_is_a_noop_under_the_limit():
    m = {"A": [_item(1, "2026-09-01")]}
    out, deferred = _cap_matches(m, 50)
    assert out is m and deferred == 0


def test_cap_keeps_the_soonest_ending_items_across_brands():
    m = {
        "A": [_item(1, "2026-09-03"), _item(2, "2026-09-05")],
        "B": [_item(3, "2026-09-01"), _item(4, "2026-09-02")],
    }
    out, deferred = _cap_matches(m, 2)
    assert deferred == 2
    assert sum(len(v) for v in out.values()) == 2
    kept = {i["item_id"] for v in out.values() for i in v}
    assert kept == {3, 4}, "cap must keep the most urgent, not the first brand"


def test_cap_preserves_brand_grouping():
    m = {"A": [_item(1, "2026-09-01")], "B": [_item(2, "2026-09-02")]}
    out, deferred = _cap_matches(m, 1)
    assert deferred == 1 and list(out) == ["A"]


def test_cap_of_zero_or_less_disables_capping():
    m = {"A": [_item(i, f"2026-09-0{i}") for i in range(1, 5)]}
    out, deferred = _cap_matches(m, 0)
    assert deferred == 0 and sum(len(v) for v in out.values()) == 4


# ── Contract canary ──

def test_contract_flags_a_total_absence_of_results():
    problems = _contract_errors({("a", 0): [], ("b", 0): []}, 2)
    assert problems and "contract change" in problems[0]


def test_contract_flags_missing_fields():
    cache = {("a", 0): [{"itemId": 1, "title": "x"}]}      # no endTime
    problems = _contract_errors(cache, 1)
    assert problems and "endTime" in problems[0]


def test_contract_is_quiet_on_a_healthy_response():
    cache = {("a", 0): [{"itemId": 1, "title": "x", "endTime": "2026-09-01"}]}
    assert _contract_errors(cache, 1) == []


def test_contract_does_not_fire_when_no_searches_ran():
    assert _contract_errors({}, 0) == []


# ── Fail-closed dedup ──

class _ExplodingStore:
    def __init__(self):
        self.marked = []
        self.logged = None
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get_seen(self, profile): raise RuntimeError("supabase unreachable")
    def mark_batch_seen(self, profile, items): self.marked.append(items)
    def purge_old(self, days=30): pass
    def log_run(self, f, n, w, e): self.logged = (f, n, w, e)


class _API:
    def __init__(self, *a, **k): self.watchlisted = []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def ensure_auth(self, u, p): return True
    def get_my_bids(self, days_back=180): return []
    def get_favorites(self, status="open"): return []
    def search_all_pages(self, term, cat=0, page_size=40, max_pages=5):
        return [{"itemId": i, "title": "Patagonia Fleece XL", "currentPrice": 5.0,
                 "endTime": "2026-09-01T00:00:00", "imageURL": ""} for i in range(200)]
    def add_to_watchlist(self, iid):
        self.watchlisted.append(iid); return True


def test_dedup_failure_skips_the_profile_instead_of_sending_everything(monkeypatch):
    from thrift_scout.config import Config, Profile, Target
    store, api = _ExplodingStore(), _API()
    sent = []
    monkeypatch.setattr(run_mod, "Store", lambda *a, **k: store)
    monkeypatch.setattr(run_mod, "ShopGoodwillAPI", lambda *a, **k: api)
    monkeypatch.setattr(run_mod, "send_email",
                        lambda subject, html, cfg, to: sent.append(subject) or True)

    cfg = Config(profiles=[Profile(name="Brandon", email="b@example.com", targets=[
        Target(brand="Patagonia", aliases=["Patagonia"], sizes=["XL"])])],
        email_sender="s@example.com", email_password="pw",
        sgw_username="u", sgw_password="p")
    run_mod._execute(cfg, None)

    assert store.marked == [], "a dedup outage must not persist anything"
    assert api.watchlisted == [], "a dedup outage must not touch the watchlist"
    assert sent == ["Thrift Scout: Scan skipped (dedup unavailable)"]
    assert any("Dedup unavailable" in e for e in store.logged[3])
