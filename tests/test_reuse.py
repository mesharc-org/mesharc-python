"""A repeated scrape or crawl: when it is the same job, and how that is said.

The MCP tools key a scrape and a crawl by their inputs, so a retry -- after a
dropped connection, a timeout, a 429 -- lands on the job the first call made
instead of starting the work twice. The API remembers a key for a day, and the
key was the inputs alone: the same list sent the next morning came back as
yesterday's batch, with nothing saying so (MCP re-test, 11 Nov 2026).

So the key carries a ten-minute window, a read made by an earlier call is
marked `reused` with its age, and force_refresh reads again now.
"""
from datetime import datetime, timedelta, timezone

import pytest

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class _Arc:
    """A client that records the key each call was sent with."""

    def __init__(self, age_s=0.0, rows=None):
        self.keys, self._age, self._rows = [], age_s, rows

    def scrape(self, urls, idempotency_key=None, **_kw):
        self.keys.append(idempotency_key)
        if isinstance(urls, str):
            return {"url": urls, "status": 200, "words": 5, "markdown": "m", "crawledAt": _ago(self._age)}
        rows = self._rows or [{"url": u, "status": "ok", "words": 5, "markdown": "m"} for u in urls]
        return {"id": "b1", "status": "done", "finishedAt": _ago(self._age), "pages": rows}

    def crawl(self, url, idempotency_key=None, **_kw):
        self.keys.append(idempotency_key)
        raise mcp_mod.MeshArcError(418, "stop here: the key is what this test reads")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


@pytest.fixture
def clock(monkeypatch):
    """The wall clock the key's window is read from."""
    now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(mcp_mod.time, "time", lambda: now["t"])
    return now


URLS = ["https://x.test/a", "https://x.test/b"]


def test_the_same_list_within_the_window_is_the_same_job_and_a_new_one_after_it(monkeypatch, clock):
    arc = _Arc()
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    mcp_mod.scrape_urls(URLS)
    clock["t"] += 30
    mcp_mod.scrape_urls(URLS)                          # a retry: the same key, so the same batch
    clock["t"] += 15 * 60
    mcp_mod.scrape_urls(URLS)                          # fifteen minutes on: a new batch
    assert arc.keys[0] == arc.keys[1]
    assert arc.keys[2] != arc.keys[0]


def test_one_url_and_a_crawl_are_keyed_the_same_way(monkeypatch, clock):
    arc = _Arc()
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    mcp_mod.scrape_urls(URLS[:1])
    mcp_mod.crawl_site("https://x.test/")
    clock["t"] += 15 * 60
    mcp_mod.scrape_urls(URLS[:1])
    mcp_mod.crawl_site("https://x.test/")
    scrape_then, crawl_then, scrape_now, crawl_now = arc.keys
    assert scrape_then != scrape_now and crawl_then != crawl_now


def test_force_refresh_is_a_key_of_its_own_every_time(monkeypatch, clock):
    arc = _Arc()
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    mcp_mod.scrape_urls(URLS)
    mcp_mod.scrape_urls(URLS, force_refresh=True)
    mcp_mod.scrape_urls(URLS, force_refresh=True)
    assert len(set(arc.keys)) == 3


def test_a_read_made_by_an_earlier_call_says_so_with_its_age(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(age_s=240))
    for out in (mcp_mod.scrape_urls(URLS), mcp_mod.scrape_urls(URLS[:1])):
        assert out["reused"] is True
        assert 235 <= out["ageSeconds"] <= 250


def test_a_read_made_just_now_is_not_marked_even_with_clocks_a_few_seconds_apart(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(age_s=8))
    for out in (mcp_mod.scrape_urls(URLS), mcp_mod.scrape_urls(URLS[:1])):
        assert "reused" not in out and "ageSeconds" not in out


# --- a url that did not come back ok says why -----------------------------

def test_a_name_that_does_not_resolve_is_told_apart_from_a_404_in_the_index(monkeypatch):
    """Both read `missing`. The reason and the code are what tell them apart,
    and an index row used to carry neither."""
    rows = [{"url": "https://x.test/a", "status": "ok", "words": 5, "markdown": "m", "reason": "", "errorCode": "OK"},
            {"url": "https://no-such-host.test/", "status": "missing", "words": 0,
             "reason": "no-such-host.test does not resolve: no such site", "errorCode": "DNS_FAILED"},
            {"url": "https://x.test/gone", "status": "missing", "words": 0, "reason": "HTTP 404", "errorCode": "NOT_FOUND"},
            {"url": "https://x.test/robots", "status": "robots", "error": "robots.txt disallows this url"}]
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(rows=rows))
    index = {r["url"]: r for r in mcp_mod.scrape_urls([r["url"] for r in rows])["index"]}
    assert "reason" not in index["https://x.test/a"] and "errorCode" not in index["https://x.test/a"]
    assert index["https://no-such-host.test/"]["errorCode"] == "DNS_FAILED"
    assert "does not resolve" in index["https://no-such-host.test/"]["reason"]
    assert index["https://x.test/gone"]["errorCode"] == "NOT_FOUND"
    assert index["https://x.test/robots"]["reason"] == "robots.txt disallows this url"
