"""MeshArc as an MCP server: the API's verbs as tools an agent can call.

    pip install "mesharc[mcp]"                      # Python 3.10+
    MESHARC_API_KEY=mesharc_... mesharc-mcp         # stdio, for Claude Desktop, Claude Code, Cursor

    claude mcp add mesharc -e MESHARC_API_KEY=mesharc_... -- mesharc-mcp

Tools return the API's JSON, trimmed where a body would swamp a context
window (markdown is capped per page; ask for one page to get all of it).
Every tool is a call through the Python client, so what the agent gets
is what the API gives.
"""

import hashlib
import json
import os
import uuid

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


class _Shared(MeshArc):
    """One client for the server's life. The tools open it with `with`,
    which for a shared client must not close it -- and the client's memory
    of the key's rate-limit window has to outlive one tool call, or every
    call starts blind and the first after a busy one is refused."""

    def __exit__(self, *exc):
        return None


_CLIENT = None


def _client():
    global _CLIENT
    key = os.environ.get("MESHARC_API_KEY") or ""
    if not key:
        raise RuntimeError("MESHARC_API_KEY is not set")
    if _CLIENT is None:
        _CLIENT = _Shared(key, base_url=os.environ.get("MESHARC_API_URL") or None)
    return _CLIENT


def _trim_page(p):
    if isinstance(p, dict):
        for k in ("markdown", "text", "cleanHtml", "html"):
            if isinstance(p.get(k), str) and len(p[k]) > MARKDOWN_CAP:
                p[k] = p[k][:MARKDOWN_CAP] + f"\n… [{len(p[k]) - MARKDOWN_CAP} more characters; fetch this page alone for all of it]"
        p.pop("response", None)
    return p


def _with_documents(config, parse_documents):
    """A PDF, a Word file or a spreadsheet handed to an assistant should be
    read, not reported as skipped: documents are parsed unless the caller
    says otherwise or the config already decides."""
    cfg = dict(config or {})
    cfg.setdefault("parse_documents", bool(parse_documents))
    return cfg


def _key(*parts):
    """An idempotency key for one call, stable for the same inputs within
    this server's life, so a repeated tool call is the same job."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return "mcp-" + _SESSION + "-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


_SESSION = uuid.uuid4().hex[:8]


def _safe(fn):
    try:
        return fn()
    except MeshArcError as exc:
        return {"error": exc.detail, "status": exc.status}
    except Exception as exc:                          # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@server.tool(description="Scrape a list of URLs (up to 500) into markdown, no project needed. Waits for the batch. "
                         "One URL is answered in the same request where the page is quick. PDFs, Word files and "
                         "spreadsheets are read as text unless parse_documents is false. "
                         "`config` is any subset of a project config, e.g. {\"formats\": [\"markdown\", \"text\"], \"concurrency\": 4, \"render_js\": \"always\"}.")
def scrape_urls(urls: list[str], config: dict | None = None, formats: str = "markdown",
                parse_documents: bool = True) -> dict:
    def go():
        cfg = _with_documents(config, parse_documents)
        # A key of our own, so a retry -- the client's on a dropped
        # connection, the assistant's on a 429 -- lands on the same job.
        key = _key("scrape", urls, cfg, formats)
        with _client() as s:
            if len(urls) == 1:
                # One URL takes the synchronous path: the API holds the
                # request open for the page rather than making us poll.
                return {"status": "done", "urls": 1,
                        "pages": [_trim_page(s.scrape(urls[0], config=cfg, formats=formats, idempotency_key=key))]}
            b = s.scrape(urls, config=cfg, formats=formats, idempotency_key=key)
            b["pages"] = [_trim_page(p) for p in b.get("pages", [])[:PAGES_CAP]]
            return b
    return _safe(go)


@server.tool(description="Extract one URL with every format a project can produce (markdown, text, cleanHtml, "
                         "json fields, screenshot), through the fetch ladder: plain http first, a browser only "
                         "when needed or when render_js is 'always'. Browser `actions` (click, type, select, press, "
                         "wait, scroll; a click with repeat 'until_gone' for Load-more buttons; `each` to click "
                         "every match of a selector and run nested steps) run before the page is read.")
def extract_url(url: str, config: dict | None = None, parse_documents: bool = True) -> dict:
    def go():
        with _client() as s:
            r = s.extract(url, config=_with_documents(config, parse_documents))
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
                         "`include_paths` and `exclude_paths` are globs over the URL path -- '/blog/*' for a section, "
                         "'*.pdf' for documents; a bare '/blog/' matches only that one page. "
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
            job = s.crawl(url, idempotency_key=_key("crawl", url, opts), **opts)
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


# The settings an assistant is likely to set, with what each means. The
# full list, with defaults, comes back from describe_project_config; this
# is the vocabulary that turns "only the blog, weekly, rendered" into a
# config without guessing key names.
CONFIG_GUIDE = {
    "include_paths": "list of globs over the URL path to crawl, e.g. ['/blog/*'] (which takes /blog itself too); "
                     "empty means the whole site. This is THE setting for 'only this section': it applies to "
                     "links and to sitemap URLs alike",
    "exclude_paths": "list of globs to leave out, e.g. ['/tag/*', '*.pdf']; an exclude wins over an include",
    "sitemap_include": "globs over sitemap FILE urls or section labels (e.g. ['sitemap-posts.xml']) on a site with "
                       "several sitemap files; leave empty for a section of pages -- use include_paths for that",
    "sitemap_exclude": "globs over sitemap file urls or section labels to drop; usually empty",
    "crawl_mode": "'sitemap_first' (declared URLs, then links; default), 'sitemap_only' (exactly the declared URLs), 'links' (follow links only)",
    "max_pages": "the most pages one run reads (plan cap applies)",
    "max_depth": "how many links deep from the seed (0 = the seed only)",
    "crawl_delay_ms": "milliseconds between requests to the host (default 1000)",
    "concurrency": "pages fetched at once (1-8)",
    "render_js": "'auto' (a browser only when the page needs one; default), 'always', 'never'",
    "max_tier": "the highest rung allowed: 'http', 'browser', 'stealth'",
    "max_credits_per_page": "cap on what one page may cost; 0 = no cap",
    "formats": "list of bodies to keep: 'markdown', 'text', 'cleanHtml', 'rawHtml', 'links', 'screenshot', 'json'",
    "only_main_content": "true drops navigation, headers, footers and sidebars from the markdown",
    "include_tags": "CSS selectors to keep, e.g. ['article', '.post']",
    "exclude_tags": "CSS selectors to drop, e.g. ['.comments', '#newsletter']",
    "min_words": "pages shorter than this are recorded but not compared (0 = keep all)",
    "languages": "list of language codes to keep, e.g. ['en']; empty = all",
    "parse_documents": "true reads PDFs, Word and spreadsheet files the crawl meets",
    "respect_robots": "obey robots.txt (default true)",
    "allow_subdomains": "follow links to subdomains of the seed's domain",
    "use_proxy": "route through the residential exits (costs more; for walled sites)",
    "json_schema": "a JSON schema of fields to extract from every page",
    "llm_extract": "true lets a model fill fields the markup could not",
    "webhook_url": "where to POST run and change events",
    "webhook_events": "which events to send, e.g. ['run.finished', 'page.changed']",
    "notify_min_words": "a change smaller than this many words is not notified",
    "chat_url": "a Slack, Discord, Teams, Mattermost or Google Chat incoming-webhook URL: the run record is posted "
                "there when a run finds something (never when nothing changed). Read back masked; sending the "
                "mask keeps it",
    "change_digest": "{connection_id, focus}: after each compared run a model (an llm connection the workspace "
                     "added under Connectors) writes a paragraph on what changed and why it matters, with up to "
                     "five points; 'focus' says what the reader cares about, e.g. 'pricing and plan limits'. "
                     "null = off. Carried by the email, the chat message and the run.finished webhook",
    "actions": "browser steps before reading (click, type, scroll, wait...); see the docs",
    "wait_for_selector": "in the browser, wait until this selector appears before reading",
}


@server.tool(description="Every project setting an assistant can set: name, meaning, and the default. Read this "
                         "before create_project or update_project when the request names a section, a schedule, "
                         "a format, a limit or a behaviour -- the keys are exact, guessed names are refused.")
def describe_project_config() -> dict:
    def go():
        with _client() as s:
            defaults = (s.meta().get("configDefaults") or {})
            return {"settings": [{"key": k, "meaning": v, "default": defaults.get(k)} for k, v in CONFIG_GUIDE.items()],
                    "other_keys": sorted(k for k in defaults if k not in CONFIG_GUIDE),
                    "schedules": ["manual", "hourly", "daily", "weekly"],
                    "note": "include_paths and exclude_paths are globs over the URL path: '/blog/*' is the blog section "
                            "(its index included), '/blog/' is one page. To limit a project to a section, set include_paths "
                            "and nothing else; sitemap_include is for choosing among sitemap files, not pages."}
    return _safe(go)


@server.tool(description="One project with its settings, schedule and last run -- read it before changing it.")
def get_project(project_id: str) -> dict:
    return _safe(lambda: _client().projects.get(project_id))


@server.tool(description="Create a project for a site (seed URL) so it is crawled on a schedule and its changes recorded. "
                         "`config` is any subset of the settings describe_project_config lists -- for one section of "
                         "a site pass include_paths (globs), e.g. {\"include_paths\": [\"/blog/*\"]}; map_site first "
                         "shows how the site is laid out. schedule: manual | hourly | daily | weekly.")
def create_project(seed: str, name: str | None = None, schedule: str = "manual", config: dict | None = None) -> dict:
    return _safe(lambda: _client().projects.create(seed, name=name, schedule=schedule, config=config))


@server.tool(description="Change a project: its name, schedule, or any settings in `config` (only the keys given "
                         "change; the rest stay). The next run uses the new settings.")
def update_project(project_id: str, name: str | None = None, schedule: str | None = None, config: dict | None = None) -> dict:
    def go():
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if schedule is not None:
            fields["schedule"] = schedule
        if config:
            fields["config"] = config
        if not fields:
            return {"error": "nothing to change: give a name, a schedule or config"}
        with _client() as s:
            return s.projects.update(project_id, **fields)
    return _safe(go)


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
