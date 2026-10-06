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
(a multi-page answer is an index with excerpts inside 60,000 characters; one
page asked for on its own comes back with each body capped at 12,000). Every tool is
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
from typing import Annotated, Literal

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
    from pydantic import Field
except ImportError as exc:  # pragma: no cover
    raise SystemExit('The MCP server needs the "mcp" package: pip install "mesharc[mcp]"') from exc

from mesharc import MeshArc, MeshArcError, MeshArcTimeoutError
# The one place that decides whether a job is still going. Listing terminal
# names here instead meant `error` was missed and failed crawls polled for ever.
from mesharc import _running

# One page asked for on its own: how much of its body comes back.
MARKDOWN_CAP = 12_000
# A multi-page result: how many pages carry an excerpt, and how many
# characters of excerpt the whole answer gets to spend between them. The cap
# used to be per page, which is the bug this fixes: fifty pages at twelve
# thousand each is an answer no assistant can load, and the link lists alone
# ran to a megabyte. A reader now gets an index of everything, a taste of each
# page, and a way to ask for the one page it actually wants.
PAGES_CAP = 50
RESULT_BUDGET = 60_000
# Below this an excerpt says nothing, so the budget is allowed to overrun
# rather than hand back fifty useless fragments.
EXCERPT_FLOOR = 600
# However tight the budget, this many pages carry a readable excerpt: an
# index with nothing to read is a list of links, not an answer.
MIN_EXCERPTS = 5
# Rows in the index: every page the crawl found, up to this, so an assistant
# knows what exists even where the excerpts are short.
INDEX_CAP = 500
# A batch past its index: the urls that did not come back ok, named up to this.
REST_BUDGET = 8_000

NAME = "mesharc"
INSTRUCTIONS = (
    "MeshArc turns URLs into clean content and keeps a record of what changed. "
    "Use scrape_urls for a list of pages, extract_url for one page with every format, "
    "map_site to see what URLs a site declares before fetching any of them, and "
    "crawl_site to crawl a whole site once without setting a project up first "
    "(keep_crawl_as_project turns one of those into a watched project afterwards). "
    "The project tools are for a site watched over time: its pages, its change record, "
    "and search inside a run. Blocked pages are reported as blocked, never as missing."
)

# The tools, in the order they are declared, with how each was registered.
# `MCPServer.tool` hands the function straight back, so every tool below stays
# an ordinary module-level function -- which is what lets the same set be put
# on a second server later, built with auth in its constructor. The registry is
# what carries the descriptions across: a description is the only thing an
# assistant has to pick a tool by, and re-registering without them would leave
# seventeen nameless verbs.
_TOOLS: "list[tuple]" = []

# What each tool does to the world, in the hints MCP clients read. A tool that
# only reads what the workspace has stored is safe to call freely; one that
# reaches a site spends credits. The descriptions say the rest -- what it
# costs, what comes back -- and must never contradict these.
READS_STORED = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                               idempotent_hint=True, open_world_hint=False)


def _acts(*, destructive=False, idempotent=False, open_world=True):
    return ToolAnnotations(read_only_hint=False, destructive_hint=destructive,
                           idempotent_hint=idempotent, open_world_hint=open_world)


# The parameters most tools share, described once.
ProjectId = Annotated[str, Field(description="The project's id, as list_projects or create_project returns it.")]
RunId = Annotated[str | None, Field(
    description="A run's id, from start_run or the runId a list_pages answer carries; "
                "leave empty for the last finished run.")]
Schedule = Literal["manual", "hourly", "daily", "weekly"]
FetchConfig = Annotated[dict | None, Field(
    description="Optional fetch settings: any subset of the keys describe_project_config lists, "
                "e.g. {\"render_js\": \"always\", \"only_main_content\": true}. Omit for the defaults.")]
ParseDocuments = Annotated[bool, Field(
    description="Read PDFs, Word files and spreadsheets as text (default true); false leaves their text out. "
                "A parse_documents key in config wins.")]


def tool(**how):
    """Declare a tool, and remember how it was declared."""
    def register(fn):
        _TOOLS.append((fn, how))
        return fn
    return register


def _build(**kw):
    """A server with this package's tools on it, and whatever `kw` says.

    Auth belongs in `kw`: `token_verifier` and `auth` are constructor
    arguments, and passing them here is what keeps this file off the SDK's
    private attributes.
    """
    srv = MCPServer(NAME, instructions=INSTRUCTIONS, **kw)
    for fn, how in _TOOLS:
        srv.add_tool(fn, **how)
    return srv


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
    """One page, as a single-page tool returns it: all of it, bodies capped."""
    if isinstance(p, dict):
        for k in ("markdown", "text", "cleanHtml", "html"):
            if isinstance(p.get(k), str) and len(p[k]) > MARKDOWN_CAP:
                p[k] = p[k][:MARKDOWN_CAP] + (
                    f"\n… [{len(p[k]) - MARKDOWN_CAP} more characters; "
                    f"{MARKDOWN_CAP:,} is the cap for one page]")
        p.pop("response", None)
    return p


def _index_row(p):
    """What a page is, in four fields: enough to choose it by."""
    head = p.get("head")
    return {"url": p.get("url", ""),
            "title": (head.get("title") or "") if isinstance(head, dict) else "",
            "words": p.get("words"), "status": p.get("status")}


def _summary_page(p, cap, how=""):
    """One page of a multi-page result: what it is, and a taste of it.

    Everything else goes: the link list, with a count in its place -- it was
    the single biggest thing in a crawl result, ahead of all the markdown put
    together -- the head apart from the title, and the diagnostic fields an
    assistant has no use for. `how` says how to come back for the whole page,
    and is added only where the body was actually cut.
    """
    if not isinstance(p, dict):
        return p
    out = _index_row(p)
    links = p.get("links")
    out["links"] = len(links) if isinstance(links, list) else 0
    body = p.get("markdown")
    if isinstance(body, str) and body:
        out["markdown"] = body[:cap]
        if len(body) > cap:
            out["markdown"] += (f"\n… [{len(body) - cap} more characters"
                                + (f"; {how}" if how else "") + "]")
    return out


def _cost(obj):
    return len(json.dumps(obj))


def _excerpts(rows, how, reserve=0):
    """How a multi-page answer spends its budget: (pages, index, cap, kept).

    `reserve` is what the answer costs around these two -- its status, counts,
    note and the rest. The caller measures it rather than guessing, because
    guessing is how the first version came out at 60,190 against a promise of
    60,000: everything inside the pages was counted and the envelope holding
    them was not.

    `RESULT_BUDGET` is for the whole answer, so everything is measured, not
    estimated -- the keys, the urls, the titles and the note on a cut excerpt
    as much as the bodies. Summarising at a cap of zero is that measurement.

    Order of claims. The index is the map and is paid first, because an
    assistant that knows what exists can ask for any of it; a sample of fifty
    pages with no map is the weaker answer. Then as many excerpts as the rest
    will pay for at the floor -- **fewer excerpts, not thinner ones**. That is
    the correction: dividing the budget by fifty and clamping each share up to
    the floor made the floor win, and a five-hundred page crawl came back at
    101,399 characters against a promise of 60,000. A promise a result does not
    keep is worse than a smaller promise, and this one existed to stop exactly
    the answer it was producing.

    The map can still be trimmed, but only to leave room for `MIN_EXCERPTS`
    samples: a thousand rows of index and nothing to read is not an answer
    either. `kept` is how many rows of the index survived, so the caller can
    say so rather than implying the crawl was that size.
    """
    budget = max(MIN_EXCERPTS * EXCERPT_FLOOR, RESULT_BUDGET - reserve)
    index = [_index_row(p) for p in rows]
    floor_cost = MIN_EXCERPTS * EXCERPT_FLOOR + _cost([_summary_page(p, 0, how) for p in rows[:MIN_EXCERPTS]])
    while len(index) > MIN_EXCERPTS and _cost(index) + floor_cost > budget:
        index = index[:max(MIN_EXCERPTS, len(index) - max(1, len(index) // 10))]
    spent = _cost(index)

    shown = rows[:PAGES_CAP]
    while shown:
        frame = _cost([_summary_page(p, 0, how) for p in shown])
        if spent + frame + len(shown) * EXCERPT_FLOOR <= budget:
            break
        shown = shown[:len(shown) - max(1, len(shown) // 5)]
    if not shown:
        return [], index, MARKDOWN_CAP, len(index)
    frame = _cost([_summary_page(p, 0, how) for p in shown])
    cap = max(EXCERPT_FLOOR, min(MARKDOWN_CAP, (budget - spent - frame) // len(shown)))
    return [_summary_page(p, cap, how) for p in shown], index, cap, len(index)


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


def _walk(job, deadline=None, **how):
    """Up to `INDEX_CAP` pages, and the cursor the next window starts at.

    Two slices of one generator rather than one slice of a list. `pages()`
    fetches in batches of `PAGES_CAP`, so once the first `PAGES_CAP` rows have
    been taken the envelope is that batch's and its cursor is the position just
    past them -- which is exactly what a caller asking for the next window of
    excerpts resumes from. Walking the whole crawl and slicing afterwards
    throws that position away, which is why the answer used to tell an
    assistant to pass a cursor and then not give it one.
    """
    walk = job.pages(limit=PAGES_CAP, **how)
    first = list(islice(walk, PAGES_CAP))
    tail = (job.envelope.get("cursor") or "") if len(first) == PAGES_CAP else ""
    rows = list(first)
    # The rest a batch at a time, so a hosted `deadline` stops the walk on a
    # batch boundary: what was read is whole, and the cursor still resumes
    # after the first batch. Hosted, the budget covers this as well as the wait.
    while len(first) == PAGES_CAP and len(rows) < INDEX_CAP:
        if deadline is not None and time.monotonic() >= deadline:
            break
        batch = list(islice(walk, min(PAGES_CAP, INDEX_CAP - len(rows))))
        rows.extend(batch)
        if len(batch) < PAGES_CAP:
            break
    return rows, tail


def _crawl_result(job, pages, window=""):
    """A finished crawl, shaped once. `crawl_site` and `get_job` both call
    this, so the two cannot drift apart.

    `index` is every page the walk saw; `pages` is the first `PAGES_CAP` of
    them with an excerpt each. A two-hundred page crawl came back at 2.2
    million characters before this -- more than a client will load, let alone
    a context window -- so the answer is now a map with samples on it, and two
    ways to drill in: one page in full, or the next window of pages.
    """
    e = job.envelope
    rows = [p for p in pages if isinstance(p, dict)][:INDEX_CAP]
    # Build it, weigh it, and if the shell tipped it over, hand the overshoot
    # back as a reserve and build again. Two passes settle it; the loop is
    # bounded because each pass reserves strictly more.
    reserve, out = 0, None
    for _attempt in range(4):
        out = _shape_crawl(job, e, rows, window, reserve)
        over = _cost(out) - RESULT_BUDGET
        if over <= 0:
            break
        reserve += over + 64
    return out


def _shape_crawl(job, e, rows, window, reserve):
    """One pass at the answer, at the budget `reserve` leaves."""
    shaped, index, cap, kept = _excerpts(rows, "call get_job with this page's url", reserve)
    notes = []
    if any(isinstance(p.get("markdown"), str) and len(p["markdown"]) > cap for p in rows[:len(shaped)]):
        notes.append("the excerpts are cut: call get_job with this crawl's id and "
                     "url=<a page's url> for that page in full")
    if kept < len(rows):
        notes.append(f"the index lists {kept} of the {len(rows)} pages read, to stay inside "
                     f"{RESULT_BUDGET:,} characters")
    # The cursor is a batch boundary -- it resumes the walk after the first
    # PAGES_CAP rows -- so the note says that rather than "the next window",
    # which would read as "the pages after the ones excerpted here" and be
    # wrong by however many of that batch the budget could not excerpt. Those
    # are in the index either way. And it is only mentioned when it is here:
    # the answer used to name a cursor it did not carry.
    more = len(rows) > len(shaped) or bool(e.get("next"))
    window = window if more else ""
    if more:
        notes.append("more pages than are excerpted here. Every page read is in the index, and "
                     "get_job with url=<a page's url> reads any one in full"
                     + (f"; get_job with cursor= continues after the first {PAGES_CAP} read"
                        if window else ""))
    if e.get("ephemeral"):
        notes.append("this crawl is kept for a day unless keep_crawl_as_project is called")
    out = {"crawl_id": job.id, "status": e.get("status"), "url": e.get("url", ""),
           "pages": shaped, "index": index, "counts": e.get("counts"),
           "stop": e.get("stop", ""), "note": ". ".join(notes)}
    if window:
        out["cursor"] = window
    return out


def _batch_result(b):
    """A finished multi-URL scrape, shaped once.

    Shaped like a crawl, for the same reason: five hundred URLs at a full page
    each is not an answer. One URL is the exception -- `scrape_urls` with a
    single url returns the whole page, because that is plainly what was asked
    for.
    """
    rows = [p for p in b.get("pages", []) if isinstance(p, dict)][:INDEX_CAP]
    base = {k: v for k, v in b.items() if k != "pages"}
    if isinstance(base.get("runs"), list):
        # One run per url on a small host: five hundred of them came to seventy
        # thousand characters before a page was shown, and every page row
        # already says how it went. A count by status is what is left of them.
        by_status: dict = {}
        for r in base["runs"]:
            if isinstance(r, dict):
                by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1
        base["runs"] = {"count": len(base["runs"]), "byStatus": by_status}
    # Built, weighed, and rebuilt with the overshoot reserved, as a crawl is.
    reserve, out = 0, None
    for _attempt in range(4):
        out = _shape_batch(base, rows, reserve)
        over = _cost(out) - RESULT_BUDGET
        if over <= 0:
            break
        reserve += over + 64
    return out


def _shape_batch(base, rows, reserve):
    """One pass at a batch's answer, at the budget `reserve` leaves. The caller
    named every url and needs to know how each went: past the index, a count by
    status and the urls that did not come back ok."""
    pages, index, _cap, kept = _excerpts(rows, "call extract_url on it", reserve)
    out = {**base, "pages": pages, "index": index}
    rest = rows[kept:]
    if rest:
        by_status: dict = {}
        for p in rest:
            by_status[str(p.get("status"))] = by_status.get(str(p.get("status")), 0) + 1
        not_ok, size = [], 0
        for p in rest:
            if p.get("status") != "ok":
                row = {"url": p.get("url", ""), "status": p.get("status")}
                size += _cost(row)
                if size > REST_BUDGET:
                    break
                not_ok.append(row)
        out["rest"] = {"count": len(rest), "byStatus": by_status, "notOk": not_ok}
        out["note"] = ". ".join(n for n in (base.get("note"), (
            f"the index lists the first {kept} of {len(rows)} urls; `rest` counts the others by "
            "status and names those that did not come back ok. extract_url reads one again, "
            "and spends credits doing it")) if n)
    return out


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


def _scopes():
    """What this caller was approved for, hosted. None locally, where the
    server cannot know what the key behind it may do."""
    if not _HTTP:
        return None
    from mcp.server.auth.middleware.auth_context import get_access_token
    at = get_access_token()
    return list(getattr(at, "scopes", None) or []) if at else []


# Nine of the seventeen tools need write, including every one that fetches a
# page: fetching spends the workspace's credits, so it is not a read however
# it reads to an assistant asking for one URL. The API answers "this needs the
# member role", which is true and tells an assistant nothing it can act on --
# it does not know what a role is, that it has one, or that the person who
# approved the connection chose it.
READ_ONLY = (
    "this connection was approved read-only. It can read what the workspace has "
    "already stored -- projects, pages, change records, search, get_job -- but it "
    "cannot fetch a new page or change anything: those reach the site or alter the "
    "workspace, and most of them spend credits. "
    "Ask the person to reconnect the app and tick write access."
)


# The API returns a project's webhook signing secret with the project, which
# is right for a program that is going to verify signatures with it. A tool's
# answer is not that: it goes through somebody's AI app and into a model's
# context, where a shared secret has no business being and nothing can use it.
# Dropped here, in the one place every tool's answer passes through, so a tool
# added later cannot leak it either -- and not at the API, which the SDK and
# the app still need it from.
WEBHOOK_SECRET_NOTE = ("withheld from assistants; the signing secret is on the "
                       "project's page in the MeshArc app")


def _no_secret(out):
    if isinstance(out, dict) and out.get("webhookSecret"):
        out = {k: v for k, v in out.items() if k != "webhookSecret"}
        out["webhookSecretNote"] = WEBHOOK_SECRET_NOTE
    return out


def _safe(fn):
    try:
        return _no_secret(fn())
    except MeshArcError as exc:
        out = {"error": exc.detail, "status": exc.status}
        # A 403 carrying the status's own default code is the role check. The
        # named ones -- suspended, email_unverified, mfa_required -- are other
        # refusals and have to keep saying what they say.
        #
        # "no code at all" was the first version of this test, and it never
        # fired: the API fills a code on every error (api/app.py _error_body),
        # so a live read-only connection still heard about roles. Checked
        # against the running API rather than reasoned about, which is the
        # only reason it is right now.
        scopes = _scopes()
        if (exc.status == 403 and (exc.code or "") in ("", "forbidden")
                and scopes is not None and "write" not in scopes):
            out = {"error": READ_ONLY, "status": 403, "code": "read_only", "detail": exc.detail}
        return out
    except Exception as exc:                          # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@tool(title="Scrape a list of URLs",
      annotations=_acts(idempotent=True),
      description="Fetch known URLs (1 to 500) once and return each page's content, markdown by default; no project "
                  "is created. For one page with every format or browser steps use extract_url; to find pages by "
                  "following links use crawl_site; to list a site's URLs without fetching them use map_site. "
                  "Each page costs the credits of the engine that read it (1 plain fetch, 4 browser render), and a "
                  "page the site refuses is free. A single URL answers in the same request; a list waits for the "
                  "batch, and a large one comes back as an index with excerpts inside 60,000 characters. Hosted, a "
                  "batch still going after the time budget comes back as a job for get_job, and repeating the same "
                  "call returns that job instead of starting another.")
def scrape_urls(urls: Annotated[list[str], Field(
                    description="The pages to fetch: 1 to 500 absolute http(s) URLs.")],
                config: FetchConfig = None,
                formats: Annotated[str, Field(
                    description="Comma-separated bodies to return per page, from markdown (default), text, "
                                "cleanHtml and rawHtml, e.g. 'markdown,text'.")] = "markdown",
                parse_documents: ParseDocuments = True) -> dict:
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


@tool(title="Extract one page in full",
      annotations=_acts(),
      description="Fetch one URL exactly as a project with the given settings would, and return the whole page: "
                  "the bodies config's formats ask for plus the raw html (each capped at 12,000 characters), head "
                  "and extracted fields, its images, and which engine read it -- without its link list. Use it "
                  "for a single page that needs browser steps, structured fields or a settings trial before "
                  "create_project; for plain content of one or many known URLs use scrape_urls, and get_page "
                  "reads a page a project already stored without fetching. It climbs the fetch ladder -- plain "
                  "http first, a browser only when the page needs one or render_js is 'always' -- and costs the "
                  "credits of the rung that read it (1 to 4 for most pages, +1 when formats ask for a "
                  "screenshot). Browser `actions` in config (click, type, select, press, wait, scroll; repeat "
                  "'until_gone' for Load-more buttons; `each` to act on every match) run before the page is read.")
def extract_url(url: Annotated[str, Field(description="The absolute http(s) URL of the page to read.")],
                config: FetchConfig = None,
                parse_documents: ParseDocuments = True) -> dict:
    def go():
        with _client() as s:
            r = s.extract(url, config=_with_documents(config, parse_documents))
            if r.get("page"):
                r["page"] = _trim_page(r["page"])
                r["page"].pop("links", None)
            return r
    return _safe(go)


@tool(title="Map a site's declared URLs",
      annotations=_acts(),
      description="List every URL a site declares in its sitemaps -- found through robots.txt and the well-known "
                  "paths, each index file walked to its children -- without fetching any of the pages. Use it "
                  "first, to see how big a site is and what sections it has, before crawl_site or create_project; "
                  "it cannot read page content (scrape_urls or crawl_site do) and misses pages a site links to but "
                  "does not declare. Costs 1 credit per sitemap file read, usually 1 in total, never per URL. "
                  "Returns the discovery method, totals, up to `limit` URLs and the section names.")
def map_site(url: Annotated[str, Field(description="Any URL on the site, usually its home page.")],
             search: Annotated[str | None, Field(
                 description="Keep only URLs containing this text, e.g. '/blog/'; omit for all.")] = None,
             limit: Annotated[int, Field(
                 description="The most URLs to return, 1 to 5,000 (default 1,000); totals still count them all.")]
             = 1000) -> dict:
    def go():
        with _client() as s:
            out = s.map_details(url, search=search, limit=min(limit, 5000))
            return {"url": out["url"], "method": out["method"], "totals": out["totals"],
                    "urls": [u["url"] for u in out["data"]],
                    "sections": sorted({u.get("section", "") for u in out["data"] if u.get("section")})}
    return _safe(go)


@tool(title="Crawl a whole site once",
      annotations=_acts(idempotent=True),
      description="Crawl a site once from a start URL -- following its links and reading its sitemap, up to "
                  "`limit` pages -- and return what it found, without setting up a project. Use it to read a site "
                  "or section whose page URLs you do not know; for known URLs use scrape_urls, and to watch a site "
                  "over time use create_project (or keep_crawl_as_project on this crawl afterwards). Each page "
                  "costs the credits of the engine that read it (usually 1 to 4) and refused pages are free. It "
                  "waits for the crawl, minutes for a large limit; hosted, a crawl still going after the time "
                  "budget comes back as a job for get_job, and repeating the call returns the same crawl. The "
                  "answer is an index of the pages read plus excerpts inside 60,000 characters; get_job with "
                  "`url` reads one page in full and with `cursor` the next window. The crawl is kept for a day; "
                  "pass the result's crawl_id to keep_crawl_as_project to keep it for good.")
def crawl_site(url: Annotated[str, Field(description="The absolute http(s) URL to start from, e.g. the home page.")],
               limit: Annotated[int, Field(
                   description="The most pages to read, 1 to 5,000 (default 50); the plan's page cap also applies.")]
               = 50,
               max_depth: Annotated[int, Field(
                   description="How many links deep to follow from the start URL (0 = that page only; default 3).")]
               = 3,
               include_paths: Annotated[list[str] | None, Field(
                   description="Globs over the URL path to keep, e.g. ['/blog/*'] for a section (its index "
                               "included); a bare '/blog/' matches only that one page. Omit for the whole site.")]
               = None,
               exclude_paths: Annotated[list[str] | None, Field(
                   description="Globs over the URL path to skip, e.g. ['/tag/*', '*.pdf']; an exclude wins over "
                               "an include.")] = None,
               config: FetchConfig = None) -> dict:
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
            # Waited for first, then walked. `_walk` takes its cursor after the
            # first PAGES_CAP rows, which is a batch boundary only when every
            # batch comes back full -- on a finished crawl. Walked while it ran,
            # a short first batch put the cursor past rows it had not read.
            # The wait is up to an hour by default. That is right down a pipe
            # and wrong down a socket, so hosted mode gives it a budget and
            # hands back the job id instead.
            budget = _budget()
            deadline = None if budget is None else time.monotonic() + budget
            try:
                if budget is None:
                    job.wait()
                else:
                    job.wait(timeout=budget)
            except MeshArcTimeoutError:
                # The budget -- or, down a pipe, the hour -- not a failure: the
                # crawl is still going, and get_job picks it up by this id.
                return _still_running("crawl", job.id, job.envelope.get("counts"))
            return _crawl_result(job, *_walk(job, deadline=deadline, wait=False))
    return _safe(go)


@tool(title="Keep a crawl as a project",
      annotations=_acts(open_world=False),
      description="Turn a crawl_site crawl into a project, so the site is watched over time and each later run "
                  "is compared against this one. Nothing is fetched again and it costs no credits: the crawl's "
                  "pages become the project's first run. Use it after crawl_site when the site is worth watching; "
                  "to start a watched site from scratch use create_project. Without it a crawl and its pages "
                  "expire after a day. It counts toward the plan's project limit (refused with plan_limit when "
                  "full). Returns the new project, whose id the other project tools take.")
def keep_crawl_as_project(crawl_id: Annotated[str, Field(
                              description="The crawl_id a crawl_site answer (or get_job on a crawl) returned.")],
                          name: Annotated[str | None, Field(
                              description="A name for the project; omit to keep the crawl's own name.")] = None,
                          schedule: Annotated[Schedule, Field(
                              description="How often the project re-crawls on its own; 'manual' (default) runs "
                                          "only when start_run is called. Every run spends credits.")]
                          = "manual") -> dict:
    def go():
        with _client() as s:
            return s.get_crawl(crawl_id).keep(name=name, schedule=schedule)
    return _safe(go)


@tool(title="List all projects",
      annotations=READS_STORED,
      description="List every project in the workspace -- the sites it watches over time -- with each one's id, "
                  "name, seed URL, host, schedule, page count, coverage, last run and health. Call it first to "
                  "find a project_id for the other project tools, or to check whether a site is already watched "
                  "before create_project; get_project gives one project's full settings. Reads only what is "
                  "stored: free, and it fetches nothing. A key limited to some projects sees only those.")
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
    "max_tier": "the highest rung allowed: 'auto' (as high as the plan allows), 'http', 'browser', 'stealth'",
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


@tool(title="Describe project settings",
      annotations=READS_STORED,
      description="List the project settings an assistant can set -- each key with its meaning and its default "
                  "-- plus the valid schedules and how path globs work. Read it before create_project, "
                  "update_project or a `config` argument whenever the request names a section, schedule, format, "
                  "limit or behaviour: keys are exact and guessed names are refused. It describes settings in "
                  "general; get_project shows the values one project has. Free, and it fetches nothing.")
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


@tool(title="Get one project",
      annotations=READS_STORED,
      description="Read one project in full: its settings (config), schedule, retention, coverage and last run. "
                  "Read it before update_project, to see the values you are about to change; list_projects is "
                  "the lighter way to find projects, and list_pages or get_changes read what its runs found. "
                  "Free, fetches nothing; an unknown or hidden project answers 404. The webhook signing secret "
                  "is withheld from the answer.")
def get_project(project_id: ProjectId) -> dict:
    return _safe(lambda: _client().projects.get(project_id))


@tool(title="Create a watched project",
      annotations=_acts(),
      description="Create a project that watches a site over time: it is crawled on its schedule and each run is "
                  "compared with the last, recording pages added, modified and removed. Use it when the request "
                  "is to monitor or track a site; for a one-off read use crawl_site (keep_crawl_as_project can "
                  "turn that into a project later), and check list_projects first so the site is not added "
                  "twice. Creating it reads the site's robots.txt and sitemaps but fetches no pages; every run "
                  "then spends credits per page. Refused when the plan's project limit is reached. Returns the "
                  "project, whose id start_run takes to crawl it now.")
def create_project(seed: Annotated[str, Field(
                       description="Where the crawl starts: a public site's URL or bare domain, e.g. "
                                   "'https://example.com/blog/' or 'example.com'. The project covers that host.")],
                   name: Annotated[str | None, Field(
                       description="A name for the project; omit to use the site's host name.")] = None,
                   schedule: Annotated[Schedule, Field(
                       description="How often it re-crawls on its own; 'manual' (default) runs only when "
                                   "start_run is called.")] = "manual",
                   config: Annotated[dict | None, Field(
                       description="Any subset of the settings describe_project_config lists. For one section "
                                   "of a site pass include_paths, e.g. {\"include_paths\": [\"/blog/*\"]}; "
                                   "map_site shows the site's sections first. Omit for the defaults.")]
                   = None) -> dict:
    return _safe(lambda: _client().projects.create(seed, name=name, schedule=schedule, config=config))


@tool(title="Update a project's settings",
      annotations=_acts(destructive=True, idempotent=True, open_world=False),
      description="Change an existing project's name, schedule or settings. Only what you pass changes: in "
                  "`config` only the keys given are replaced, the rest stay; a replaced value is not kept. Call "
                  "get_project first to see the current values, and describe_project_config for valid keys; to "
                  "start a different site use create_project instead. Nothing is fetched and no credits are "
                  "spent now; the next run uses the new settings, and a settings change that alters how pages "
                  "are read starts a fresh comparison baseline. Returns the updated project.")
def update_project(project_id: ProjectId,
                   name: Annotated[str | None, Field(description="A new name; omit to leave it.")] = None,
                   schedule: Annotated[Schedule | None, Field(
                       description="A new schedule; omit to leave it.")] = None,
                   config: Annotated[dict | None, Field(
                       description="Settings to change, e.g. {\"max_pages\": 200}; keys not given keep their "
                                   "values. Omit to leave them all.")] = None) -> dict:
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


@tool(title="Start a project run now",
      annotations=_acts(),
      description="Crawl a project's site now with its saved settings, outside its schedule; the run is then "
                  "compared with the last one. Use it after create_project or update_project, or when fresh "
                  "results are wanted; to re-read only a few pages use recrawl_pages, and for a site with no "
                  "project use crawl_site. Every page read costs credits (usually 1 to 4) and a run stops when "
                  "the credit budget runs out, keeping what it read; it is refused with 409 while a run of the "
                  "project is already queued or running, and 402 when no credits are left. Without wait it "
                  "returns the queued run at once; with wait it returns the finished run, except hosted, where "
                  "a run still going after "
                  "the time budget comes back as a job for get_job. Results are read with list_pages and "
                  "get_changes.")
def start_run(project_id: ProjectId,
              wait: Annotated[bool, Field(
                  description="true waits for the run to finish (minutes for a large site); false (default) "
                              "returns as soon as it is queued.")] = False) -> dict:
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


@tool(title="List a run's pages",
      annotations=READS_STORED,
      description="List the pages one project run stored -- the last finished run unless run_id is given -- "
                  "with each page's url, status, depth, word count and when it last changed, up to 500 rows, "
                  "plus the runId. Use it to see what a crawl covered or to pick a URL; get_page reads one of "
                  "them in full, search_pages finds pages by their text, and get_changes lists only what "
                  "changed. Free and reads only what is stored: it never fetches the site. A project with no "
                  "finished run answers with no pages.")
def list_pages(project_id: ProjectId, run_id: RunId = None) -> dict:
    def go():
        r = _client().pages(project_id, run_id)
        r["pages"] = r.get("pages", [])[:500]
        return r
    return _safe(go)


@tool(title="Get one stored page",
      annotations=READS_STORED,
      description="Read one page a project run stored, in full: its markdown and other bodies (each capped at "
                  "12,000 characters), head fields (title, description, canonical, h1), extracted fields, and "
                  "its versions across runs. Use it when you know the page's URL; list_pages or search_pages "
                  "finds the URL first, and for a page from a crawl_site crawl use get_job with url instead. "
                  "Free and reads only what is stored, so it never fetches the site -- use extract_url for a "
                  "live copy. A URL the run did not store answers 404.")
def get_page(project_id: ProjectId,
             url: Annotated[str, Field(
                 description="The page's full URL as list_pages or search_pages shows it; the other scheme or "
                             "trailing slash is matched too.")],
             run_id: Annotated[str | None, Field(
                 description="A run's id, from start_run or a list_pages answer, to read that run's copy; leave "
                             "empty for the newest copy across the project's recent runs.")] = None) -> dict:
    return _safe(lambda: _trim_page(_client().page(project_id, url, run_id)))


@tool(title="Get what changed in a run",
      annotations=READS_STORED,
      description="Report what changed in a project run against the run before it -- the last finished run "
                  "unless run_id is given: pages added, modified and removed, head-field changes (title, "
                  "description, canonical...), and the run's coverage, with up to 200 entries per list plus the "
                  "runs a record exists for. Use it to answer 'what changed on the site'; list_pages shows every "
                  "page whatever changed, and get_page shows one page's text and versions. Removals are withheld "
                  "when the crawl reached under 90% of the site, so a blocked crawl never reports the site as "
                  "gone. A project's first run is a baseline with nothing to compare. Free and reads only what "
                  "is stored.")
def get_changes(project_id: ProjectId, run_id: RunId = None) -> dict:
    def go():
        r = _client().changes(project_id, run_id)
        ch = r.get("change") or {}
        for k in ("feed", "fields", "withheld"):
            if isinstance(ch.get(k), list):
                ch[k] = ch[k][:200]
        return r
    return _safe(go)


@tool(title="Search a run's stored pages",
      annotations=READS_STORED,
      description="Find which pages of a project run contain some text or an element -- the last finished run "
                  "unless run_id is given -- returning the matching URLs with how many pages were scanned. Use "
                  "it to locate pages by what they say or contain; list_pages lists every page and get_page "
                  "then reads one in full. It scans the stored copies and never fetches the site, so it is free "
                  "and as fresh as the run. A selector search needs the run to have kept html (the rawHtml "
                  "format); when none was kept the answer says so rather than reporting no matches.")
def search_pages(project_id: ProjectId,
                 q: Annotated[str, Field(
                     description="What to look for, 1 to 200 characters. In content mode every word must "
                                 "appear and \"quoted phrases\" match as written; in selector mode a CSS "
                                 "selector, or an XPath starting with / or (.")],
                 mode: Annotated[Literal["content", "selector"], Field(
                     description="'content' (default) searches the extracted markdown; 'selector' searches the "
                                 "stored html.")] = "content",
                 run_id: RunId = None) -> dict:
    return _safe(lambda: _client().search(project_id, q, mode=mode, run_id=run_id))


@tool(title="Re-crawl chosen pages",
      annotations=_acts(),
      description="Fetch specific pages of a project again now, as a small run of their own whose record "
                  "compares just those pages with the last full run. Use it to check a few pages you expect "
                  "changed without crawling the whole site; start_run re-crawls everything, and extract_url "
                  "reads a page outside any project. Each page costs credits (usually 1 to 4). The URLs must be "
                  "on the project's site; refused with 409 while another run of the project is queued or "
                  "running. Returns the queued run; follow it with get_job (kind 'run') and read results with "
                  "get_changes.")
def recrawl_pages(project_id: ProjectId,
                  urls: Annotated[list[str], Field(
                      description="1 to 500 full URLs on the project's site to fetch again.")]) -> dict:
    return _safe(lambda: _client().recrawl(project_id, urls))


@tool(title="Follow a long-running job",
      annotations=READS_STORED,
      description="Check on a job a long tool handed back -- a crawl from crawl_site, a run from start_run or "
                  "recrawl_pages, or a batch from scrape_urls -- and, once it has finished, return the same "
                  "result the original tool would have given; while it is still going, its status and counts. "
                  "Call it when a tool answered with status 'running' and a job; calling the original tool "
                  "again is not needed and would not start a second job. For a crawl it is also how to read "
                  "further: `url` returns one page in full (even mid-crawl) and `cursor` the next window of "
                  "pages. For a project's stored pages use get_page instead. Free: it only reads stored "
                  "results and never fetches.")
def get_job(kind: Annotated[Literal["crawl", "run", "batch"], Field(
                description="What the job is: 'crawl' (crawl_site), 'run' (start_run, recrawl_pages) or "
                            "'batch' (scrape_urls); the 'job' object a tool returned names it.")],
            id: Annotated[str, Field(
                description="The job's id, from that 'job' object, or a crawl_site answer's crawl_id.")],
            project_id: Annotated[str | None, Field(
                description="Required for a run: the project it belongs to (the 'job' object carries it). "
                            "Ignored for a crawl or a batch.")] = None,
            url: Annotated[str | None, Field(
                description="Crawls only: one page's URL from the crawl's index, to read that page in full.")]
            = None,
            cursor: Annotated[str | None, Field(
                description="Crawls only: the cursor a previous crawl answer returned, to read the next "
                            "window of pages.")] = None) -> dict:
    """The other half of a bounded wait, and the way into a large crawl.

    A tool that stopped waiting has to leave something to come back to, and
    this is it. The result of a finished job is shaped by the same helpers the
    original tool uses, so what an assistant gets by polling is what it would
    have got by waiting.

    A crawl result is a map with samples on it, which leaves two things to ask
    for afterwards and both arrive here. `url` is one page in full -- readable
    while the crawl is still going, because a page that has been crawled is
    stored. `cursor` is the next window of pages.
    """
    def go():
        s = _client()
        if kind == "crawl":
            job = s.get_crawl(id)
            if url:
                # Readable before the crawl ends, and the link list goes: a
                # reader asking for one page wants the page, not its outbound
                # links, which is what made the crawl result unloadable.
                page = job.page(url)
                if isinstance(page, dict):
                    page.pop("links", None)
                return _trim_page(page)
            if _running(job.envelope.get("status")) and not cursor:
                return _still_running("crawl", id, job.envelope.get("counts"))
            # Finished, however it finished -- `error` and `cancelled` included.
            # An assistant has to be told a crawl failed, not kept polling.
            budget = _budget()
            pages, window = _walk(job, deadline=None if budget is None else time.monotonic() + budget,
                                  wait=False, cursor=cursor)
            return _crawl_result(job, pages, window)
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


# Stdio's server, built now that the seventeen are declared. Hosted mode
# builds its own in `authorize`, because auth is set in the constructor.
server = _build()


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


def authorize(issuer, public, secret):
    """The hosted server: the same seventeen tools, with OAuth attached.

    A server of its own rather than the stdio one with auth bolted on after
    the fact. `token_verifier` and `auth` are constructor arguments, and
    setting them afterwards means writing to a private attribute -- which
    works, and silently does nothing the day it is renamed. What it would do
    is leave the app with no auth middleware at all, serving every request
    that carries no token. Passing them to the constructor cannot fail that
    way: it validates the pair, and refuses one without the other.

    The module's `server` is rebound so there is one answer to which server is
    serving, and the verifier comes back so its secret can be probed before
    anything is served.
    """
    global server
    from mcp.server.auth.settings import AuthSettings

    # Pydantic URL types, not strings: AuthSettings declares AnyHttpUrl, and a
    # bad value should be refused here rather than at the first request.
    from pydantic import AnyHttpUrl
    verifier = _IntrospectionVerifier(issuer, public, secret)
    server = _build(
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer),
            resource_server_url=AnyHttpUrl(public),
            required_scopes=["read"],
            # Explicit in 2.x, the default in 3.0. Without it, a token minted
            # for another resource would be accepted here.
            validate_token_resource=True,
        ),
    )
    return verifier


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

    verifier = authorize(issuer, public, secret)
    _check_secret(verifier)
    _HTTP = True

    from urllib.parse import urlparse
    server.run("streamable-http", host=args.host, port=args.port,
               streamable_http_path=urlparse(public).path or "/mcp",
               transport_security=_transport_security(public))


if __name__ == "__main__":
    main()
