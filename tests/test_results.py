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
        """Batched, as the API pages: the envelope moves with the walk, which
        is where the next window's cursor comes from."""
        self.asked.append({"limit": limit, "wait": wait, "cursor": cursor})
        start = int(cursor or 0)
        rows = self._pages[start:]
        for at in range(0, len(rows), limit):
            batch = rows[at:at + limit]
            self.envelope = {**self.envelope,
                             "cursor": str(start + at + len(batch)),
                             "next": "?cursor=x" if start + at + limit < len(self._pages) else None}
            yield from batch

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


@pytest.mark.parametrize("pages", [50, 200, 500, 700])
def test_a_crawl_result_keeps_the_budget_it_promises(pages):
    """At 50 this passed against a hand-picked 65,000, while the code promised
    60,000 and a 500-page crawl came back at 101,399. The budget is the
    assertion now, and the sizes that broke it are in the list."""
    job = _Job([_page(i) for i in range(pages)])
    out = mcp_mod._crawl_result(job, *mcp_mod._walk(job))
    size = len(json.dumps(out))
    assert size <= mcp_mod.RESULT_BUDGET, f"{size:,} characters against a promise of {mcp_mod.RESULT_BUDGET:,}"
    assert len(out["pages"]) >= mcp_mod.MIN_EXCERPTS, "an index with nothing to read is not an answer"
    assert all(len(p["markdown"]) >= mcp_mod.EXCERPT_FLOOR for p in out["pages"])


@pytest.mark.parametrize("pages", [51, 200, 500, 700])
def test_a_cursor_is_named_only_when_it_is_there(pages):
    """The answer told an assistant to "call get_job with cursor=" and carried
    no cursor, for every crawl of 51 to 500 pages."""
    job = _Job([_page(i) for i in range(pages)])
    out = mcp_mod._crawl_result(job, *mcp_mod._walk(job))
    assert ("cursor=" in out["note"]) == ("cursor" in out)
    assert out["cursor"] == str(mcp_mod.PAGES_CAP), "the walk resumes after the first batch"
    # And it resumes from there.
    job2 = _Job([_page(i) for i in range(pages)])
    nxt = mcp_mod._crawl_result(job2, *mcp_mod._walk(job2, cursor=out["cursor"]))
    assert nxt["index"][0]["url"] == f"https://x.test/{mcp_mod.PAGES_CAP}"


def test_fifty_fat_pages_stay_inside_the_budget():
    job = _Job([_page(i) for i in range(50)])
    out = mcp_mod._crawl_result(job, job.pages())
    size = len(json.dumps(out))
    assert size <= mcp_mod.RESULT_BUDGET, f"{size} characters is more than promised"

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
    out = mcp_mod._crawl_result(job, *mcp_mod._walk(job))
    assert 0 < len(out["pages"]) <= mcp_mod.PAGES_CAP
    assert len(out["index"]) == 120, "an assistant has to know what exists, not just what it was shown"
    assert "in the index" in out["note"]


def test_the_index_itself_is_bounded():
    job = _Job([_page(i, links=1, chars=10) for i in range(mcp_mod.INDEX_CAP + 200)])
    out = mcp_mod._crawl_result(job, job.pages())
    assert len(out["index"]) == mcp_mod.INDEX_CAP


def test_the_budget_buys_fewer_excerpts_rather_than_thinner_ones():
    """The correction. Dividing the budget by fifty and clamping each share up
    to the floor made the floor win and the total overrun; spending on as many
    pages as the budget covers keeps both the floor and the promise."""
    few, _index, cap, _kept = mcp_mod._excerpts([_page(i, links=1, chars=50_000) for i in range(3)], "x")
    assert cap == mcp_mod.MARKDOWN_CAP and len(few) == 3, "a short crawl gets whole pages"

    many, index, cap, kept = mcp_mod._excerpts([_page(i) for i in range(500)], "x")
    assert len(many) < mcp_mod.PAGES_CAP, "fewer pages, not a thinner one each"
    assert cap >= mcp_mod.EXCERPT_FLOOR
    assert kept == len(index)
    assert len(json.dumps(index)) + len(json.dumps(many)) <= mcp_mod.RESULT_BUDGET


def test_the_cut_note_on_one_page_does_not_ask_for_the_call_just_made():
    """It said "fetch this page alone for all of it" to a caller that had."""
    out = mcp_mod._trim_page(_page(0, chars=20_000))
    assert "fetch this page alone" not in out["markdown"]
    assert "cap for one page" in out["markdown"]


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


# --- what a read-only connection is told ---------------------------------

def _refuse(status, detail, code=""):
    from mesharc import MeshArcError

    def boom():
        raise MeshArcError(status, detail, code=code)
    return boom


def test_a_read_only_connection_is_told_why_rather_than_about_roles(monkeypatch):
    """"this needs the member role" is true and useless: an assistant does
    not know what a role is, that it has one, or that the person who
    approved the connection chose it."""
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read"])
    # "forbidden" is what the API actually sends: it fills a code on every
    # error, so a test written against a code-less 403 passes while the
    # thing it is testing never happens.
    out = mcp_mod._safe(_refuse(403, "this needs the member role", code="forbidden"))
    assert out["code"] == "read_only"
    assert "approved read-only" in out["error"]
    assert "write access" in out["error"], "it has to say what would fix it"
    assert out["detail"] == "this needs the member role", "the API's own words are kept"


def test_a_connection_with_write_hears_the_api(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read", "write"])
    out = mcp_mod._safe(_refuse(403, "this needs the owner role", code="forbidden"))
    assert out == {"error": "this needs the owner role", "status": 403}


def test_the_other_refusals_keep_saying_what_they_say(monkeypatch):
    """A suspended workspace and an unverified address are also 403s, and
    neither is a scope problem. They carry a code; the role check does not."""
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read"])
    for code, detail in (("suspended", "this workspace is suspended; contact support"),
                         ("email_unverified", "verify your email address first"),
                         ("mfa_required", "this workspace requires two-factor authentication")):
        out = mcp_mod._safe(_refuse(403, detail, code=code))
        assert out == {"error": detail, "status": 403}, code


def test_locally_the_server_does_not_guess_at_the_key(monkeypatch):
    """Stdio holds one key and cannot see its scopes, so it passes the API's
    answer through rather than asserting something it does not know."""
    monkeypatch.setattr(mcp_mod, "_HTTP", False)
    assert mcp_mod._scopes() is None
    out = mcp_mod._safe(_refuse(403, "this needs the member role", code="forbidden"))
    assert out == {"error": "this needs the member role", "status": 403}


def test_a_webhook_signing_secret_never_reaches_an_assistant():
    """The API returns it with a project, for a program that will verify
    signatures with it. A tool's answer goes into a model's context, where
    nothing can use it and it should not be."""
    out = mcp_mod._safe(lambda: {"id": "p1", "name": "Site", "webhookSecret": "whsec_live_abc"})
    assert "webhookSecret" not in out
    assert "whsec_live_abc" not in str(out)
    assert "MeshArc app" in out["webhookSecretNote"], "say where it can be seen"
    # A project with no webhook configured gains no note.
    assert mcp_mod._safe(lambda: {"id": "p1", "webhookSecret": ""}) == {"id": "p1", "webhookSecret": ""}


def test_the_settings_guide_lists_every_tier_the_api_takes():
    """`auto` is accepted (api/projects.py max_tier) and was missing here, so
    an assistant reading the guide would never set it."""
    assert "'auto'" in mcp_mod.CONFIG_GUIDE["max_tier"]
    for tier in ("http", "browser", "stealth"):
        assert f"'{tier}'" in mcp_mod.CONFIG_GUIDE["max_tier"]


# --- crawl_site waits, then walks; a batch past its budget still accounts for every url ----

class _Running(_Job):
    """A crawl as the client sees it while it runs: the first poll finds `first`
    rows, later ones the rest; each fetch replaces the envelope."""

    def __init__(self, pages, first=12, finishes=True):
        super().__init__(pages, envelope={"status": "running"})
        self.first, self.finishes, self.waited = first, finishes, []

    def wait(self, timeout=3600, **_kw):
        self.waited.append(timeout)
        if not self.finishes:
            raise mcp_mod.MeshArcTimeoutError("still running", self.id)
        self.envelope = {**self.envelope, "status": "done"}
        return self.envelope

    def pages(self, limit=25, wait=True, cursor=None, **_kw):
        self.asked.append({"limit": limit, "wait": wait, "cursor": cursor})
        pos, seen = int(cursor or 0), (len(self._pages) if not wait else self.first)
        while pos < len(self._pages):
            end = min(pos + limit, seen)
            self.envelope = {**self.envelope, "cursor": str(end),
                             "next": f"https://api.test/api/v1/crawl/c1?cursor={end}" if end < len(self._pages) and end - pos == limit else None}
            yield from self._pages[pos:end]
            pos = end
            if pos >= seen:
                if not wait:
                    return
                seen = len(self._pages)


class _Starts:
    def __init__(self, job):
        self.job = job

    def crawl(self, url, **_kw):
        return self.job

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


@pytest.mark.parametrize("n", [60, 400])
def test_crawl_site_waits_for_the_crawl_then_walks_it(monkeypatch, n):
    """Walked while it ran, a first poll of twelve rows put the cursor past rows
    the walk had not read (62 for a 400-page crawl, against "after the first 50"),
    or past the end of a 60-page one. Waited for first, the walk is of a finished
    crawl and every batch is whole."""
    job = _Running([_page(i, links=1, chars=100) for i in range(n)])
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Starts(job))
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    out = mcp_mod.crawl_site("https://x.test/", limit=n)
    assert job.waited and all(a["wait"] is False for a in job.asked)
    assert out["status"] == "done"
    assert out.get("cursor") in (None, str(mcp_mod.PAGES_CAP)), out.get("cursor")


def test_hosted_crawl_site_hands_back_the_job_when_its_budget_runs_out(monkeypatch):
    job = _Running([_page(0)], finishes=False)
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Starts(job))
    monkeypatch.setattr(mcp_mod, "_budget", lambda: 9.0)
    out = mcp_mod.crawl_site("https://x.test/")
    assert job.waited == [9.0] and out["status"] == "running" and out["job"] == {"kind": "crawl", "id": "c1"}


def test_a_big_batch_accounts_for_every_url_inside_the_budget():
    """The index kept 297 of 500 and dropped the rest without a word, and the
    answer came to 60,051 against 60,000."""
    def page(i):
        return {"url": f"https://www.example-company.com/products/category-{i % 20}/item-name-number-{i}",
                "head": {"title": f"Item name number {i} | Example Company - Products and Services"},
                "words": 1234, "status": "blocked" if i in (300, 420) else "ok", "links": [{"href": "x"}] * 200,
                "markdown": "w " * 10_000}
    out = mcp_mod._batch_result({"id": "b1", "status": "done", "pages": [page(i) for i in range(500)]})
    assert len(json.dumps(out)) <= mcp_mod.RESULT_BUDGET
    rest = out["rest"]
    assert rest["count"] + len(out["index"]) == 500
    assert rest["byStatus"] == {"ok": rest["count"] - 2, "blocked": 2}
    assert [r["url"].rsplit("-", 1)[1] for r in rest["notOk"]] == ["300", "420"]
    assert "of 500 urls" in out["note"] and "credits" in out["note"]
