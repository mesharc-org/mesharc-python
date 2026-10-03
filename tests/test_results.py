"""What a multi-page result is allowed to weigh.

A finished two-hundred page crawl came back at 2.2 million characters: every
outbound link of every page passed through untouched (a megabyte of it, more
than all the markdown together), and a twelve-thousand character cap applied
fifty times over. No client would load it and no context window would hold it.

So a multi-page result is now a map with samples on it -- an index of every
page, an excerpt of the first fifty, a link count in place of the links -- and
anything more is asked for: one page in full, or the next window of pages.
Single-page tools are untouched; asking for one page still gets the page.
"""
import json

import pytest

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")


class _Job:
    """A finished crawl, with as many pages as the test wants."""

    def __init__(self, pages, envelope=None):
        self.id = "c1"
        self._pages = pages
        self.envelope = {"status": "done", "url": "https://x.test/",
                         "counts": {"ok": len(pages)}, "stop": "page budget reached",
                         **(envelope or {})}
        self.asked = []

    def pages(self, limit=25, wait=True, cursor=None, **_kw):
        self.asked.append({"limit": limit, "wait": wait, "cursor": cursor})
        start = int(cursor or 0)
        yield from self._pages[start:]

    def page(self, url):
        for p in self._pages:
            if p["url"] == url:
                return dict(p)
        raise AssertionError("asked for a page that is not in this crawl")


def _page(i, links=200, chars=20_000):
    return {"url": f"https://x.test/{i}",
            "head": {"title": f"Page {i}", "description": "d" * 400},
            "status": 200, "words": 1500,
            "links": [{"href": f"https://x.test/{i}/{j}", "text": "t" * 30} for j in range(links)],
            "markdown": "m" * chars,
            # The diagnostic fields an assistant has no use for.
            "fingerprint": "f" * 64, "contentHash": "h" * 64, "noiseRemoved": 1234,
            "fields": {"a": "b"}, "signals": {"blocked": False}, "htmlBytes": 98765}


def test_fifty_fat_pages_stay_inside_the_budget():
    job = _Job([_page(i) for i in range(50)])
    out = mcp_mod._crawl_result(job, job.pages())
    size = len(json.dumps(out))
    assert size <= 65_000, f"{size} characters is more than a client will load"

    # The links are the single biggest thing that was in here.
    assert all(isinstance(p["links"], int) for p in out["pages"])
    assert "href" not in json.dumps(out), "not one link list survived"

    # And the diagnostics are gone with them.
    for junk in ("fingerprint", "contentHash", "noiseRemoved", "signals", "htmlBytes", "description"):
        assert junk not in json.dumps(out), junk

    # Every page is still accounted for, and every cut excerpt says how to
    # read the rest of it.
    assert len(out["index"]) == 50
    assert out["index"][7] == {"url": "https://x.test/7", "title": "Page 7",
                               "words": 1500, "status": 200}
    assert all("more characters" in p["markdown"] for p in out["pages"])
    assert all("get_job" in p["markdown"] for p in out["pages"])
    assert "url=<a page's url>" in out["note"]


def test_a_small_crawl_is_not_rationed():
    """Three pages is not the problem, and three pages should not be cut."""
    job = _Job([_page(i, links=2, chars=8_000) for i in range(3)])
    out = mcp_mod._crawl_result(job, job.pages())
    assert [len(p["markdown"]) for p in out["pages"]] == [8_000] * 3
    assert "more characters" not in json.dumps(out["pages"])
    assert out["note"] == ""


def test_the_index_covers_pages_the_excerpts_do_not():
    job = _Job([_page(i, links=1, chars=100) for i in range(120)])
    out = mcp_mod._crawl_result(job, job.pages())
    assert len(out["pages"]) == mcp_mod.PAGES_CAP
    assert len(out["index"]) == 120, "an assistant has to know what exists, not just what it was shown"
    assert "cursor=" in out["note"]


def test_the_index_itself_is_bounded():
    job = _Job([_page(i, links=1, chars=10) for i in range(mcp_mod.INDEX_CAP + 200)])
    out = mcp_mod._crawl_result(job, job.pages())
    assert len(out["index"]) == mcp_mod.INDEX_CAP


def test_an_excerpt_is_never_cut_to_nothing():
    """The floor. A budget divided by enough pages reaches zero, and fifty
    empty strings are worse than fifty short ones."""
    assert mcp_mod._excerpt_cap(5_000, 0) == mcp_mod.EXCERPT_FLOOR
    assert mcp_mod._excerpt_cap(50, 5_000) == (mcp_mod.RESULT_BUDGET - 5_000) // 50
    assert mcp_mod._excerpt_cap(3, 0) == mcp_mod.MARKDOWN_CAP, "a short crawl gets whole pages"
    assert mcp_mod._excerpt_cap(0, 0) == mcp_mod.MARKDOWN_CAP


def test_the_next_window_is_offered_and_resumed_from():
    job = _Job([_page(i, links=1, chars=100) for i in range(80)],
               envelope={"next": "https://api.test/api/v1/crawl/c1?cursor=abc123&limit=50"})
    out = mcp_mod._crawl_result(job, job.pages())
    assert out["cursor"] == "abc123"


def test_a_batch_is_shaped_like_a_crawl():
    b = {"id": "b1", "status": "done", "pages": [_page(i) for i in range(50)]}
    out = mcp_mod._batch_result(b)
    assert len(json.dumps(out)) <= 65_000
    assert all(isinstance(p["links"], int) for p in out["pages"])
    assert len(out["index"]) == 50
    assert "extract_url" in out["pages"][0]["markdown"]


def test_one_page_asked_for_alone_is_not_rationed():
    """The single-page tools are the other half of this: the excerpts are
    short because asking for a page is cheap and exact."""
    full = mcp_mod._trim_page(_page(0, links=3, chars=500))
    assert full["markdown"] == "m" * 500
    assert isinstance(full["links"], list), "one page keeps its links"
    assert full["fingerprint"] == "f" * 64


# --- the two ways to drill in --------------------------------------------

class _Arc:
    """Enough of a client for the tools that go through one."""

    def __init__(self, job=None, page=None):
        self._job, self._page = job, page

    def get_crawl(self, _id):
        return self._job

    def scrape(self, url, **_kw):
        return dict(self._page, url=url)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def test_one_page_of_a_crawl_comes_back_in_full(monkeypatch):
    job = _Job([_page(i) for i in range(50)])
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(job))
    out = mcp_mod.get_job("crawl", "c1", url="https://x.test/3")
    assert out["url"] == "https://x.test/3"
    # Capped as a single page is, which is twelve thousand and not the
    # excerpt's share of the budget.
    assert out["markdown"].startswith("m" * mcp_mod.MARKDOWN_CAP)
    assert "links" not in out, "a reader asking for a page does not want its outbound links"
    assert out["head"]["title"] == "Page 3", "the whole head, not just the title"


def test_a_page_is_readable_before_the_crawl_finishes(monkeypatch):
    """Pages already crawled are stored, so there is no reason to make an
    assistant wait for the whole crawl to read one of them."""
    job = _Job([_page(0)], envelope={"status": "running"})
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(job))
    out = mcp_mod.get_job("crawl", "c1", url="https://x.test/0")
    assert out["status"] == 200 and out["markdown"]


def test_the_cursor_returns_the_next_window(monkeypatch):
    job = _Job([_page(i, links=1, chars=100) for i in range(80)])
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(job))
    out = mcp_mod.get_job("crawl", "c1", cursor="60")
    assert job.asked[-1]["cursor"] == "60", "the cursor has to reach the walk"
    assert [r["url"] for r in out["index"]] == [f"https://x.test/{i}" for i in range(60, 80)]
    assert out["pages"][0]["url"] == "https://x.test/60"


def test_a_cursor_reads_a_running_crawl_rather_than_reporting_it(monkeypatch):
    """Without a cursor, a running crawl answers "running". With one, the
    caller is paging through what has landed and wants the pages."""
    job = _Job([_page(i, links=1, chars=100) for i in range(10)],
               envelope={"status": "running"})
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(job))
    assert mcp_mod.get_job("crawl", "c1")["status"] == "running"
    out = mcp_mod.get_job("crawl", "c1", cursor="5")
    assert len(out["index"]) == 5


def test_scraping_one_url_still_returns_the_whole_page(monkeypatch):
    """The excerpts are short because asking for one page is exact. If that
    stopped being true there would be no way to read anything in full."""
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(page=_page(0, links=4, chars=900)))
    out = mcp_mod.scrape_urls(["https://x.test/0"])
    page = out["pages"][0]
    assert page["markdown"] == "m" * 900
    assert isinstance(page["links"], list) and len(page["links"]) == 4
    assert "index" not in out, "one page is not a result that needs an index"
