"""MeshArc as an MCP server: the API's verbs as tools an agent can call.

    pip install "mesharc[mcp]"                      # Python 3.10+
    MESHARC_API_KEY=mesharc_... mesharc-mcp         # stdio, for Claude Desktop, Claude Code, Cursor

    claude mcp add mesharc -e MESHARC_API_KEY=mesharc_... -- mesharc-mcp

Tools return the API's JSON, trimmed where a body would swamp a context
window (markdown is capped per page; ask for one page to get all of it).
Every tool is a call through the Python client, so what the agent gets
is what the API gives.
"""

import os

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover
    raise SystemExit('The MCP server needs the "mcp" package: pip install "mesharc[mcp]"') from exc

from mesharc import MeshArc, MeshArcError

MARKDOWN_CAP = 12_000
PAGES_CAP = 50

server = MCPServer(
    "mesharc",
    instructions=(
        "MeshArc turns URLs into clean content and keeps a record of what changed. "
        "Use scrape_urls for a list of pages, extract_url for one page with every format, "
        "map_site to see what URLs a site declares before fetching any of them, and "
        "crawl_site to crawl a whole site once without setting a project up first "
        "(keep_crawl_as_project turns one of those into a watched project afterwards). "
        "The project tools are for a site watched over time: its pages, its change record, "
        "and search inside a run. Blocked pages are reported as blocked, never as missing."
    ),
)


def _client():
    key = os.environ.get("MESHARC_API_KEY") or ""
    if not key:
        raise RuntimeError("MESHARC_API_KEY is not set")
    return MeshArc(key, base_url=os.environ.get("MESHARC_API_URL") or None)


def _trim_page(p):
    if isinstance(p, dict):
        for k in ("markdown", "text", "cleanHtml", "html"):
            if isinstance(p.get(k), str) and len(p[k]) > MARKDOWN_CAP:
                p[k] = p[k][:MARKDOWN_CAP] + f"\n… [{len(p[k]) - MARKDOWN_CAP} more characters; fetch this page alone for all of it]"
        p.pop("response", None)
    return p


def _safe(fn):
    try:
        return fn()
    except MeshArcError as exc:
        return {"error": exc.detail, "status": exc.status}
    except Exception as exc:                          # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@server.tool(description="Scrape a list of URLs (up to 500) into markdown, no project needed. Waits for the batch. "
                         "One URL is answered in the same request where the page is quick. "
                         "`config` is any subset of a project config, e.g. {\"formats\": [\"markdown\", \"text\"], \"concurrency\": 4, \"render_js\": \"always\"}.")
def scrape_urls(urls: list[str], config: dict | None = None, formats: str = "markdown") -> dict:
    def go():
        with _client() as s:
            if len(urls) == 1:
                # One URL takes the synchronous path: the API holds the
                # request open for the page rather than making us poll.
                return {"status": "done", "urls": 1,
                        "pages": [_trim_page(s.scrape(urls[0], config=config, formats=formats))]}
            b = s.scrape(urls, config=config, formats=formats)
            b["pages"] = [_trim_page(p) for p in b.get("pages", [])[:PAGES_CAP]]
            return b
    return _safe(go)


@server.tool(description="Extract one URL with every format a project can produce (markdown, text, cleanHtml, "
                         "json fields, screenshot), through the fetch ladder: plain http first, a browser only "
                         "when needed or when render_js is 'always'. Browser `actions` (click, type, select, press, "
                         "wait, scroll; a click with repeat 'until_gone' for Load-more buttons; `each` to click "
                         "every match of a selector and run nested steps) run before the page is read.")
def extract_url(url: str, config: dict | None = None) -> dict:
    def go():
        with _client() as s:
            r = s.extract(url, config=config)
            if r.get("page"):
                r["page"] = _trim_page(r["page"])
                r["page"].pop("links", None)
            return r
    return _safe(go)


@server.tool(description="Every URL a site declares in its sitemaps -- robots.txt, the well-known paths, and every "
                         "index file walked to its children -- without fetching any of the pages. Cheap, and the right "
                         "first step before crawling: it says how big a site is and what sections it has. `search` "
                         "narrows to URLs containing a string.")
def map_site(url: str, search: str | None = None, limit: int = 1000) -> dict:
    def go():
        with _client() as s:
            out = s.map_details(url, search=search, limit=min(limit, 5000))
            return {"url": out["url"], "method": out["method"], "totals": out["totals"],
                    "urls": [u["url"] for u in out["data"]],
                    "sections": sorted({u.get("section", "") for u in out["data"] if u.get("section")})}
    return _safe(go)


@server.tool(description="Crawl a whole site once and return its pages -- no project needed. Follows links from the "
                         "URL given, reads the sitemap, and stops at `limit` pages. Returns when the crawl finishes "
                         "(minutes for a large limit); the pages come back with markdown, capped per page. "
                         "`crawl_id` in the result can be handed to keep_crawl_as_project.")
def crawl_site(url: str, limit: int = 50, max_depth: int = 3, include_paths: list[str] | None = None,
               exclude_paths: list[str] | None = None, config: dict | None = None) -> dict:
    def go():
        with _client() as s:
            opts = {"limit": min(limit, 5000), "maxDepth": max_depth}
            if include_paths:
                opts["includePaths"] = include_paths
            if exclude_paths:
                opts["excludePaths"] = exclude_paths
            if config:
                opts["config"] = config
            job = s.crawl(url, **opts)
            pages = [_trim_page(p) for p in job.pages(limit=50)][:PAGES_CAP]
            e = job.envelope
            return {"crawl_id": job.id, "status": e.get("status"), "url": url,
                    "pages": pages, "counts": e.get("counts"), "stop": e.get("stop", ""),
                    "note": ("this crawl is kept for a day unless keep_crawl_as_project is called"
                             if e.get("ephemeral") else "")}
    return _safe(go)


@server.tool(description="Keep a crawl from crawl_site as a project, so the site is watched over time and its changes "
                         "are recorded. Nothing is re-fetched: the crawl's pages become the project's first run. "
                         "schedule: manual | hourly | daily | weekly.")
def keep_crawl_as_project(crawl_id: str, name: str | None = None, schedule: str = "manual") -> dict:
    def go():
        with _client() as s:
            return s.get_crawl(crawl_id).keep(name=name, schedule=schedule)
    return _safe(go)


@server.tool(description="The workspace's projects: sites watched over time, with their last run's counts.")
def list_projects() -> list | dict:
    return _safe(lambda: [{k: p.get(k) for k in ("id", "name", "seed", "host", "schedule", "pages", "coverage", "lastRun", "health")}
                          for p in _client().projects.list()])


@server.tool(description="Create a project for a site (seed URL) so it is crawled on a schedule and its changes recorded. "
                         "schedule: manual | hourly | daily | weekly.")
def create_project(seed: str, name: str | None = None, schedule: str = "manual", config: dict | None = None) -> dict:
    return _safe(lambda: _client().projects.create(seed, name=name, schedule=schedule, config=config))


@server.tool(description="Start a crawl of a project now. With wait=true, returns the finished run (may take minutes).")
def start_run(project_id: str, wait: bool = False) -> dict:
    return _safe(lambda: _client().runs.start(project_id, wait=wait))


@server.tool(description="The pages of a project's last finished run (or run_id): url, status, depth, words, when changed.")
def list_pages(project_id: str, run_id: str | None = None) -> dict:
    def go():
        r = _client().pages(project_id, run_id)
        r["pages"] = r.get("pages", [])[:500]
        return r
    return _safe(go)


@server.tool(description="One stored page in full: markdown, head fields, fields, and its versions across runs.")
def get_page(project_id: str, url: str, run_id: str | None = None) -> dict:
    return _safe(lambda: _trim_page(_client().page(project_id, url, run_id)))


@server.tool(description="What changed in a project's last run against the run before it: pages added, modified, "
                         "removed (withheld when the crawl reached under 90% of the site), and head-field changes.")
def get_changes(project_id: str, run_id: str | None = None) -> dict:
    def go():
        r = _client().changes(project_id, run_id)
        ch = r.get("change") or {}
        for k in ("feed", "fields", "withheld"):
            if isinstance(ch.get(k), list):
                ch[k] = ch[k][:200]
        return r
    return _safe(go)


@server.tool(description="Search inside a project's run. mode 'content': every word must appear, \"quoted phrases\" as written, "
                         "over the extracted markdown. mode 'selector': a CSS selector or XPath (starting with / or () over the stored html.")
def search_pages(project_id: str, q: str, mode: str = "content", run_id: str | None = None) -> dict:
    return _safe(lambda: _client().search(project_id, q, mode=mode, run_id=run_id))


@server.tool(description="Fetch listed pages of a project again now, as a run of their own compared against the last full run.")
def recrawl_pages(project_id: str, urls: list[str]) -> dict:
    return _safe(lambda: _client().recrawl(project_id, urls))


def main() -> None:
    """The `mesharc-mcp` command: serve over stdio."""
    server.run("stdio")


if __name__ == "__main__":
    main()
