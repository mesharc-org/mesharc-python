"""MeshArc as an MCP server: the API's verbs as tools an agent can call.

Two ways to run it, differing in one thing: whose credential a request acts
with.

*Stdio*, the default, on the user's own machine with their own key:

    pip install "mesharc[mcp]"                      # Python 3.10+
    MESHARC_API_KEY=mesharc_... mesharc-mcp         # Claude Desktop, Claude Code, Cursor

    claude mcp add mesharc -e MESHARC_API_KEY=mesharc_... -- mesharc-mcp

*HTTP*, hosted, for clients that only speak remote MCP -- a URL to paste
instead of an install:

    MESHARC_MCP_PUBLIC_URL=https://mcp.example.dev/mcp \\
    MESHARC_OAUTH_ISSUER=https://api.example.dev \\
    MESHARC_INTROSPECT_SECRET=...                    \\
      mesharc-mcp --http --host 127.0.0.1 --port 8040

In that mode the server holds no key of its own. Each request carries the
caller's OAuth token, which is verified against the API and then used to make
the call, so one process serves many workspaces without any of them reaching
another's. `MESHARC_API_KEY` is never read there -- see `_client`.

Tools return the API's JSON, trimmed where a body would swamp a context window
(markdown is capped per page; ask for one page to get all of it). Every tool is
a call through the Python client, so what the agent gets is what the API gives.
A tool that would otherwise hold a connection open for minutes hands back a job
instead, for `get_job` to follow.
"""

import argparse
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from itertools import islice

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover
    raise SystemExit('The MCP server needs the "mcp" package: pip install "mesharc[mcp]"') from exc

from mesharc import MeshArc, MeshArcError, MeshArcTimeoutError
# The one place that decides whether a job is still going. Listing terminal
# names here instead meant `error` was missed and failed crawls polled for ever.
from mesharc import _running

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


# Which mode this process serves. Stdio is the default and behaves exactly as
# it always has: one key from the environment, one client, waits that block for
# as long as they like. Hosted is the other world -- many callers, each with
# their own token, and no key of the server's own at all.
_HTTP = False
# What a tool may hold a connection for in hosted mode before it hands back a
# job id instead. Many MCP hosts time a tool call out well before a minute, and
# this server cannot know which one is calling.
WAIT_DEFAULT_S = 25.0

_CLIENT = None
# Hosted mode: one client per caller, keyed by the hash of their token. The
# client remembers the rate-limit window the API told it about, and that memory
# belongs to one key -- shared across tenants, each would start blind and the
# first call after somebody else's busy patch would be refused.
_CLIENTS: "OrderedDict[str, _Shared]" = OrderedDict()
_CLIENTS_MAX = 1024


def _token_hash(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _caller_token():
    """The token this request arrived with.

    `get_access_token()` reads a contextvar the SDK sets per request, and anyio
    copies the context into the worker thread a sync tool runs on -- so this
    works from inside a tool with no wrapping.
    """
    from mcp.server.auth.middleware.auth_context import get_access_token
    at = get_access_token()
    return getattr(at, "token", None) if at else None


def _client():
    """The client this call acts as.

    Hosted mode takes the caller's own token and **never** reads
    MESHARC_API_KEY. That is the one line in this file that must not be wrong:
    with the fallback, every caller would act inside whichever workspace the
    operator's key belongs to, reading other tenants' pages and spending their
    credits. So there is no path from here to the environment.
    """
    global _CLIENT
    base = os.environ.get("MESHARC_API_URL") or None
    if _HTTP:
        token = _caller_token()
        if not token:
            raise RuntimeError("this request carried no access token")
        h = _token_hash(token)
        found = _CLIENTS.get(h)
        if found is not None:
            _CLIENTS.move_to_end(h)
            return found
        client = _Shared(token, base_url=base)
        _CLIENTS[h] = client
        while len(_CLIENTS) > _CLIENTS_MAX:
            _CLIENTS.popitem(last=False)
        return client
    key = os.environ.get("MESHARC_API_KEY") or ""
    if not key:
        raise RuntimeError("MESHARC_API_KEY is not set")
    if _CLIENT is None:
        _CLIENT = _Shared(key, base_url=base)
    return _CLIENT


def _budget():
    """How long a tool may block, or None for as long as it takes.

    Stdio waits: the caller is a local process that asked for the answer.
    Hosted does not: the caller is a socket through a proxy, behind a client
    with a timeout this server never sees.
    """
    if not _HTTP:
        return None
    try:
        return max(1.0, float(os.environ.get("MESHARC_MCP_WAIT") or WAIT_DEFAULT_S))
    except ValueError:
        return WAIT_DEFAULT_S


def _trim_page(p):
    if isinstance(p, dict):
        for k in ("markdown", "text", "cleanHtml", "html"):
            if isinstance(p.get(k), str) and len(p[k]) > MARKDOWN_CAP:
                p[k] = p[k][:MARKDOWN_CAP] + f"\n… [{len(p[k]) - MARKDOWN_CAP} more characters; fetch this page alone for all of it]"
        p.pop("response", None)
    return p


def _still_running(kind, job_id, counts=None, project_id=None):
    """What a long tool answers when its budget ran out.

    The job is not cancelled -- it goes on server-side, and `get_job` picks it
    up. Saying so matters: an assistant told only "running" would start the
    work again.
    """
    job = {"kind": kind, "id": job_id}
    if project_id:
        job["project_id"] = project_id
    return {"status": "running", "job": job, "counts": counts or {},
            "note": "call get_job with this job to check; it keeps running server-side"}


def _crawl_result(job, pages):
    """A finished crawl, shaped once. `crawl_site` and `get_job` both call
    this, so the two cannot drift apart."""
    e = job.envelope
    return {"crawl_id": job.id, "status": e.get("status"), "url": e.get("url", ""),
            "pages": [_trim_page(p) for p in pages][:PAGES_CAP],
            "counts": e.get("counts"), "stop": e.get("stop", ""),
            "note": ("this crawl is kept for a day unless keep_crawl_as_project is called"
                     if e.get("ephemeral") else "")}


def _batch_result(b):
    """A finished multi-URL scrape, shaped once."""
    b["pages"] = [_trim_page(p) for p in b.get("pages", [])[:PAGES_CAP]]
    return b


def _with_documents(config, parse_documents):
    """A PDF, a Word file or a spreadsheet handed to an assistant should be
    read, not reported as skipped: documents are parsed unless the caller
    says otherwise or the config already decides."""
    cfg = dict(config or {})
    cfg.setdefault("parse_documents", bool(parse_documents))
    return cfg


def _key(*parts):
    """An idempotency key for one call, stable for the same inputs, so a
    repeated tool call lands on the same job.

    The prefix has to be per caller, not per process. In hosted mode that is
    the grant: two workspaces asking for the same URL are two jobs, and a
    restart must not make yesterday's key collide with today's.
    """
    raw = json.dumps(parts, sort_keys=True, default=str)
    return "mcp-" + _scope() + "-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _scope():
    """What an idempotency key belongs to: the grant, hosted, and this
    process otherwise."""
    if not _HTTP:
        return _SESSION
    from mcp.server.auth.middleware.auth_context import get_access_token
    at = get_access_token()
    grant = (getattr(at, "claims", None) or {}).get("grant") if at else None
    return str(grant or getattr(at, "client_id", None) or _SESSION)[:16]


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
            # A list is a batch, and `scrape` waits for every row -- for up to
            # an hour by default. Hosted, that wait is bounded and the batch id
            # comes back instead, for get_job to pick up.
            budget = _budget()
            if budget is None:
                return _batch_result(s.scrape(urls, config=cfg, formats=formats, idempotency_key=key))
            try:
                return _batch_result(s.scrape(urls, config=cfg, formats=formats,
                                              idempotency_key=key, timeout=budget))
            except MeshArcTimeoutError as exc:
                return _still_running("batch", getattr(exc, "job_id", "") or "")
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
            # `pages()` waits for more while the crawl runs, for up to an hour
            # by default. That is right down a pipe and wrong down a socket, so
            # hosted mode gives it a budget and hands back the job id instead.
            budget = _budget()
            if budget is None:
                return _crawl_result(job, islice(job.pages(limit=PAGES_CAP), PAGES_CAP))
            try:
                # islice, not list: `pages()` walks the whole crawl, and only
                # PAGES_CAP of it is ever returned.
                pages = list(islice(job.pages(limit=PAGES_CAP, timeout=budget), PAGES_CAP))
            except MeshArcTimeoutError:
                # The budget, not a failure: the crawl is still going, and
                # `pages()` says so by raising once its deadline passes.
                return _still_running("crawl", job.id, job.envelope.get("counts"))
            return _crawl_result(job, pages)
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


@server.tool(description="Start a crawl of a project now. With wait=true, returns the finished run; hosted, a run "
                         "still going after a few seconds comes back as a job to follow with get_job.")
def start_run(project_id: str, wait: bool = False) -> dict:
    def go():
        s = _client()
        budget = _budget()
        if not wait or budget is None:
            return s.runs.start(project_id, wait=wait)
        try:
            return s.runs.start(project_id, wait=True, timeout=budget)
        except MeshArcTimeoutError as exc:
            return _still_running("run", getattr(exc, "job_id", "") or "",
                                  project_id=project_id)
    return _safe(go)


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


@server.tool(description="Follow a job a long tool handed back: a crawl from crawl_site, a run from start_run, or a "
                         "batch from scrape_urls. Returns its status and counts, and once it has finished, the same "
                         "result the original tool would have given. kind: crawl | run | batch. A run needs its "
                         "project_id. Costs nothing.")
def get_job(kind: str, id: str, project_id: str | None = None) -> dict:
    """The other half of a bounded wait.

    A tool that stopped waiting has to leave something to come back to, and
    this is it. The result of a finished job is shaped by the same helpers the
    original tool uses, so what an assistant gets by polling is what it would
    have got by waiting.
    """
    def go():
        s = _client()
        if kind == "crawl":
            job = s.get_crawl(id)
            if _running(job.envelope.get("status")):
                return _still_running("crawl", id, job.envelope.get("counts"))
            # Finished, however it finished -- `error` and `cancelled` included.
            # An assistant has to be told a crawl failed, not kept polling.
            return _crawl_result(job, islice(job.pages(limit=PAGES_CAP, wait=False), PAGES_CAP))
        if kind == "run":
            if not project_id:
                return {"error": "a run needs its project_id", "code": "validation"}
            run = s.runs.get(project_id, id)
            if _running(run.get("status")) or run.get("queued"):
                return _still_running("run", id, run.get("counts"), project_id=project_id)
            return run
        if kind == "batch":
            return _batch_result(s.batch(id))
        return {"error": f"kind must be crawl, run or batch, not {kind!r}", "code": "validation"}
    return _safe(go)


class _IntrospectionVerifier:
    """Verify a bearer token by asking the API about it.

    Opaque tokens and an introspection call, rather than something signed this
    server could check alone. The trade is deliberate: revocation is instant,
    because there is nothing cached to outlive it, and the cost is one API hop
    on a cold call plus a hard dependency on the API being up.

    It fails closed. A timeout, a 5xx or an answer that does not parse returns
    None, which the SDK turns into a 401. There is no branch here that lets a
    request through because the check could not be made.
    """

    def __init__(self, issuer, resource, secret, cache_s=60.0):
        self._url = issuer.rstrip("/") + "/oauth/introspect"
        self._resource = resource
        self._secret = secret
        self._cache_s = cache_s
        self._cache: "dict[str, tuple[float, object]]" = {}

    async def _ask(self, token):
        """Introspect, without stopping the server while it happens.

        `verify_token` runs on the event loop, so a blocking call here held
        every other request for up to the timeout on each token it had not
        seen -- one slow introspection stalling everybody. An async client
        waits on the socket and lets the loop get on with the rest.
        """
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as http:
            r = await http.post(self._url, data={"token": token},
                                headers={"X-Introspect-Secret": self._secret})
        r.raise_for_status()
        return r.json()

    async def verify_token(self, token):
        from mcp.server.auth.provider import AccessToken
        now = time.time()
        h = _token_hash(token)
        hit = self._cache.get(h)
        if hit and hit[0] > now:
            return hit[1]
        try:
            body = await self._ask(token)
        except Exception:                                  # noqa: BLE001 -- fail closed, whatever broke
            return None
        if not isinstance(body, dict) or not body.get("active"):
            return None
        scopes = [s for s in str(body.get("scope") or "").split() if s]
        # A token with no audience is refused, not adopted. Substituting this
        # server's own URL here made `validate_token_resource` compare the
        # value against itself and pass every time -- which is the whole check
        # that stops a token minted for somewhere else being spent here.
        aud = body.get("aud")
        if not aud:
            return None
        at = AccessToken(
            token=token,
            client_id=str(body.get("client_id") or ""),
            scopes=scopes,
            expires_at=int(body["exp"]) if body.get("exp") else None,
            resource=aud,
            claims={"org": body.get("org"), "grant": body.get("client_id")},
        )
        # Only a live answer is cached, and never past the token's own expiry.
        self._cache[h] = (min(now + self._cache_s, float(at.expires_at or now + self._cache_s)), at)
        if len(self._cache) > 4096:
            self._cache.clear()
        return at


def _check_secret(verifier):
    """Prove the introspection secret works before serving anything.

    A wrong secret makes every token look invalid, so without this the service
    starts clean and then 401s everything, with nothing pointing at the cause.
    One deliberately invalid token: `{"active": false}` means the endpoint
    answered us, and a 401 or 403 means it did not.
    """
    import httpx
    try:
        r = httpx.post(verifier._url, data={"token": "mesharc_oat_startup-probe"},
                       headers={"X-Introspect-Secret": verifier._secret}, timeout=5.0)
    except Exception as exc:                               # noqa: BLE001
        raise SystemExit(f"cannot reach {verifier._url}: {exc}\n"
                         "MESHARC_OAUTH_ISSUER has to point at a running MeshArc API.") from exc
    if r.status_code in (401, 403):
        raise SystemExit(f"{verifier._url} refused this server's introspection secret.\n"
                         "MESHARC_INTROSPECT_SECRET must match OAUTH_INTROSPECT_SECRET in the API's environment.")
    if r.status_code != 200:
        raise SystemExit(f"{verifier._url} answered {r.status_code}; expected 200 with active=false.")


def _transport_security(public_url):
    """Which Host and Origin headers to admit.

    This has to be passed. Binding 127.0.0.1 makes the SDK turn DNS-rebinding
    protection on by itself, with an allow-list of localhost only -- and behind
    a proxy the Host is the public domain, so every request would be refused.
    The middleware's own default is off, so it is this host-based branch that
    catches a deployment out.

    Origins matter for the same reason: a present Origin that is not listed is
    refused. An absent one passes, which is why a server-to-server connector
    would not notice, and a browser-based client would.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    from urllib.parse import urlparse
    p = urlparse(public_url)
    host = p.netloc
    origin = f"{p.scheme}://{p.netloc}"
    extra = [o.strip() for o in (os.environ.get("MESHARC_MCP_ALLOWED_ORIGINS") or "").split(",") if o.strip()]
    local_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    local_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=([host] if host else []) + local_hosts,
        allowed_origins=([origin] if host else []) + local_origins + extra,
    )


def main() -> None:
    """The `mesharc-mcp` command.

        mesharc-mcp                                  # stdio, as it always was
        mesharc-mcp --http --host 127.0.0.1 --port 8040

    Stdio stays the default, so an existing install is untouched by this.
    """
    global _HTTP
    ap = argparse.ArgumentParser(prog="mesharc-mcp", description="MeshArc as an MCP server.")
    ap.add_argument("--http", action="store_true",
                    help="serve streamable HTTP with OAuth instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8040)
    args = ap.parse_args()

    if not args.http:
        server.run("stdio")
        return

    public = (os.environ.get("MESHARC_MCP_PUBLIC_URL") or "").strip()
    issuer = (os.environ.get("MESHARC_OAUTH_ISSUER") or "").strip()
    secret = (os.environ.get("MESHARC_INTROSPECT_SECRET") or "").strip()
    missing = [n for n, v in (("MESHARC_MCP_PUBLIC_URL", public),
                              ("MESHARC_OAUTH_ISSUER", issuer),
                              ("MESHARC_INTROSPECT_SECRET", secret)) if not v]
    if missing:
        raise SystemExit("http mode needs " + ", ".join(missing))
    if os.environ.get("MESHARC_API_KEY"):
        # Not used, and saying so is better than leaving someone to believe it
        # is the credential requests act with. In http mode each caller brings
        # their own and this is never read.
        print("  ! MESHARC_API_KEY is set and will be ignored: in http mode every "
              "request acts as the caller who sent it.", flush=True)

    from mcp.server.auth.settings import AuthSettings
    verifier = _IntrospectionVerifier(issuer, public, secret)
    _check_secret(verifier)
    _HTTP = True

    # The tools registered on `server` at import, so auth is attached to that
    # instance rather than a second one being built around it. These are the
    # two things the streamable-http app reads (mcpserver.server:246-255): the
    # constructor would only have set the same pair, and it refuses one without
    # the other, so they are set together here too.
    # Pydantic URL types, not strings: AuthSettings declares AnyHttpUrl, and a
    # bad value should be refused here rather than at the first request.
    from pydantic import AnyHttpUrl
    server.settings.auth = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(public),
        required_scopes=["read"],
        # Explicit in 2.x, the default in 3.0. Without it, a token minted for
        # another resource would be accepted here.
        validate_token_resource=True,
    )
    server._token_verifier = verifier

    from urllib.parse import urlparse
    server.run("streamable-http", host=args.host, port=args.port,
               streamable_http_path=urlparse(public).path or "/mcp",
               transport_security=_transport_security(public))


if __name__ == "__main__":
    main()
