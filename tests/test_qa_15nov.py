"""Fixes from the MCP test of 15 Nov 2026: N3 (a batch is read with its formats; every row
carries every body asked for) and N8 (every error a tool builds has a status and code)."""
import pytest

from mesharc import MeshArc

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")


def test_a_batch_scrape_sends_its_formats_on_the_request_that_starts_it():
    arc = MeshArc("mesharc_test")
    sent = []
    arc._h = lambda method, path, **kw: sent.append((method, path, kw.get("json"))) or {"id": "b1"}  # type: ignore[assignment]
    arc.scrape(["https://a.test/", "https://b.test/"], formats="markdown,text", wait=False)
    assert sent == [("POST", "/scrape", {"urls": ["https://a.test/", "https://b.test/"], "formats": "markdown,text"})]


def test_a_row_of_a_multi_page_answer_excerpts_every_body_it_was_given():
    # The page's cap is shared by its bodies (review of N3): 20 is 10 each, not 20 each.
    row = mcp_mod._summary_page({"url": "https://a.test/", "markdown": "m" * 50, "text": "t" * 50,
                                 "links": ["x", "y"]}, cap=20)
    assert row["markdown"].startswith("m" * 10) and not row["markdown"].startswith("m" * 11)
    assert row["text"].startswith("t" * 10) and "40 more characters" in row["text"] and row["links"] == 2
    one = mcp_mod._summary_page({"url": "https://a.test/", "markdown": "m" * 50}, cap=20)
    assert one["markdown"].startswith("m" * 20) and "30 more characters" in one["markdown"]


def test_an_answer_is_measured_in_the_characters_it_carries():
    # Accented text is not counted six times over (review of N3).
    assert mcp_mod._cost({"t": "é" * 10}) == mcp_mod._cost({"t": "e" * 10})


def test_an_error_a_tool_builds_itself_carries_status_code_and_request_id():
    out = mcp_mod._safe(lambda: {"error": "give q or url, not both", "code": "validation"})
    assert out == {"error": "give q or url, not both", "code": "validation", "status": 400, "request_id": None}
    crash = mcp_mod._safe(lambda: 1 / 0)
    assert (crash["status"], crash["code"]) == (500, "internal") and "ZeroDivisionError" in crash["error"]


def test_a_successful_answer_with_an_empty_error_field_is_left_alone():
    assert mcp_mod._safe(lambda: {"status": "done", "error": ""}) == {"status": "done", "error": ""}


def test_a_network_failure_and_a_missing_key_get_their_own_codes():
    # Review of N8: both came back as a bare 500 internal.
    import httpx

    def unreachable():
        raise httpx.ConnectError("no route")

    def no_key():
        raise ValueError("An API key is required: MeshArc('mesharc_...') or set MESHARC_API_KEY.")
    net, key = mcp_mod._safe(unreachable), mcp_mod._safe(no_key)
    assert (net["status"], net["code"]) == (503, "network") and "could not be reached" in net["error"]
    assert (key["status"], key["code"]) == (401, "no_api_key")


class _Meta:
    def meta(self):
        return {"configDefaults": {"render_js": "auto", "max_pages": 100}}


def test_a_setting_sent_in_the_call_is_shown_even_at_its_default():
    # Review of #22: update with render_js "auto" answered config {} and looked ignored.
    out = mcp_mod._lean_config(_Meta(), {"config": {"render_js": "auto", "max_pages": 100}}, sent={"render_js": "auto"})
    assert out["config"] == {"render_js": "auto"}


def test_reading_a_batch_back_sends_formats_only_when_named():
    # Review of N3: a default "markdown" replaced the batch's own formats.
    arc = MeshArc("mesharc_test")
    sent = []
    arc._h = lambda method, path, **kw: sent.append(kw.get("params")) or {"status": "done"}  # type: ignore[assignment]
    arc.batch("b1")
    arc.batch("b1", formats="text")
    assert sent == [None, {"formats": "text"}]


def test_an_api_error_with_no_request_id_or_code_still_carries_both():
    # Second review of N8: such an error had neither, and a client network
    # error (status 0) came back as status 0.
    from mesharc import MeshArcError

    def bare():
        raise MeshArcError(404, "gone")

    def offline():
        raise MeshArcError(0, "network error: no route", "network")
    out, net = mcp_mod._safe(bare), mcp_mod._safe(offline)
    assert out == {"error": "gone", "status": 404, "code": "error", "request_id": None}
    assert (net["status"], net["code"], net["request_id"]) == (503, "network", None)


def test_a_cut_text_body_is_noticed_as_cut():
    # Second review of N3: only markdown was looked at.
    assert mcp_mod._cut({"markdown": "m" * 10, "text": "t" * 80}, cap=100) is True
    assert mcp_mod._cut({"markdown": "m" * 40, "text": "t" * 40}, cap=100) is False
