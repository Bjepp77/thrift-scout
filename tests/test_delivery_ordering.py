"""The scanner must never mark an item seen unless it was actually delivered.

Marking first meant a failed SMTP call buried that day's finds permanently:
recorded as reported, skipped by every later run, never seen by anyone.
"""
import pytest

from thrift_scout import run as run_mod
from thrift_scout.config import Config, Profile, Target


class _FakeStore:
    def __init__(self):
        self.marked = []
        self.logged = None

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get_seen(self, profile): return set(), set()
    def mark_batch_seen(self, profile, items): self.marked.append((profile, items))
    def purge_old(self, days=30): pass
    def log_run(self, f, n, w, e): self.logged = (f, n, w, e)


class _FakeAPI:
    def __init__(self, *a, **k): self.watchlisted = []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def ensure_auth(self, u, p): return True
    def get_my_bids(self, days_back=180): return []
    def get_favorites(self, status="open"): return []
    def search_all_pages(self, term, cat=0, page_size=40, max_pages=5):
        return [{"itemId": 1, "title": "Patagonia Fleece XL", "currentPrice": 10.0,
                 "endTime": "2026-09-01T00:00:00", "imageURL": ""}]
    def add_to_watchlist(self, iid):
        self.watchlisted.append(iid); return True


def _config():
    return Config(profiles=[Profile(name="Brandon", email="b@example.com", targets=[
        Target(brand="Patagonia", aliases=["Patagonia"], sizes=["XL"])])],
        email_sender="s@example.com", email_password="pw",
        sgw_username="u", sgw_password="p")


@pytest.fixture
def wired(monkeypatch):
    store = _FakeStore()
    api = _FakeAPI()
    monkeypatch.setattr(run_mod, "Store", lambda *a, **k: store)
    monkeypatch.setattr(run_mod, "ShopGoodwillAPI", lambda *a, **k: api)
    return store, api


def test_delivery_failure_does_not_mark_items_seen(wired, monkeypatch):
    store, api = wired
    monkeypatch.setattr(run_mod, "send_email", lambda *a, **k: False)

    run_mod._execute(_config(), None)

    assert store.marked == [], "items were marked seen despite the email failing"
    assert api.watchlisted == [], "watchlist was touched for an undelivered digest"
    assert any("Email delivery failed" in e for e in store.logged[3])


def test_successful_delivery_marks_items_seen(wired, monkeypatch):
    store, api = wired
    monkeypatch.setattr(run_mod, "send_email", lambda *a, **k: True)

    run_mod._execute(_config(), None)

    assert len(store.marked) == 1
    profile, items = store.marked[0]
    assert profile == "Brandon"
    assert [i["item_id"] for i in items] == [1]
    assert api.watchlisted == [1]


def test_preview_never_persists(wired, monkeypatch, tmp_path):
    store, api = wired
    monkeypatch.setattr(run_mod, "send_email", lambda *a, **k: True)

    run_mod._execute(_config(), str(tmp_path / "out"))

    assert store.marked == [], "a preview run wrote to the seen table"
    assert api.watchlisted == []
