"""TM's second round of 15 Nov 2026: a screenshot tool that returns the
picture itself, and list_projects saying which projects are archived."""
import asyncio

import pytest

import mesharc
import mesharc.mcp as mcp_mod


class _Shot:
    def __init__(self, page, run_id="r1"):
        self.page, self.run_id, self.calls = page, run_id, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract(self, url, config=None, timeout=None):
        self.calls.append(("extract", url, config))
        return {"id": self.run_id, "page": self.page}

    def screenshot(self, run_id):
        self.calls.append(("screenshot", run_id))
        return b"\xff\xd8\xffjpeg"


def test_screenshot_url_returns_the_picture_and_what_it_cost(monkeypatch):
    arc = _Shot({"screenshot": True, "head": {"title": "Example Domain"}, "pageStatus": "ok", "credits": 5})
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    got = asyncio.run(mcp_mod.server.call_tool(
        "screenshot_url", {"url": "https://example.com/", "viewport": "mobile",
                           "actions": [{"type": "scroll", "times": 2}], "wait_ms": 500}))
    image, text = got.content
    assert image.type == "image" and image.mime_type == "image/jpeg"
    assert '"credits": 5' in text.text and '"title": "Example Domain"' in text.text
    assert got.structured_content["bytes"] == 7 and got.structured_content["pageStatus"] == "ok"
    assert arc.calls[0] == ("extract", "https://example.com/", {
        "render_js": "always", "formats": ["markdown", "screenshot"], "viewport": "mobile",
        "actions": [{"type": "scroll", "times": 2}], "wait_ms": 500})
    assert arc.calls[1] == ("screenshot", "r1")


def test_screenshot_url_says_why_when_no_picture_came_back(monkeypatch):
    arc = _Shot({"screenshot": False, "screenshotError": "TimeoutError: Timeout 30000ms exceeded.",
                 "pageStatus": "ok", "credits": 4})
    monkeypatch.setattr(mcp_mod, "_client", lambda: arc)
    out = mcp_mod.screenshot_url("https://example.com/")
    assert out["code"] == "no_screenshot" and out["status"] == 502 and out["request_id"] is None
    assert out["error"] == "no screenshot was taken: TimeoutError: Timeout 30000ms exceeded."
    assert out["pageStatus"] == "ok" and out["credits"] == 4
    assert [c[0] for c in arc.calls] == ["extract"]


def test_the_sdk_reads_a_screenshot_as_bytes(monkeypatch):
    seen = {}

    def fake(method, path, idempotency_key=None, as_bytes=False, **kw):
        seen.update(method=method, path=path, as_bytes=as_bytes)
        return b"jpeg"
    client = mesharc.MeshArc(api_key="k", base_url="https://api.test")
    monkeypatch.setattr(client, "_h", fake)
    assert client.screenshot("r9") == b"jpeg"
    assert seen == {"method": "GET", "path": "/playground/r9/screenshot", "as_bytes": True}


class _Projects:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    class projects:
        @staticmethod
        def list():
            return [{"id": "p1", "name": "live", "status": "healthy", "archived": False, "purgeAt": None},
                    {"id": "p2", "name": "old", "status": "draft", "archived": True,
                     "purgeAt": "2026-12-15T18:06:39+00:00"}]

    def billing(self):
        return {}


def test_list_projects_says_which_projects_are_archived(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_client", lambda: _Projects())
    rows = mcp_mod.list_projects()["projects"]
    assert [(r["id"], r["archived"], r["purgeAt"]) for r in rows] == [
        ("p1", False, None), ("p2", True, "2026-12-15T18:06:39+00:00")]


@pytest.mark.parametrize("name", ["screenshot_url"])
def test_the_new_tool_is_listed(name):
    assert name in {t.name for t in asyncio.run(mcp_mod.server.list_tools())}
