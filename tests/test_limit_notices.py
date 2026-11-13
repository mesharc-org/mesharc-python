"""What a limit kept from an answer is said, not swallowed.

Since 10 Nov 2026 the API marks a page a limit stopped -- the plan's tier, the
free plan's stealth trial, the credits left -- with a `limit`, and a crawl,
scrape, batch or search answer with `notices`, a line per reason. The client
logs them when it waited for the answer, and the MCP tools put them first, so
an assistant tells the person rather than quietly returning less.
"""
import logging

import pytest

from mesharc import Crawl

from test_client import scripted

NOTICE = {"code": "plan_limited", "reason": "stealth_trial_used", "pages": 2, "needs": "stealth",
          "examples": ["https://a.test/x", "https://a.test/y"], "unlockedBy": "growth",
          "upgradeUrl": "https://mesharc.dev/billing?plan=growth&from=limit_api",
          "message": "2 pages needed the stealth tier, and this workspace's free stealth trial is used up. "
                     "Growth includes the stealth tier."}
LIMIT = {"needs": "browser", "reason": "tier_ceiling", "ceiling": "http", "unlockedBy": None,
         "upgradeUrl": "https://mesharc.dev/billing?from=limit_api",
         "message": "This page needed the browser tier, above the max tier set on this project or request. "
                    "Raise max_tier to browser to read them."}


def _said(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "mesharc" and r.levelno == logging.WARNING]


def test_a_waited_crawl_logs_its_notices_once_and_keeps_them(caplog):
    arc = scripted({"id": "c1", "status": "running", "url": "https://a.test/"},
                   {"id": "c1", "status": "done", "notices": [NOTICE]},
                   {"id": "c1", "status": "done", "data": [], "notices": [NOTICE]})
    caplog.set_level(logging.WARNING, logger="mesharc")
    job = arc.crawl("https://a.test/", wait=True, poll=0)
    assert isinstance(job, Crawl) and job.notices == [NOTICE]
    list(job.pages(poll=0))
    assert _said(caplog) == [NOTICE["message"]], "said once, however the crawl was waited for"


def test_a_crawl_with_nothing_held_back_says_nothing(caplog):
    arc = scripted({"id": "c2", "status": "running"}, {"id": "c2", "status": "done", "notices": []})
    caplog.set_level(logging.WARNING, logger="mesharc")
    job = arc.crawl("https://a.test/", wait=True, poll=0)
    assert job.notices == [] and _said(caplog) == []


def test_an_older_api_without_notices_is_read_as_none():
    arc = scripted({"id": "c3", "status": "done"})
    assert arc.get_crawl("c3").notices == []


def test_one_scraped_page_logs_its_own_limit_and_keeps_it(caplog):
    page = {"url": "https://a.test/", "status": "blocked", "errorCode": "TIER_LIMIT", "limit": LIMIT}
    arc = scripted({"id": "s1", "status": "done", "data": [page], "notices": []})
    caplog.set_level(logging.WARNING, logger="mesharc")
    out = arc.scrape("https://a.test/")
    assert out["limit"] == LIMIT
    assert _said(caplog) == [LIMIT["message"]]


def test_a_waited_batch_and_search_log_their_notices(caplog):
    arc = scripted({"id": "b1", "status": "done", "notices": [NOTICE]},
                   {"id": "q1", "status": "done", "data": [], "notices": [NOTICE]})
    caplog.set_level(logging.WARNING, logger="mesharc")
    assert arc.batch("b1", wait=True)["notices"] == [NOTICE]
    assert arc.web_search("mesh arc")["notices"] == [NOTICE]
    assert _said(caplog) == [NOTICE["message"]] * 2


def test_an_answer_not_waited_for_is_returned_as_it_is_without_a_word(caplog):
    arc = scripted({"id": "b2", "status": "done", "notices": [NOTICE]})
    caplog.set_level(logging.WARNING, logger="mesharc")
    assert arc.batch("b2")["notices"] == [NOTICE]
    assert _said(caplog) == []


# --- the MCP tools: the notice comes first ---------------------------------

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")


def test_the_notice_line_comes_first_with_what_a_script_acts_on():
    out = mcp_mod._with_notice({"status": "done", "pages": []}, mcp_mod._limit_notice([NOTICE]))
    assert list(out)[0] == "notice" and out["notice"] == NOTICE["message"]
    assert out["notices"] == [{"reason": "stealth_trial_used", "pages": 2, "needs": "stealth",
                               "examples": NOTICE["examples"], "unlockedBy": "growth",
                               "upgradeUrl": NOTICE["upgradeUrl"]}]


def test_nothing_held_back_leaves_the_answer_as_it_was():
    out = {"status": "done", "pages": []}
    assert mcp_mod._with_notice(out, mcp_mod._limit_notice([])) is out
    assert mcp_mod._limit_notice(page={"url": "u", "limit": None}) == {}


def test_a_crawl_batch_and_search_answer_lead_with_the_notice():
    from test_results import _Job, _page
    crawl = mcp_mod._crawl_result(_Job([_page(0)], envelope={"notices": [NOTICE]}), [_page(0)])
    batch = mcp_mod._batch_result({"id": "b1", "status": "done", "pages": [_page(0)], "notices": [NOTICE]})
    search = mcp_mod._search_result({"id": "q1", "status": "done", "data": [], "notices": [NOTICE]})
    for out in (crawl, batch, search):
        assert list(out)[0] == "notice" and out["notice"] == NOTICE["message"]
        assert out["notices"][0]["upgradeUrl"] == NOTICE["upgradeUrl"]
        assert "message" not in out["notices"][0], "said once, in the line"


def test_a_crawl_answer_with_a_notice_still_keeps_its_budget():
    from test_results import _Job, _page
    pages = [_page(i) for i in range(500)]
    out = mcp_mod._crawl_result(_Job(pages, envelope={"notices": [NOTICE, {**NOTICE, "reason": "credits_left"}]}), pages)
    assert out["notice"] and len(__import__("json").dumps(out)) <= mcp_mod.RESULT_BUDGET


def test_one_scraped_page_leads_with_its_own_limit(monkeypatch):
    from test_results import _Arc
    page = {"url": "https://a.test/", "status": "blocked", "errorCode": "TIER_LIMIT", "limit": LIMIT, "markdown": ""}
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Arc(page=page))
    out = mcp_mod.scrape_urls(["https://a.test/"])
    assert list(out)[0] == "notice" and out["notice"] == LIMIT["message"]
    assert out["notices"] == [{"reason": "tier_ceiling", "pages": 1, "needs": "browser",
                               "examples": ["https://a.test/"], "unlockedBy": None,
                               "upgradeUrl": LIMIT["upgradeUrl"]}]
    assert out["pages"][0]["limit"] == LIMIT, "the page keeps its own limit"


@pytest.mark.parametrize("tool", ["scrape_urls", "crawl_site", "search_web", "get_job"])
def test_the_notice_comes_first_in_the_structured_answer_too(tool):
    """A host reading `structuredContent` gets the fields in the output type's
    order, not the dict's: `notice` has to be declared first for it to lead."""
    meta = mcp_mod._build()._tool_manager.get_tool(tool).fn_metadata
    res = meta.convert_result({"status": "done", "note": "x", "notice": "N", "notices": [{"reason": "r"}]})
    keys = list(res.structured_content)
    assert keys[0] == "notice" and keys.index("notices") > keys.index("note")


def test_a_search_notice_keeps_the_engine_it_names():
    search = {**NOTICE, "pages": 1, "examples": [], "engine": "google"}
    assert mcp_mod._limit_notice([search])["notices"][0]["engine"] == "google"
    assert "engine" not in mcp_mod._limit_notice([NOTICE])["notices"][0], "absent where the API sends none"


@pytest.mark.parametrize("likely, said", [
    ("yes", "The stealth tier is likely to read it: a captcha widget, which the stealth tier's solver answers."),
    ("no", "The stealth tier is not likely to read it either: a captcha widget, which the stealth tier's solver "
           "answers."),
    ("unknown", "Whether the stealth tier reads it is not known: a captcha widget, which the stealth tier's solver "
                "answers."),
])
def test_a_page_limit_says_whether_the_tier_it_needs_is_likely_to_read_it(likely, said):
    """The API's `outlook` on a page's limit (13 Nov 2026) is said on the
    notice line and kept in the list, so an upgrade is not offered with no
    word of whether it would help."""
    outlook = {"tier": "stealth", "likely": likely, "why": "a captcha widget, which the stealth tier's solver answers"}
    page = {"url": "https://a.test/", "limit": {**LIMIT, "needs": "stealth", "outlook": outlook}}
    out = mcp_mod._limit_notice(page=page)
    assert out["notice"] == LIMIT["message"] + " " + said
    assert out["notices"][0]["outlook"] == outlook
    assert "outlook" not in mcp_mod._limit_notice(page={"url": "u", "limit": LIMIT})["notices"][0], \
        "absent where the API sends none"
    assert mcp_mod._limit_notice(page={"url": "u", "limit": LIMIT})["notice"] == LIMIT["message"]


def test_search_web_says_how_a_search_is_read_and_charged_today():
    """DuckDuckGo between Google and Bing, and one price for a search
    (Seam-be #97, live 13 Nov 2026): no "4 credits or more"."""
    text = mcp_mod._build()._tool_manager.get_tool("search_web").description
    assert "DuckDuckGo" in text and "at most 4 credits a search" in text
    assert "or more" not in text and "Bing's when Google refuses" not in text
