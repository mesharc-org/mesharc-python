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
    assert "scrape_urls" in out["pages"][0]["markdown"]


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
    assert "Also allow changes" in out["error"], "name the box on the consent screen"
    assert out["detail"] == "this needs the member role", "the API's own words are kept"


def test_a_connection_with_write_hears_the_api(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read", "write"])
    out = mcp_mod._safe(_refuse(403, "this needs the owner role", code="forbidden"))
    assert out == {"error": "this needs the owner role", "status": 403, "code": "forbidden"}


def test_the_other_refusals_keep_saying_what_they_say(monkeypatch):
    """A suspended workspace and an unverified address are also 403s, and
    neither is a scope problem. They carry a code; the role check does not."""
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read"])
    for code, detail in (("suspended", "this workspace is suspended; contact support"),
                         ("email_unverified", "verify your email address first"),
                         ("mfa_required", "this workspace requires two-factor authentication")):
        out = mcp_mod._safe(_refuse(403, detail, code=code))
        assert out == {"error": detail, "status": 403, "code": code}, code


def test_locally_the_server_does_not_guess_at_the_key(monkeypatch):
    """Stdio holds one key and cannot see its scopes, so it passes the API's
    answer through rather than asserting something it does not know."""
    monkeypatch.setattr(mcp_mod, "_HTTP", False)
    assert mcp_mod._scopes() is None
    out = mcp_mod._safe(_refuse(403, "this needs the member role", code="forbidden"))
    assert out == {"error": "this needs the member role", "status": 403, "code": "forbidden"}


def test_an_error_carries_its_code_and_request_id():
    """The code says why a call was refused and the request id is what
    support looks it up by; dropping either leaves nobody able to act."""
    from mesharc import MeshArcError

    def boom():
        raise MeshArcError(429, "slow down", code="rate_limited", request_id="req_1")
    out = mcp_mod._safe(boom)
    assert out == {"error": "slow down", "status": 429, "code": "rate_limited",
                   "request_id": "req_1"}


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


def test_hosted_the_walk_after_the_wait_is_bounded_too(monkeypatch):
    """The budget covered the wait and not the walk after it: past the deadline
    the walk now stops at a batch boundary, with the cursor still after the first."""
    clock = iter([0.0, 0.0, 5.0, 30.0, 30.0, 30.0, 30.0])
    monkeypatch.setattr(mcp_mod.time, "monotonic", lambda: next(clock))
    job = _Running([_page(i, links=1, chars=100) for i in range(400)])
    rows, tail = mcp_mod._walk(job, deadline=25.0, wait=False)
    assert len(rows) % mcp_mod.PAGES_CAP == 0 and mcp_mod.PAGES_CAP <= len(rows) < 400
    assert tail == str(mcp_mod.PAGES_CAP)
    rows, tail = mcp_mod._walk(_Running([_page(i, links=1, chars=100) for i in range(400)]), wait=False)
    assert len(rows) == 400, "without a deadline the walk reads to INDEX_CAP as before"


def test_a_batch_keeps_a_note_of_its_own():
    b = {"id": "b1", "status": "done", "note": "the API's own word",
         "pages": [{"url": f"https://www.example-company.com/products/item-name-number-{i}",
                    "head": {"title": f"Item name number {i} | Example Company - Products and Services"},
                    "words": 1234, "status": "ok", "markdown": "w " * 10_000} for i in range(500)]}
    out = mcp_mod._batch_result(b)
    assert out["note"].startswith("the API's own word. ") and "rest" in out


def test_a_batch_over_many_hosts_keeps_the_budget():
    """One run per url on a small host: 500 runs were 70,300 characters before a
    page was shown, and the answer came to 73,821."""
    pages = [{"url": f"https://host-{i // 5}.example.com/products/item-{i}", "head": {"title": f"Item {i}"},
              "words": 900, "status": "ok", "markdown": "w " * 5_000} for i in range(500)]
    runs = [{"id": f"{i:032x}", "host": f"host-{i // 5}.example.com", "status": "done", "done": 1, "total": 1,
             "stop": "no more pages to crawl"} for i in range(500)]
    out = mcp_mod._batch_result({"id": "b1", "status": "done", "runs": runs, "pages": pages})
    assert len(json.dumps(out)) <= mcp_mod.RESULT_BUDGET
    assert out["runs"] == {"count": 500, "byStatus": {"done": 500}}
    assert len(out["pages"]) >= mcp_mod.MIN_EXCERPTS


def test_a_local_crawl_that_outlasts_the_wait_hands_back_its_job(monkeypatch):
    job = _Running([_page(0)], finishes=False)
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Starts(job))
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    out = mcp_mod.crawl_site("https://x.test/")
    assert out["status"] == "running" and out["job"] == {"kind": "crawl", "id": "c1"}


def test_long_urls_still_leave_room_for_five_excerpts():
    """Five summaries cost more than their bodies once urls and titles are long:
    the index left room for 5 x 600 and the excerpts came out at four."""
    def page(i):
        return {"url": "https://www.example-company.com/" + "segment/" * 34 + f"item-{i}",
                "head": {"title": "A long product title " * 10 + str(i)}, "words": 1234, "status": "ok",
                "markdown": "w " * 10_000}
    job = _Job([page(i) for i in range(500)])
    out = mcp_mod._crawl_result(job, job.pages())
    assert len(out["pages"]) >= mcp_mod.MIN_EXCERPTS and len(json.dumps(out)) <= mcp_mod.RESULT_BUDGET


# --- web_search, waited for down a pipe and bounded hosted ----------------

class _Searches:
    """Enough of a client for web_search and get_job's search branch: each
    call takes the next answer, or raises what it was given."""

    def __init__(self, *answers, raises=None):
        self.answers, self.raises, self.calls = list(answers), raises, []

    def web_search(self, query, **kw):
        self.calls.append((query, kw))
        if self.raises is not None:
            raise self.raises
        return self.answers.pop(0)

    def get_search(self, id):
        self.calls.append((id, {}))
        return self.answers.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def _hit(i, page=None):
    h = {"position": i + 1, "title": f"Result {i}", "url": f"https://x.test/{i}",
         "snippet": "s" * 160, "source": "organic", "engine": "google"}
    if page is not None:
        h["page"] = page
    return h


def _search(status="done", hits=(), **extra):
    return {"id": "s1", "status": status, "query": "q", "engine": "google", "cached": False,
            "creditsUsed": 3, "data": list(hits), **extra}


def test_ten_scraped_results_stay_inside_the_budget(monkeypatch):
    """Ten hits at a full page each is what a crawl used to answer with: the
    pages come back as excerpts with a link count, inside the same promise."""
    arc = _Searches(_search(hits=[_hit(i, _page(i)) for i in range(10)]))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    out = mcp_mod.search_web("q", scrape=True)
    size = len(json.dumps(out))
    assert size <= mcp_mod.RESULT_BUDGET, f"{size:,} characters against a promise of {mcp_mod.RESULT_BUDGET:,}"
    assert len(out["results"]) == 10
    assert all(isinstance(r["page"]["links"], int) for r in out["results"])
    assert "href" not in json.dumps(out), "not one link list survived"
    assert all("call scrape_urls with that url" in r["page"]["markdown"] for r in out["results"])
    (query, kw), = arc.calls
    assert query == "q" and kw["scrape"] is True
    assert "idempotency_key" not in kw, "a repeat search is a new search by design"


def test_a_blocked_search_says_how_it_was_refused(monkeypatch):
    blocked = _search(status="blocked", error="every engine refused the results page",
                      attempts=[{"engine": "google", "status": 429}, {"engine": "bing", "status": 403}])
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Searches(blocked))
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    out = mcp_mod.search_web("q")
    assert out["status"] == "blocked" and out["results"] == []
    assert out["error"] == "every engine refused the results page"
    assert out["attempts"] == blocked["attempts"]


def test_a_local_search_that_outlasts_the_clients_wait_hands_back_its_job(monkeypatch):
    arc = _Searches(raises=mcp_mod.MeshArcTimeoutError("still running", "s9"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    out = mcp_mod.search_web("q")
    assert out["status"] == "running" and out["job"] == {"kind": "search", "id": "s9"}


def test_hosted_web_search_asks_once_inside_its_budget(monkeypatch):
    """No poll loop down the socket: one call that the API holds for the
    budget less five seconds, and whatever it has comes back."""
    monkeypatch.setattr(mcp_mod, "_budget", lambda: 25.0)
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read", "write"])

    arc = _Searches(_search(status="queued"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.search_web("q")
    (_query, kw), = arc.calls
    assert kw["wait"] is False and kw["timeout_s"] == 20.0
    assert out["status"] == "running" and out["job"] == {"kind": "search", "id": "s1"}
    assert "results" not in out

    arc = _Searches(_search(status="running", hits=[_hit(i) for i in range(3)]))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.search_web("q", scrape=True)
    assert [r["url"] for r in out["results"]] == [f"https://x.test/{i}" for i in range(3)]
    assert out["job"] == {"kind": "search", "id": "s1"}
    assert "get_job" in out["note"]

    arc = _Searches(_search(status="error", error="every engine failed"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.search_web("q")
    assert out == {"error": "every engine failed", "status": 502, "code": "job_failed"}


def test_get_job_follows_a_search(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_budget", lambda: None)
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Searches(_search(status="queued")))
    out = mcp_mod.get_job("search", "s1")
    assert out["status"] == "running" and out["job"] == {"kind": "search", "id": "s1"}

    done = _search(hits=[_hit(i, _page(i)) for i in range(4)])
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Searches(done))
    polled = mcp_mod.get_job("search", "s1")
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Searches(done))
    waited = mcp_mod.search_web("q", scrape=True)
    assert polled == waited, "what an assistant gets by polling is what it would have got by waiting"
    assert "job" not in polled

    monkeypatch.setattr(mcp_mod, "_client",
                        lambda: _Searches(_search(status="error", error="every engine failed")))
    out = mcp_mod.get_job("search", "s1")
    assert out == {"error": "every engine failed", "status": 502, "code": "job_failed"}


# --- run_agent, and an agent run through get_job and cancel_job ----------

class _AgentRun:
    """A handle as `MeshArc.agent` and `get_agent` return one: `refresh`
    reads the next envelope the test scripted, as the API would."""

    def __init__(self, arc, envelope):
        self.arc, self.id, self.envelope = arc, envelope["id"], envelope

    @property
    def continued_by(self):
        return self.envelope.get("continuedBy") or None

    def refresh(self):
        self.arc.calls.append(("refresh", self.id))
        if self.arc.now:
            self.envelope = self.arc.now.pop(0)
        return self

    def cancel(self):
        self.arc.calls.append(("cancel", self.id))
        return self.arc.cancel_answer

    def continue_(self, max_credits=None, max_steps=None, timeout_s=0, idempotency_key=None):
        self.arc.calls.append(("continue", self.id, idempotency_key,
                               {"max_credits": max_credits, "max_steps": max_steps}))
        if self.arc.continue_raises is not None:
            raise self.arc.continue_raises
        return _AgentRun(self.arc, dict(self.arc.continued))


class _Agents:
    """Enough of a client for run_agent, continue_agent and the agent branches
    of get_job and cancel_job. `posted` is what POST answers, `continued` what
    POST .../continue answers, `now` what each later read finds; `raises` is
    raised by the POST or the read instead, `continue_raises` by the continue."""

    def __init__(self, posted=None, now=(), raises=None, cancel_answer=None, continued=None,
                 continue_raises=None):
        self.posted, self.now, self.raises = posted, list(now), raises
        self.cancel_answer = cancel_answer
        self.continued, self.continue_raises = continued, continue_raises
        self.calls = []

    def agent(self, prompt, idempotency_key=None, **kw):
        self.calls.append(("agent", prompt, idempotency_key, kw))
        if self.raises is not None:
            raise self.raises
        return _AgentRun(self, dict(self.posted))

    def get_agent(self, id):
        self.calls.append(("get_agent", id))
        if self.raises is not None:
            raise self.raises
        return _AgentRun(self, self.now.pop(0))

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def _agent_env(status="running", **extra):
    return {"id": "a1", "kind": "agent", "status": status, "prompt": "find it", "params": {}, "data": None,
            "sources": [], "creditsUsed": 12, "budget": 2000, "budgetLimited": False,
            "tokens": {"in": 100, "out": 20}, "steps": 3, "model": "m", "stopReason": "", "error": "",
            "next": "/api/v1/agent/a1", **extra}


def test_run_agent_keys_a_retry_onto_the_same_run_for_ten_minutes(monkeypatch):
    clock = [600 * 1000 + 10]
    monkeypatch.setattr(mcp_mod.time, "time", lambda: clock[0])
    arc = _Agents(posted=_agent_env("queued"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)

    def key(prompt="find it", **kw):
        arc.now = [_agent_env("running")]
        mcp_mod.run_agent(prompt, **kw)
        return arc.calls[-2][2]

    first = key(urls=["https://x.test/"], max_credits=50)
    assert first.startswith("mcp-")
    clock[0] += 500
    assert key(urls=["https://x.test/"], max_credits=50) == first, "a retry in the same window is the same run"
    assert key(urls=["https://x.test/"], max_credits=60) != first, "different inputs are a different run"
    assert key("find something else", urls=["https://x.test/"], max_credits=50) != first
    clock[0] += 100
    assert key(urls=["https://x.test/"], max_credits=50) != first, "a re-run in a later window is a new run"
    # Every input reaches the client.
    _name, prompt, _key, kw = arc.calls[-2]
    assert prompt == "find it" and kw["urls"] == ["https://x.test/"] and kw["max_credits"] == 50


def test_run_agent_reads_the_run_after_starting_it_and_hands_back_a_job(monkeypatch):
    arc = _Agents(posted=_agent_env("queued", creditsUsed=0, steps=0),
                  now=[_agent_env("running", creditsUsed=40, steps=2)])
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.run_agent("find it", schema={"type": "object"}, allowed_domains=["x.test"], max_steps=10)
    assert [c[0] for c in arc.calls] == ["agent", "refresh"]
    assert arc.calls[0][3]["schema"] == {"type": "object"} and arc.calls[0][3]["allowed_domains"] == ["x.test"]
    assert arc.calls[0][3]["max_steps"] == 10
    assert out["status"] == "running" and out["job"] == {"kind": "agent", "id": "a1"}
    assert out["budget"] == 2000 and out["creditsUsed"] == 40 and out["steps"] == 2
    assert "get_job" in out["note"] and "30 to 60 seconds" in out["note"]


def test_a_replayed_run_that_has_finished_answers_with_its_result(monkeypatch):
    """A replayed key answers with the first stored envelope -- here a stale
    'queued' -- so the run is read again rather than reported as queued."""
    done = _agent_env("done", data={"text": "the answer"}, creditsUsed=90, steps=7,
                      sources=[{"url": "https://x.test/a", "title": "A", "pageId": "p1"}])
    arc = _Agents(posted=_agent_env("queued"), now=[done])
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.run_agent("find it")
    assert out["status"] == "done" and "job" not in out
    assert out["data"] == {"text": "the answer"} and out["sources"] == done["sources"]
    assert out["creditsUsed"] == 90 and out["steps"] == 7 and out["id"] == "a1"
    assert "error" not in out, "an empty error is not an error"


@pytest.mark.parametrize("detail,code", [
    # The API's own code for it, and an API from before that code, which said so only in the message.
    ("a request with this Idempotency-Key is still running", "in_flight"),
    ("a request with this Idempotency-Key is still running", "conflict"),
])
def test_a_key_still_being_accepted_is_answered_not_raised(monkeypatch, detail, code):
    from mesharc import MeshArcError
    arc = _Agents(raises=MeshArcError(409, detail, code))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.run_agent("find it")
    assert out == {"status": "running", "note": mcp_mod.AGENT_ACCEPTING}
    assert "run_agent again" in out["note"]


def test_run_agent_passes_on_a_409_that_is_not_a_key_in_flight(monkeypatch):
    """A 409 the API means as a refusal is not "still being accepted":
    answering it as running would have an assistant ask again for ever."""
    from mesharc import MeshArcError
    arc = _Agents(raises=MeshArcError(409, "the workspace already has an agent run going", "conflict", "req_7"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.run_agent("find it")
    assert out == {"error": "the workspace already has an agent run going", "status": 409, "code": "conflict",
                   "request_id": "req_7"}


def test_get_job_follows_an_agent_run(monkeypatch):
    def poll(envelope):
        monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[envelope]))
        return mcp_mod.get_job("agent", "a1")

    out = poll(_agent_env("running", creditsUsed=55, steps=4))
    assert out["status"] == "running" and out["job"] == {"kind": "agent", "id": "a1"}
    assert out["creditsUsed"] == 55 and out["steps"] == 4 and out["budget"] == 2000

    sources = [{"url": f"https://x.test/{i}", "title": f"T{i}", "pageId": f"p{i}"} for i in range(3)]
    out = poll(_agent_env("done", data={"plans": [{"name": "Pro", "price": 20}]}, sources=sources))
    assert out["status"] == "done" and out["data"] == {"plans": [{"name": "Pro", "price": 20}]}
    assert out["sources"] == sources and "job" not in out and out["note"] == ""

    out = poll(_agent_env("credit_limit", data={"partial": "half of it"}, stopReason="credit_limit"))
    assert out["status"] == "credit_limit" and out["data"] == {"partial": "half of it"}
    assert "data.partial" in out["note"] and "max_credits" in out["note"]
    assert "continue_agent with id='a1'" in out["note"], "the note names the tool and the run to carry on"
    assert "run_agent" not in out["note"], "starting over would pay for the pages again"
    assert out["stopReason"] == "credit_limit"


def test_a_stopped_run_already_carried_on_points_at_the_run_that_did(monkeypatch):
    stopped = _agent_env("credit_limit", data={"partial": "half"}, stopReason="credit_limit", continuedBy="a2")
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[stopped]))
    out = mcp_mod.get_job("agent", "a1")
    assert "carried on by run a2" in out["note"] and "get_job" in out["note"]
    assert "continue_agent" not in out["note"], "a second continue is refused, so it is not suggested"


def test_an_expired_agent_run_is_an_answer_not_an_error(monkeypatch):
    from mesharc import MeshArcError
    arc = _Agents(raises=MeshArcError(410, "this agent run has expired", "expired"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.get_job("agent", "a1")
    assert out == {"status": "expired", "id": "a1", "note": mcp_mod.AGENT_EXPIRED}
    assert "7 days" in out["note"]


def test_an_agent_run_that_failed_keeps_the_apis_words(monkeypatch):
    failed = _agent_env("error", error="the model provider refused the request")
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[failed]))
    out = mcp_mod.get_job("agent", "a1")
    assert out["status"] == "error" and out["error"] == "the model provider refused the request"
    assert "job" not in out


@pytest.mark.parametrize("chars", ["x", "é\"\\"], ids=["plain", "escaped"])
def test_a_huge_agent_answer_stays_inside_the_budget(monkeypatch, chars):
    """Five hundred sources and an answer of a few hundred thousand
    characters: the sources go down to fifty first, then the answer is cut,
    and the note says where the whole of it is. Quotes, backslashes and
    anything not ASCII weigh more in JSON than they count in the text."""
    text = chars * (200_000 // len(chars))
    sources = [{"url": f"https://www.example-company.com/products/item-{i}", "title": f"Item {i} | Example",
                "pageId": f"{i:032x}"} for i in range(500)]
    huge = _agent_env("done", data={"text": text}, sources=sources)
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[huge]))
    out = mcp_mod.get_job("agent", "a1")
    size = len(json.dumps(out))
    assert size <= mcp_mod.RESULT_BUDGET, f"{size:,} characters against a promise of {mcp_mod.RESULT_BUDGET:,}"
    assert len(out["sources"]) == mcp_mod.AGENT_SOURCES_CAP and out["sources"] == sources[:50]
    assert isinstance(out["data"], str) and out["data"].startswith('{"text": "')
    assert "more characters" in out["data"]
    assert size > mcp_mod.RESULT_BUDGET - 1_000, "the cut answer uses the room it has"
    assert "GET /api/v1/agent/a1" in out["note"] and "get_agent" in out["note"]
    assert "first 50 of 500" in out["note"]


def test_many_sources_alone_are_cut_and_the_answer_kept(monkeypatch):
    sources = [{"url": f"https://www.example-company.com/products/category/item-{i}",
                "title": "A long product title " * 4, "pageId": f"{i:032x}"} for i in range(800)]
    many = _agent_env("done", data={"text": "short"}, sources=sources)
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[many]))
    out = mcp_mod.get_job("agent", "a1")
    assert len(json.dumps(out)) <= mcp_mod.RESULT_BUDGET
    assert out["data"] == {"text": "short"}, "the answer is cut last"
    assert len(out["sources"]) == 50 and "first 50 of 800" in out["note"] and "get_agent" in out["note"]


def _field_sources(n):
    return {f"plans[{i}].price": {"url": f"https://www.example-company.com/pricing/plan-{i}",
                                  "pageId": f"{i:032x}"} for i in range(n)}


def test_a_finished_answer_says_where_each_value_came_from(monkeypatch):
    fields = _field_sources(2)
    done = _agent_env("done", data={"plans": [{"price": 20}, {"price": 40}]}, fieldSources=fields,
                      sources=[{"url": "https://www.example-company.com/pricing/plan-0", "title": "P",
                                "pageId": f"{0:032x}"}])
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[done]))
    out = mcp_mod.get_job("agent", "a1")
    assert out["fieldSources"] == fields and out["note"] == ""
    # A run the API said nothing about answers an empty map, not a missing one.
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[_agent_env("done", fieldSources=None)]))
    assert mcp_mod.get_job("agent", "a1")["fieldSources"] == {}


def test_field_sources_are_left_out_first_when_the_answer_is_too_long(monkeypatch):
    """The answer and its sources fit; the map of where each value came from
    tips it over, so the map goes and nothing else is cut."""
    sources = [{"url": f"https://x.test/{i}", "title": f"T{i}", "pageId": f"p{i}"} for i in range(80)]
    data = {"text": "y" * 30_000}
    fields = _field_sources(400)
    assert len(json.dumps(fields)) > 30_000, "the map alone has to tip the answer over"
    env = _agent_env("done", data=data, sources=sources, fieldSources=fields)
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[env]))
    out = mcp_mod.get_job("agent", "a1")
    assert len(json.dumps(out)) <= mcp_mod.RESULT_BUDGET
    assert "fieldSources" not in out
    assert out["data"] == data and out["sources"] == sources, "the answer and all 80 sources are kept"
    assert "fieldSources is left out" in out["note"] and "sources lists" not in out["note"]
    assert "GET /api/v1/agent/a1" in out["note"] and "get_agent('a1')" in out["note"]


def test_a_huge_answer_with_field_sources_loses_them_and_then_the_rest(monkeypatch):
    sources = [{"url": f"https://www.example-company.com/products/item-{i}", "title": f"Item {i}",
                "pageId": f"{i:032x}"} for i in range(500)]
    huge = _agent_env("done", data={"text": "x" * 200_000}, sources=sources, fieldSources=_field_sources(400))
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[huge]))
    out = mcp_mod.get_job("agent", "a1")
    assert len(json.dumps(out)) <= mcp_mod.RESULT_BUDGET
    assert "fieldSources" not in out and len(out["sources"]) == 50 and isinstance(out["data"], str)
    assert ("fieldSources is left out, sources lists the first 50 of 500 and data is the answer's JSON text, cut"
            in out["note"])


def test_cancel_job_stops_an_agent_run_and_passes_on_the_apis_status(monkeypatch):
    for status in ("cancelling", "cancelled", "done"):
        arc = _Agents(now=[_agent_env("running")], cancel_answer={"id": "a1", "status": status})
        monkeypatch.setattr(mcp_mod, "_client", lambda arc=arc: arc)
        out = mcp_mod.cancel_job("agent", "a1")
        assert arc.calls == [("get_agent", "a1"), ("cancel", "a1")]
        assert out["kind"] == "agent" and out["id"] == "a1" and out["outcome"] == status
        assert "stay charged" in out["note"] and "next step" in out["note"]


def test_run_agent_answers_through_the_server_inside_its_output_schema(monkeypatch):
    """Through call_tool, so the answer is checked against the schema the
    tool declares -- and that schema describes an error too."""
    import asyncio
    arc = _Agents(posted=_agent_env("queued"), now=[_agent_env("running")])
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    got = asyncio.run(mcp_mod.server.call_tool("run_agent", {"prompt": "find it"}))
    assert got.structured_content["job"] == {"kind": "agent", "id": "a1"}

    tool = {t.name: t for t in asyncio.run(mcp_mod.server.list_tools())}["run_agent"]
    schema = tool.output_schema
    assert schema["additionalProperties"] is True and not schema.get("required")
    for field in ("id", "status", "data", "fieldSources", "sources", "creditsUsed", "budget", "steps", "job",
                  "note", "error"):
        assert (schema["properties"][field].get("description") or "").strip(), field
    assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False and tool.annotations.open_world_hint is True


# --- continue_agent ------------------------------------------------------------

def _stopped(**extra):
    return _agent_env("credit_limit", data={"partial": "half"}, stopReason="credit_limit", **extra)


def _continued(status="queued", **extra):
    return {**_agent_env(status, **extra), "id": "a2", "continuesRunId": "a1", "next": "/api/v1/agent/a2"}


def test_continue_agent_starts_the_new_run_and_hands_back_its_job(monkeypatch):
    arc = _Agents(now=[_stopped(), _continued("running", creditsUsed=5, steps=1)], continued=_continued())
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.continue_agent("a1", max_credits=4000, max_steps=20)
    assert [c[0] for c in arc.calls] == ["get_agent", "continue", "refresh"]
    assert arc.calls[1][1] == "a1" and arc.calls[1][3] == {"max_credits": 4000, "max_steps": 20}
    assert arc.calls[2] == ("refresh", "a2"), "the new run is read as it stands, not the stopped one"
    assert out["status"] == "running" and out["job"] == {"kind": "agent", "id": "a2"}
    assert out["creditsUsed"] == 5 and out["steps"] == 1 and "get_job" in out["note"]


def test_continue_agent_leaves_out_the_budget_it_was_not_given(monkeypatch):
    arc = _Agents(now=[_stopped(), _continued("running")], continued=_continued())
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    mcp_mod.continue_agent("a1")
    assert arc.calls[1][3] == {"max_credits": None, "max_steps": None}, "the stopped run's own then apply"


def test_continue_agent_keys_a_retry_onto_the_same_run_for_ten_minutes(monkeypatch):
    clock = [600 * 1000 + 10]
    monkeypatch.setattr(mcp_mod.time, "time", lambda: clock[0])
    arc = _Agents(continued=_continued())
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)

    def key(run_id="a1", **kw):
        arc.now = [_stopped(), _continued("running")]
        mcp_mod.continue_agent(run_id, **kw)
        return arc.calls[-2][2]

    first = key(max_credits=4000)
    assert first.startswith("mcp-")
    clock[0] += 500
    assert key(max_credits=4000) == first, "a retry in the same window is the same new run"
    assert key(max_credits=5000) != first, "a different budget is a different request"
    assert key(max_credits=4000, max_steps=10) != first
    assert key("a9", max_credits=4000) != first, "another run is another key"
    clock[0] += 100
    assert key(max_credits=4000) != first, "a later window is a new key"


def test_continue_agent_answers_a_finished_new_run_with_its_result(monkeypatch):
    done = _continued("done", data={"text": "all of it"}, fieldSources={"text": {"url": "https://x.test/",
                                                                                  "pageId": "p1"}})
    arc = _Agents(now=[_stopped(), done], continued=_continued())
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.continue_agent("a1")
    assert out["status"] == "done" and out["id"] == "a2" and out["data"] == {"text": "all of it"}
    assert out["fieldSources"] == {"text": {"url": "https://x.test/", "pageId": "p1"}} and "job" not in out


@pytest.mark.parametrize("detail,code", [
    # The API's own code for it, and an API from before that code, which said so only in the message.
    ("a request with this Idempotency-Key is still running", "in_flight"),
    ("a request with this Idempotency-Key is still running", "conflict"),
])
def test_continue_agent_while_the_same_key_is_still_being_accepted(monkeypatch, detail, code):
    from mesharc import MeshArcError
    arc = _Agents(now=[_stopped()], continue_raises=MeshArcError(409, detail, code))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.continue_agent("a1")
    assert out == {"status": "running", "note": mcp_mod.AGENT_CONTINUE_ACCEPTING}
    assert "continue_agent again" in out["note"]


@pytest.mark.parametrize("detail", [
    "only a run stopped at its credit limit can be continued; this one is done",
    "this run was already continued, by run a2",
    "this run has no saved progress to continue from",
])
def test_continue_agent_passes_on_a_conflict_about_the_run(monkeypatch, detail):
    """The same status and code as a key in flight, but a refusal: answering
    it as running would have an assistant ask again for ever."""
    from mesharc import MeshArcError
    arc = _Agents(now=[_stopped()], continue_raises=MeshArcError(409, detail, "conflict", "req_9"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.continue_agent("a1")
    assert out == {"error": detail, "status": 409, "code": "conflict", "request_id": "req_9"}


@pytest.mark.parametrize("carried", [_continued("running", creditsUsed=7, steps=2),
                                     _continued("done", data={"text": "all of it"})])
def test_continue_agent_answers_with_the_run_that_already_carried_it_on(monkeypatch, carried):
    """Already carried on -- this tool's own call, retried past the ten-minute
    key window, or another caller's: the run that did is the answer, and
    nothing is posted, as a second continue would only be refused."""
    arc = _Agents(now=[_stopped(continuedBy="a2"), carried], continued=_continued())
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.continue_agent("a1", max_credits=4000)
    assert arc.calls == [("get_agent", "a1"), ("get_agent", "a2")], "read, not continued again"
    assert out["status"] == carried["status"]
    assert out["note"].startswith("run a1 was already carried on by run a2; this is that run")
    if carried["status"] == "running":
        assert out["job"] == {"kind": "agent", "id": "a2"} and "get_job" in out["note"]
        assert out["creditsUsed"] == 7 and out["steps"] == 2
    else:
        assert out["id"] == "a2" and out["data"] == {"text": "all of it"} and "job" not in out


@pytest.mark.parametrize("status,code,detail", [
    (404, "not_found", "agent run not found"),
    (410, "expired", "this agent run's results are past their keep date"),
])
def test_continue_agent_answers_an_unknown_or_expired_run_with_its_code(monkeypatch, status, code, detail):
    from mesharc import MeshArcError
    arc = _Agents(raises=MeshArcError(status, detail, code))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.continue_agent("a1")
    assert out == {"error": detail, "status": status, "code": code}
    assert [c[0] for c in arc.calls] == ["get_agent"], "nothing is continued"
    # Past its keep date at the continue itself, too.
    arc = _Agents(now=[_stopped()], continue_raises=MeshArcError(status, detail, code))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    assert mcp_mod.continue_agent("a1") == {"error": detail, "status": status, "code": code}


def test_continue_agent_on_a_read_only_connection_says_so(monkeypatch):
    from mesharc import MeshArcError
    monkeypatch.setattr(mcp_mod, "_scopes", lambda: ["read"])
    arc = _Agents(now=[_stopped()], continue_raises=MeshArcError(403, "this needs the member role", "forbidden"))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.continue_agent("a1")
    assert out["code"] == "read_only" and out["status"] == 403 and "write access" in out["error"]


def test_continue_agent_answers_through_the_server_inside_its_output_schema(monkeypatch):
    import asyncio
    arc = _Agents(now=[_stopped(), _continued("running")], continued=_continued())
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    got = asyncio.run(mcp_mod.server.call_tool("continue_agent", {"id": "a1", "max_credits": 4000}))
    assert got.structured_content["job"] == {"kind": "agent", "id": "a2"}

    tools = {t.name: t for t in asyncio.run(mcp_mod.server.list_tools())}
    tool, run = tools["continue_agent"], tools["run_agent"]
    assert tool.output_schema == run.output_schema, "both answer an agent run the same way"
    assert tool.annotations == run.annotations
    assert set(tool.input_schema["properties"]) == {"id", "max_credits", "max_steps"}
    assert tool.input_schema["required"] == ["id"]


# --- a webhook signing secret never reaches an assistant through an agent tool -------

SECRET = "whsec_agent_live_123"


@pytest.mark.parametrize("status", ["running", "done"])
def test_no_agent_tool_hands_back_a_webhook_secret(monkeypatch, status):
    """The API returns a run's signing secret once, on the POST that made it,
    for a program that verifies signatures. Here every envelope carries one,
    the POST's and every read's, and no agent tool's answer may."""
    secret = {"webhookSecret": SECRET}
    extra = dict(secret, data={"text": "the answer"}) if status == "done" else secret

    def check(out):
        assert out.get("status") == status, out
        assert "webhookSecret" not in json.dumps(out) and SECRET not in json.dumps(out), out

    arc = _Agents(posted=_agent_env("queued", **secret), now=[_agent_env(status, **extra)])
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    check(mcp_mod.run_agent("find it"))

    arc = _Agents(now=[_stopped(**secret), _continued(status, **extra)], continued=_continued("queued", **secret))
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    check(mcp_mod.continue_agent("a1"))

    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[_agent_env(status, **extra)]))
    check(mcp_mod.get_job("agent", "a1"))
    # A stopped run read through get_job, too.
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Agents(now=[_stopped(**secret)]))
    out = mcp_mod.get_job("agent", "a1")
    assert out["status"] == "credit_limit" and SECRET not in json.dumps(out) and "webhookSecret" not in out
