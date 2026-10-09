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
from typing import Annotated, Any, Literal

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
    from pydantic import ConfigDict, Field, with_config
    from typing_extensions import TypedDict
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
    "Use scrape_urls for pages whose URLs you have (full=true for one page with every format), "
    "map_site to see what URLs a site declares before fetching any of them, and "
    "crawl_site to crawl a whole site once without setting a project up first "
    "(create_project with its crawl_id turns it into a watched project afterwards). "
    "The project tools are for a site watched over time: its runs, its pages, search inside a run, "
    "and its change record. search_web searches the web for pages whose URLs you do not know; "
    "list_pages with q searches only inside a project's run. "
    "run_agent hands a research question to an agent that searches and reads pages on its own, "
    "returning a job for get_job; continue_agent carries on a run that stopped at its credit limit. "
    "A long tool may hand back a job: get_job follows it and cancel_job stops it. "
    "Blocked pages are reported as blocked, never as missing."
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
ProjectId = Annotated[str, Field(description="The project's 32-character hex id, from list_projects.")]
RunId = Annotated[str | None, Field(
    description="A run's id, from list_runs; empty means the project's latest finished run.")]
Schedule = Literal["manual", "hourly", "daily", "weekly"]
FetchConfig = Annotated[dict | None, Field(
    description="Fetch settings to override, as {key: value}; omit for the defaults.")]
ParseDocuments = Annotated[bool, Field(
    description="Read PDF, Word and spreadsheet URLs as text (default true). A parse_documents key in config "
                "wins.")]


# What each tool answers, as output schemas. Every field is optional and any
# other field the API adds comes through as it is: these describe the answer
# for the assistant reading it, they do not filter it -- a schema that dropped
# a field the API grew later would hide it, and one that typed a field the API
# changed would turn a good answer into an error. An error has the same shape
# for every tool, so each schema carries it.
_ERR = "Only when the call failed: what went wrong, in words an assistant can act on."
_CODE = "Only on a failure: a machine-readable reason, e.g. read_only, validation, plan_limit or not_found."
_DETAIL = "Only on a failure: the API's own wording, when the error was rephrased."


@with_config(ConfigDict(extra="allow"))
class ScrapeOut(TypedDict, total=False):
    """A scrape. A list: status, the pages with excerpts and an index of every url. One URL: that page in
    full. full=true: the extraction, with the page under `page`. A batch still going: status 'running' and
    a job."""
    status: Annotated[Any, Field(description=(
        "'done' once read; 'running' with a `job` while a batch goes on; with full=true, 'blocked' when the site "
        "refused and 'error' when the read failed. On an error, the HTTP status code."))]
    pages: Annotated[Any, Field(description=(
        "The pages read. A list: up to 50, each {url, title, words, status, links (a count), markdown excerpt}, the "
        "excerpts sharing 60,000 characters. One URL: that page in full, each body asked for capped at 12,000 "
        "characters."))]
    index: Annotated[Any, Field(description=(
        "A list only: every url as {url, title, words, status}, so none is lost when excerpts are cut."))]
    rest: Annotated[Any, Field(description="A list past its index: {count, byStatus, notOk} for the urls not shown.")]
    page: Annotated[Any, Field(description=(
        "full=true only: the page in full -- markdown, html, head {title, description, canonical, h1}, fields, "
        "images, engine, pageStatus -- with each body capped at 12,000 characters."))]
    job: Annotated[Any, Field(description=(
        "While a batch is still going: {kind: 'batch', id} for get_job and cancel_job."))]
    note: Annotated[Any, Field(description="What was left out and how to get it.")]
    error: Annotated[Any, Field(description=_ERR)]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class MapOut(TypedDict, total=False):
    """The URLs a site's sitemaps declare."""
    url: Annotated[Any, Field(description="The site that was mapped.")]
    method: Annotated[Any, Field(description="How the sitemaps were found, e.g. through robots.txt.")]
    totals: Annotated[Any, Field(description=(
        "Counts over every declared URL and sitemap file, whatever `limit` returned."))]
    urls: Annotated[Any, Field(description="The declared URLs, at most `limit`, filtered by `search`.")]
    sections: Annotated[Any, Field(description=(
        "The section names the sitemaps group URLs under, e.g. 'blog'; empty when there are none."))]
    error: Annotated[Any, Field(description=_ERR)]
    status: Annotated[Any, Field(description="Only on a failure: the HTTP status code.")]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class CrawlOut(TypedDict, total=False):
    """A crawl: the pages read, as an index plus excerpts, or a job when it is still going."""
    crawl_id: Annotated[Any, Field(description=(
        "This crawl's id: for get_job, cancel_job, and create_project(crawl_id=...) to keep it."))]
    status: Annotated[Any, Field(description=(
        "'done', 'cancelled' or 'error' once finished, 'running' with a `job` while it goes on. On an error, the "
        "HTTP status code."))]
    url: Annotated[Any, Field(description="The start URL.")]
    pages: Annotated[Any, Field(description=(
        "Up to 50 pages, each {url, title, words, status, links (a count), markdown excerpt}."))]
    index: Annotated[Any, Field(description="Every page read (up to 500) as {url, title, words, status}.")]
    counts: Annotated[Any, Field(description="Pages by outcome, e.g. {ok, blocked, error}.")]
    stop: Annotated[Any, Field(description="Why the crawl ended early, e.g. its page limit; empty otherwise.")]
    cursor: Annotated[Any, Field(description=(
        "Present when more pages exist: pass it to get_job for the next window."))]
    job: Annotated[Any, Field(description="While it is still going: {kind: 'crawl', id} for get_job and cancel_job.")]
    note: Annotated[Any, Field(description="What was cut, and how to read a page in full.")]
    error: Annotated[Any, Field(description=_ERR)]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class ProjectOut(TypedDict, total=False):
    """A project, as get_project, create_project and update_project answer with it; get_project with no
    project_id answers with the settings any project can take instead."""
    id: Annotated[Any, Field(description="The project's 32-character hex id.")]
    name: Annotated[Any, Field(description="Its display name.")]
    seed: Annotated[Any, Field(description="Where its crawls start.")]
    host: Annotated[Any, Field(description="The site it covers; fixed at creation.")]
    scheduleKey: Annotated[Any, Field(description="manual, hourly, daily or weekly.")]
    config: Annotated[Any, Field(description=(
        "Its settings as {key: value}; secrets such as chat_url come back masked."))]
    retention: Annotated[Any, Field(description="How long runs are kept, e.g. '90d'.")]
    pagesN: Annotated[Any, Field(description="Pages its last finished run read.")]
    coverage: Annotated[Any, Field(description=(
        "Percent of the site the last run reached; under 90 means removals are withheld."))]
    status: Annotated[Any, Field(description=(
        "draft, crawling, healthy or failing. On an error, the HTTP status code."))]
    lastRunAt: Annotated[Any, Field(description="When the last run started (ISO 8601), or null.")]
    nextRunAt: Annotated[Any, Field(description="When the schedule runs it next, or null for manual.")]
    changeSummary: Annotated[Any, Field(description=(
        "The last compared run in words, e.g. '3 new · 2 changed · 0 removed'."))]
    webhooks: Annotated[Any, Field(description=(
        "get_project only: {url, events, deliveries}: where run and change events are POSTed, which events, and "
        "the last 10 deliveries, each {id, event, status (queued, retrying, delivered or failed), attempts, "
        "lastStatus, lastError, createdAt, deliveredAt}. The signing secret is never included."))]
    webhookTest: Annotated[Any, Field(description=(
        "update_project with test_webhook only: {queued, id} when a test run.finished message was queued, or "
        "{error, status} when the API refused it, e.g. with no webhook URL saved."))]
    settings: Annotated[Any, Field(description=(
        "No project_id only: every common setting as {key, meaning, default}."))]
    other_keys: Annotated[Any, Field(description="No project_id only: further keys accepted but rarely needed.")]
    schedules: Annotated[Any, Field(description="No project_id only: the valid schedules.")]
    note: Annotated[Any, Field(description="No project_id only: how path globs work.")]
    error: Annotated[Any, Field(description=_ERR)]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class DeleteOut(TypedDict, total=False):
    """What a delete removed."""
    id: Annotated[Any, Field(description="The deleted project's id.")]
    name: Annotated[Any, Field(description="Its name, as confirmed.")]
    deleted: Annotated[Any, Field(description="true once the project and its history are gone.")]
    note: Annotated[Any, Field(description="What was removed with it.")]
    error: Annotated[Any, Field(description=_ERR)]
    status: Annotated[Any, Field(description="Only on a failure: the HTTP status code.")]
    code: Annotated[Any, Field(description=(
        "Only on a failure: e.g. admin_only (the key is not an admin's), validation (confirm_name did not "
        "match) or not_found."))]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class ProjectsOut(TypedDict, total=False):
    """Every project the key can see."""
    projects: Annotated[Any, Field(description=(
        "Oldest first, each {id, name, seed, host, schedule, pages, coverage, lastRun, status}; status is draft, "
        "crawling, healthy or failing."))]
    workspace: Annotated[Any, Field(description=(
        "{plan, creditsLeft, creditsSpentThisMonth, oneTimeAllowance}: creditsLeft is what may still be spent, "
        "this month or, on a one-time allowance, ever; null means no limit. {note} instead when the balance could "
        "not be read."))]
    error: Annotated[Any, Field(description=_ERR)]
    status: Annotated[Any, Field(description="Only on a failure: the HTTP status code.")]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class RunOut(TypedDict, total=False):
    """A run of a project, or a job when a wait ran out first."""
    id: Annotated[Any, Field(description=(
        "The run's id: for get_job, cancel_job and the run_id of list_pages and get_changes."))]
    status: Annotated[Any, Field(description=(
        "running, complete, partial (some pages refused), failed or cancelled. On an error, the HTTP status code."))]
    queued: Annotated[Any, Field(description="true while it waits for a worker.")]
    trigger: Annotated[Any, Field(description="manual, scheduled, api or recrawl.")]
    scope: Annotated[Any, Field(description="How many URLs a start_run with urls listed; 0 for a full run.")]
    pagesN: Annotated[Any, Field(description="Pages read so far.")]
    changedN: Annotated[Any, Field(description=(
        "Pages added, modified or removed against the run before; null until compared."))]
    counts: Annotated[Any, Field(description="Pages by outcome, e.g. {ok, blocked, error}.")]
    coverage: Annotated[Any, Field(description="Percent of the known site this run reached.")]
    stop: Annotated[Any, Field(description="Why it ended, e.g. the credit budget; empty while running.")]
    startedAt: Annotated[Any, Field(description="When it started (ISO 8601).")]
    job: Annotated[Any, Field(description=(
        "When the wait ran out first: {kind: 'run', id, project_id} for get_job and cancel_job."))]
    note: Annotated[Any, Field(description="What to do next when it is still going.")]
    error: Annotated[Any, Field(description=_ERR)]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class RunsOut(TypedDict, total=False):
    """A project's runs, newest first."""
    runs: Annotated[Any, Field(description=(
        "Each {id, status, trigger, scope, pagesN, changedN, counts, coverage, stop, startedAt, elapsed, baseline, "
        "rebaselined}; baseline is a first run with nothing to compare, rebaselined one whose settings changed how "
        "pages read, so it was recorded rather than compared."))]
    error: Annotated[Any, Field(description=_ERR)]
    status: Annotated[Any, Field(description="Only on a failure: the HTTP status code.")]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class PagesOut(TypedDict, total=False):
    """A run's pages; with q the pages that match; with url one page in full."""
    runId: Annotated[Any, Field(description=(
        "The run these pages are from (with url, the run this copy is from); null when the project has no "
        "finished run."))]
    pages: Annotated[Any, Field(description=(
        "Listing: up to 500 rows, shallowest first, each {url, path, status, depth, words, changed, title, "
        "crawledAt}."))]
    linkGraph: Annotated[Any, Field(description=(
        "Listing: the run's link-graph summary (orphans, unlisted pages, dead ends), or null."))]
    hits: Annotated[Any, Field(description=(
        "With q: up to 500 matches, each {url, count (matches on the page), snippet}."))]
    scanned: Annotated[Any, Field(description="With q: pages searched.")]
    noBody: Annotated[Any, Field(description=(
        "With q: pages skipped because the body the mode needs was not kept (rawHtml for selector)."))]
    truncated: Annotated[Any, Field(description="With q: true when more pages matched than are listed.")]
    url: Annotated[Any, Field(description="With url: the page's URL as stored.")]
    pageStatus: Annotated[Any, Field(description=(
        "With url: ok, blocked, captcha, missing (404/410), timeout, redirect, robots or skipped."))]
    words: Annotated[Any, Field(description="With url: words in its markdown.")]
    markdown: Annotated[Any, Field(description=(
        "With url: its main content as markdown, capped at 12,000 characters."))]
    text: Annotated[Any, Field(description="With url: plain text, when kept.")]
    cleanHtml: Annotated[Any, Field(description="With url: simplified html, when kept.")]
    html: Annotated[Any, Field(description="With url: the raw html, when the run kept it.")]
    head: Annotated[Any, Field(description="With url: {title, description, canonical, h1, ...}.")]
    fields: Annotated[Any, Field(description="With url: fields the project's json_schema extracted, or null.")]
    versions: Annotated[Any, Field(description=(
        "With url: every stored copy across runs, newest first, each {runId, crawledAt, contentHash, words, "
        "status}; a changed contentHash is a changed page."))]
    crawledAt: Annotated[Any, Field(description="With url: when this copy was read (ISO 8601).")]
    error: Annotated[Any, Field(description=_ERR)]
    status: Annotated[Any, Field(description=(
        "With url: the HTTP status the site answered. On an error, the API's status code."))]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class ChangesOut(TypedDict, total=False):
    """A run's change record against the run before it, or against the run named in `against`."""
    change: Annotated[Any, Field(description=(
        "{runId, prevRunId, baseline, rebaselined, coverage, degraded, counts {added, modified, removed}, summary, "
        "feed, fields, withheld}. feed: each changed page as {url, path, kind (added, modified or removed), added "
        "and removed (words, e.g. '+12'), wordsBefore, wordsAfter}. fields: head and extracted field changes as "
        "{url, field, before, after}. withheld: removals held back because coverage was under 90%. Each list holds "
        "up to 200."))]
    runs: Annotated[Any, Field(description=(
        "The finished runs a record exists for, newest first, for picking another run_id."))]
    error: Annotated[Any, Field(description=_ERR)]
    status: Annotated[Any, Field(description="Only on a failure: the HTTP status code.")]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class JobOut(TypedDict, total=False):
    """A job's state, or its result once finished: a crawl gives crawl_site's answer, a batch scrape_urls',
    a search search_web's, an agent run run_agent's, a run the run record, and `url` one page in full."""
    status: Annotated[Any, Field(description=(
        "'running' while it goes on; otherwise the finished job's status; 'expired' for an agent run past its "
        "keep date. On an error, the HTTP status code."))]
    job: Annotated[Any, Field(description="While running: {kind, id, project_id} to call again with.")]
    counts: Annotated[Any, Field(description="Pages by outcome so far.")]
    crawl_id: Annotated[Any, Field(description="A crawl: its id.")]
    pages: Annotated[Any, Field(description="A finished crawl or batch: pages with excerpts.")]
    index: Annotated[Any, Field(description="A finished crawl or batch: every page as {url, title, words, status}.")]
    cursor: Annotated[Any, Field(description="A crawl with more pages: pass it back for the next window.")]
    markdown: Annotated[Any, Field(description="With url: that page's markdown, capped at 12,000 characters.")]
    id: Annotated[Any, Field(description="A run, a search or an agent run: its id.")]
    results: Annotated[Any, Field(description=(
        "A search: the ranked results, as search_web returns them, with page excerpts when it scraped."))]
    data: Annotated[Any, Field(description=(
        "A finished agent run: its answer, JSON matching the schema or {text}; {partial} at credit_limit."))]
    fieldSources: Annotated[Any, Field(description=(
        "A finished agent run: where each value of data came from, its path (e.g. 'plans[0].price') to {url, "
        "pageId}; left out first when the answer is too long."))]
    sources: Annotated[Any, Field(description=(
        "A finished agent run: the pages its answer rests on, each {url, title, pageId}."))]
    creditsUsed: Annotated[Any, Field(description="An agent run: credits spent so far, pages and model tokens.")]
    budget: Annotated[Any, Field(description="An agent run: the most it may spend.")]
    steps: Annotated[Any, Field(description="An agent run: the steps it has taken.")]
    stopReason: Annotated[Any, Field(description=(
        "A finished agent run: why it stopped early, e.g. credit_limit, step_limit or cancelled; empty when it "
        "finished on its own."))]
    note: Annotated[Any, Field(description="What to do next.")]
    error: Annotated[Any, Field(description=_ERR)]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class CancelOut(TypedDict, total=False):
    """What a cancel did."""
    kind: Annotated[Any, Field(description="crawl, run, batch or agent.")]
    id: Annotated[Any, Field(description="The job's id.")]
    project_id: Annotated[Any, Field(description="A run: its project.")]
    outcome: Annotated[Any, Field(description=(
        "'cancelled' (it had not started), 'cancelling' (it stops after the page in hand, or an agent run after "
        "the step under way), or the status of a job that had already ended."))]
    note: Annotated[Any, Field(description="What a stop keeps and drops.")]
    error: Annotated[Any, Field(description=_ERR)]
    status: Annotated[Any, Field(description="Only on a failure: the HTTP status code.")]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class SearchWebOut(TypedDict, total=False):
    """A web search: the ranked results, with an excerpt of each page when scraped, or a job when it is still
    going."""
    id: Annotated[Any, Field(description="The search's id: for get_job with kind 'search'.")]
    status: Annotated[Any, Field(description=(
        "'done'; 'blocked' when every engine refused the results page (nothing charged); 'running' with a `job` "
        "while it goes on. On an error, the HTTP status code."))]
    query: Annotated[Any, Field(description="The query as searched.")]
    engine: Annotated[Any, Field(description="The engine whose results page answered, e.g. 'google' or 'bing'.")]
    cached: Annotated[Any, Field(description=(
        "true when an equal search within the hour answered it, with no charge for the results page."))]
    creditsUsed: Annotated[Any, Field(description="Credits this search spent, scraped pages included.")]
    results: Annotated[Any, Field(description=(
        "Up to 10, as ranked, each {position, title, url, snippet, source, engine}; when scraped also `page`, "
        "{url, title, words, status, links (a count), markdown excerpt}, the excerpts sharing 60,000 characters."))]
    attempts: Annotated[Any, Field(description="Blocked only: each engine tried, and why it was refused.")]
    job: Annotated[Any, Field(description=(
        "While it is still going: {kind: 'search', id} for get_job. A scraping search can carry its results and "
        "a job for the pages still landing."))]
    note: Annotated[Any, Field(description="The cache rule, and what to do next when it is still going.")]
    error: Annotated[Any, Field(description=_ERR)]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


@with_config(ConfigDict(extra="allow"))
class AgentOut(TypedDict, total=False):
    """An agent run: its answer and the pages it rests on once finished, or a job while it works."""
    id: Annotated[Any, Field(description="The run's id: for get_job and cancel_job with kind 'agent'.")]
    status: Annotated[Any, Field(description=(
        "queued/running with a `job` while it works; done, credit_limit (stopped at its budget, data.partial "
        "holds what it found, continue_agent carries it on), cancelled or error once finished; 'expired' past "
        "its keep date. On an error, the HTTP status code."))]
    prompt: Annotated[Any, Field(description="The question as it was asked.")]
    data: Annotated[Any, Field(description=(
        "The answer: JSON matching the schema, or {text} without one; {partial} at credit_limit. When the answer "
        "is too long for 60,000 characters, a string of its JSON text, cut, and the note says where the whole "
        "answer is."))]
    fieldSources: Annotated[Any, Field(description=(
        "Where each value of data came from: its path (e.g. 'plans[0].price', or '[2].name' for a list answer) "
        "to {url, pageId}, a page the run read. The first thing left out when the answer is too long for 60,000 "
        "characters, and the note says so."))]
    sources: Annotated[Any, Field(description=(
        "The pages the answer rests on, each {url, title, pageId}; the first 50 when the answer had to be cut."))]
    creditsUsed: Annotated[Any, Field(description="Credits spent so far: pages read plus the model's tokens.")]
    budget: Annotated[Any, Field(description=(
        "The most the run may spend: max_credits, or less when the workspace had less left (budgetLimited)."))]
    budgetLimited: Annotated[Any, Field(description=(
        "true when the budget was lowered to what the workspace had left when the run started."))]
    steps: Annotated[Any, Field(description="The steps the agent has taken.")]
    stopReason: Annotated[Any, Field(description=(
        "Why it stopped early, e.g. credit_limit, step_limit or cancelled; empty when it finished on its own."))]
    job: Annotated[Any, Field(description=(
        "While it works: {kind: 'agent', id} for get_job and cancel_job."))]
    note: Annotated[Any, Field(description="What to do next, and what was cut and where to get it whole.")]
    error: Annotated[Any, Field(description=(
        "When the call failed, or the run ended in error: what went wrong, in words an assistant can act on."))]
    code: Annotated[Any, Field(description=_CODE)]
    detail: Annotated[Any, Field(description=_DETAIL)]


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


def _still_running(kind, job_id, counts=None, project_id=None, extra=None):
    """What a long tool answers when its budget ran out.

    The job is not cancelled -- it goes on server-side, and `get_job` picks it
    up. Saying so matters: an assistant told only "running" would start the
    work again. `extra` is merged into the answer, for a kind with more to say
    while it runs (an agent run's spend so far).
    """
    job = {"kind": kind, "id": job_id}
    if project_id:
        job["project_id"] = project_id
    out = {"status": "running", "job": job, "counts": counts or {},
           "note": "call get_job with this job to check; it keeps running server-side"}
    if extra:
        out.update(extra)
    return out


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
        notes.append("this crawl is kept for a day unless create_project is called with its crawl_id")
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
    pages, index, _cap, kept = _excerpts(rows, "call scrape_urls with that url", reserve)
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
            "status and names those that did not come back ok. scrape_urls reads one again, "
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


# Eleven of the seventeen tools need write, including every one that fetches a
# page: fetching spends the workspace's credits, so it is not a read however
# it reads to an assistant asking for one URL. The API answers "this needs the
# member role", which is true and tells an assistant nothing it can act on --
# it does not know what a role is, that it has one, or that the person who
# approved the connection chose it.
READ_ONLY = (
    "this connection was approved read-only. It can read what the workspace has "
    "already stored -- projects, pages, change records, search inside a project, "
    "get_job -- but it cannot fetch a new page or change anything: those reach the "
    "site or alter the workspace, and most of them spend credits. "
    "Ask the person to reconnect the app and tick 'Also allow changes' (write access)."
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
        if exc.code:
            out["code"] = exc.code
        if exc.request_id:
            out["request_id"] = exc.request_id
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
            if exc.request_id:
                out["request_id"] = exc.request_id
        return out
    except Exception as exc:                          # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@tool(title="Scrape pages by URL",
      annotations=_acts(),
      description="Fetch pages whose URLs you already have and return their content. No project is created.\n"
                  "Use crawl_site when the URLs must be found by following links, and map_site to list a site's "
                  "URLs without fetching. list_pages with url reads a page a project already stored, for free.\n"
                  "Inputs: urls come from the request or from map_site. formats adds bodies, e.g. 'markdown,text'. "
                  "config overrides fetch settings, e.g. {\"render_js\": \"always\"}; get_project with no project_id "
                  "lists the keys. full=true reads one URL in full and runs config.actions first, e.g. [{\"type\": "
                  "\"click\", \"selector\": \"#load-more\", \"repeat\": \"until_gone\"}].\n"
                  "Cost: 1 credit for a plain fetch, 4 with a browser, +1 for a screenshot. A page the site refuses "
                  "is reported as blocked and is free.\n"
                  "Lists: a list runs as one batch. If it outlasts the connection's time limit, a job comes back "
                  "for get_job, and sending the same list again returns that batch.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def scrape_urls(urls: Annotated[list[str], Field(
                    description="Absolute http(s) URLs, 1 to 500; exactly one when full is true.")],
                config: FetchConfig = None,
                formats: Annotated[str, Field(
                    description="Bodies per page, comma-separated from markdown (default), text, cleanHtml and "
                                "rawHtml. With full=true, config's formats list chooses them instead.")]
                = "markdown",
                parse_documents: ParseDocuments = True,
                full: Annotated[bool, Field(
                    description="true reads the one URL in full -- raw html, head and extracted fields, images, "
                                "the engine used -- running config's browser actions first. Default false.")]
                = False) -> ScrapeOut:
    def go():
        cfg = _with_documents(config, parse_documents)
        if full:
            if len(urls) != 1:
                return {"error": "full reads one page: pass exactly one url", "code": "validation"}
            with _client() as s:
                r = s.extract(urls[0], config=cfg)
                if r.get("page"):
                    r["page"] = _trim_page(r["page"])
                    r["page"].pop("links", None)
                return r
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


@tool(title="Map a site's declared URLs",
      annotations=_acts(),
      description="List the URLs a site declares in its sitemaps (found through robots.txt and the well-known "
                  "paths, each index file walked to its children) without fetching any page.\n"
                  "Use it first, to see how big a site is and what sections it has, before crawl_site or "
                  "create_project. It cannot read content (scrape_urls and crawl_site do), and it misses pages a "
                  "site links to but does not declare.\n"
                  "Inputs: any page of the site works as url, since discovery starts at the root. search='/blog/' "
                  "keeps the blog's URLs; it is a plain case-insensitive substring, not a glob, applied before "
                  "limit.\n"
                  "Cost: 1 credit per sitemap file read (usually 1 in total), never per URL.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def map_site(url: Annotated[str, Field(description="Any URL on the site, usually its home page.")],
             search: Annotated[str | None, Field(
                 description="Keep only URLs containing this text; omit for all.")] = None,
             limit: Annotated[int, Field(
                 description="The most URLs to return, 1 to 5,000 (default 1,000); totals count them all.")]
             = 1000) -> MapOut:
    def go():
        with _client() as s:
            out = s.map_details(url, search=search, limit=min(limit, 5000))
            return {"url": out["url"], "method": out["method"], "totals": out["totals"],
                    "urls": [u["url"] for u in out["data"]],
                    "sections": sorted({u.get("section", "") for u in out["data"] if u.get("section")})}
    return _safe(go)


@tool(title="Crawl a whole site once",
      annotations=_acts(idempotent=True),
      description="Crawl a site once from a start URL, following links and the sitemap, and return the pages read. "
                  "No project is set up.\n"
                  "Use it for a site or section whose page URLs you do not know; for known URLs use scrape_urls. To "
                  "watch the site afterwards, pass the crawl_id to create_project.\n"
                  "Inputs: include_paths=['/blog/*'] reads the blog index and everything under it; paths match the "
                  "URL path, never the host. Excluded pages do not count toward limit. limit=200 with max_depth=3 "
                  "suits one section. config takes the keys get_project lists when called with no project_id.\n"
                  "Cost: 1 to 4 credits per page, by the engine that read it; refused pages are free.\n"
                  "Timing: it waits for the crawl, minutes for a large limit. If it outlasts the connection's time "
                  "limit, a job comes back for get_job, and calling again returns the same crawl. The crawl is kept "
                  "for a day.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def crawl_site(url: Annotated[str, Field(description="The absolute http(s) URL to start from, e.g. the home page.")],
               limit: Annotated[int, Field(
                   description="The most pages to read, 1 to 5,000 (default 50); the plan's page cap also applies.")]
               = 50,
               max_depth: Annotated[int, Field(
                   description="Link hops to follow from the start URL (0 = that page only; default 3).")]
               = 3,
               include_paths: Annotated[list[str] | None, Field(
                   description="Globs over the URL path to keep; a bare '/blog/' matches that one page only. "
                               "Omit for the whole site.")]
               = None,
               exclude_paths: Annotated[list[str] | None, Field(
                   description="Globs over the URL path to skip, e.g. ['/tag/*', '*.pdf']; an exclude wins over "
                               "an include.")] = None,
               config: FetchConfig = None) -> CrawlOut:
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


SEARCH_NOTE = ("an equal search (same query, country, lang, freshness and domains) within an hour of a "
               "finished one is answered from cache with no charge for the results page; scraped pages are "
               "charged. scrape_urls with full=true reads one result in full")


def _search_result(out, running=False):
    """A web search, shaped once for an assistant.

    The hits as the API ranked them, and a scraped page as an excerpt rather
    than the page: ten results at a full page each is the answer a crawl used
    to give, and the cap is chosen the same way -- what the rest of the answer
    leaves, measured, shared between the pages. Built, weighed, and rebuilt
    with the overshoot reserved, as a batch is.
    """
    data = out.get("data")
    hits = [h for h in data if isinstance(h, dict)] if isinstance(data, list) else []
    reserve, shaped = 0, None
    for _attempt in range(4):
        shaped = _shape_search(out, hits, running, reserve)
        over = _cost(shaped) - RESULT_BUDGET
        if over <= 0:
            break
        reserve += over + 64
    return shaped


def _shape_search(out, hits, running, reserve):
    """One pass at a search's answer, at the budget `reserve` leaves."""
    how = "call scrape_urls with that url"
    results = [{k: h.get(k) for k in ("position", "title", "url", "snippet", "source", "engine")}
               for h in hits]
    paged = [i for i, h in enumerate(hits) if isinstance(h.get("page"), dict)]
    shaped = {"id": out.get("id", ""), "status": out.get("status"), "query": out.get("query", ""),
              "engine": out.get("engine", ""), "cached": bool(out.get("cached")),
              "creditsUsed": out.get("creditsUsed"), "results": results}
    if out.get("error"):
        shaped["error"] = out["error"]
    if out.get("status") == "blocked":
        shaped["attempts"] = out.get("attempts") or []
    notes = []
    if running:
        shaped["job"] = {"kind": "search", "id": out.get("id", "")}
        notes.append("the results are in and their pages are still being fetched: call get_job with "
                     "this job for the pages")
    notes.append(SEARCH_NOTE)
    shaped["note"] = ". ".join(notes)
    if paged:
        # Every page at a cap of zero is the frame; what is left is the excerpts'.
        for i in paged:
            results[i]["page"] = _summary_page(hits[i]["page"], 0, how)
        left = RESULT_BUDGET - reserve - _cost(shaped)
        cap = max(EXCERPT_FLOOR, min(MARKDOWN_CAP, left // len(paged)))
        for i in paged:
            results[i]["page"] = _summary_page(hits[i]["page"], cap, how)
    return shaped


@tool(title="Search the web",
      annotations=_acts(),
      description="Search the web for a query and return the ranked results, each with its title, url and "
                  "snippet. The results are read from Google's results page, or Bing's when Google refuses.\n"
                  "Use it to find pages whose URLs you do not know. It searches the web, unlike list_pages with q, "
                  "which searches inside a project's stored run. For URLs you already have use scrape_urls; "
                  "scrape_urls with full=true reads one result in full.\n"
                  "Inputs: query as typed into a search engine, e.g. 'python http client'. scrape=true also fetches "
                  "each result's page as markdown in the same call. include_domains=['python.org'] keeps the search "
                  "to those sites; exclude_domains drops sites, and an exclude wins.\n"
                  "Cost: Google's results page is read with a browser, 4 credits or more where it needs the stealth "
                  "tier; Bing's, used when Google refuses, is a plain fetch at 1. A results page the engines refused "
                  "is free. An equal search (same query, country, lang, freshness and domains) within an hour of a "
                  "finished one comes from cache with no charge for the results page. Scraped pages are charged, "
                  "cached or not.\n"
                  "Timing: it waits for the results. If a search outlasts the connection's time limit, a job comes "
                  "back for get_job; a scraping search whose pages are still landing returns its results with a "
                  "job for the pages.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def search_web(query: Annotated[str, Field(
                   description="What to search the web for, 1 to 400 characters, as it would be typed into a "
                               "search engine.")],
               limit: Annotated[int, Field(
                   description="How many results to return, 1 to 10 (default 10).")] = 10,
               country: Annotated[str | None, Field(
                   description="A 2-letter country code to search from, e.g. 'us' or 'de'; omit for the "
                               "default.")] = None,
               lang: Annotated[str | None, Field(
                   description="A language code for the results, e.g. 'en'; omit for the default.")] = None,
               freshness: Annotated[Literal["hour", "day", "week", "month", "year"] | None, Field(
                   description="Only results from the last hour, day, week, month or year; omit for any "
                               "time.")] = None,
               include_domains: Annotated[list[str] | None, Field(
                   description="Up to 20 sites to search within, e.g. ['python.org']; omit for the whole "
                               "web.")] = None,
               exclude_domains: Annotated[list[str] | None, Field(
                   description="Up to 20 sites to leave out of the results, e.g. ['pinterest.com']; an "
                               "exclude wins over an include.")] = None,
               scrape: Annotated[bool, Field(
                   description="true also fetches each result's page as markdown; each page costs credits. "
                               "false (default) returns the results only.")] = False) -> SearchWebOut:
    def go():
        opts = {"limit": max(1, min(limit, 10)), "country": country, "lang": lang, "freshness": freshness,
                "include_domains": include_domains or None, "exclude_domains": exclude_domains or None,
                "scrape": scrape or None}
        # No idempotency key: an equal search within the hour is served from
        # the API's cache anyway, and a repeat is a new search by design.
        with _client() as s:
            budget = _budget()
            if budget is None:
                try:
                    return _search_result(s.web_search(query, **opts))
                except MeshArcTimeoutError as exc:
                    # Past the client's own wait: still going server-side, and
                    # get_job picks it up, as crawl_site's long crawls are.
                    return _still_running("search", getattr(exc, "job_id", "") or "")
            # Hosted: the API holds the request for most of the budget and
            # answers with whatever it has, never a poll loop down this socket.
            out = s.web_search(query, wait=False, timeout_s=max(1.0, min(120.0, budget - 5)), **opts)
            if out.get("status") == "error":
                raise MeshArcError(502, out.get("error") or "the search failed", "job_failed")
            if _running(out.get("status")):
                if not out.get("data"):
                    return _still_running("search", out.get("id", ""))
                return _search_result(out, running=True)
            return _search_result(out)
    return _safe(go)


# An agent run's answer past RESULT_BUDGET: the sources it keeps first.
AGENT_SOURCES_CAP = 50
AGENT_POLL = ("an agent works for minutes: call get_job with this job every 30 to 60 seconds; it keeps running "
              "server-side, and cancel_job stops it")
AGENT_ACCEPTING = ("an identical run_agent call is still being accepted; call run_agent again in a few seconds "
                   "to get its job")
AGENT_CONTINUE_ACCEPTING = ("an identical continue_agent call is still being accepted; call continue_agent again in "
                            "a few seconds to get the new run's job")
AGENT_EXPIRED = "this agent run is past its keep date (7 days); its answer is no longer kept"
AGENT_CANCEL_NOTE = ("a queued run stops at once; a running one stops before its next step, the step under way "
                     "finishing first. Pages and model tokens already used stay charged.")


def _key_in_flight(exc):
    """A 409 saying the first request with this idempotency key is still being
    accepted: code in_flight, or only the message on an API from before that
    code. Every other 409 is a refusal about the run."""
    return exc.status == 409 and (exc.code == "in_flight" or "Idempotency-Key" in str(exc.detail or ""))


def _agent_answer(env):
    """An agent run as run_agent and get_job both answer it: the job while it
    works, with what it has spent so far, and the result once finished."""
    if _running(env.get("status")):
        return _still_running("agent", env.get("id", ""), extra={
            "budget": env.get("budget"), "creditsUsed": env.get("creditsUsed"), "steps": env.get("steps"),
            "note": AGENT_POLL})
    return _agent_result(env)


def _agent_result(env):
    """A finished agent run, shaped once, inside RESULT_BUDGET.

    The answer is what was asked for, so it is cut last. `fieldSources` goes
    first -- it says where each value came from, which the sources mostly say
    too -- then the sources go down to the first fifty, and only then is
    `data` replaced by its JSON text, cut to what the rest leaves. Either way
    the note says where the whole answer is.
    """
    rid = env.get("id", "")
    status = env.get("status")
    sources = env.get("sources") if isinstance(env.get("sources"), list) else []
    field_sources = env.get("fieldSources") if isinstance(env.get("fieldSources"), dict) else {}
    data = env.get("data")
    out = {"id": rid, "status": status, "prompt": env.get("prompt", ""), "data": data,
           "fieldSources": field_sources, "sources": sources,
           "creditsUsed": env.get("creditsUsed"), "budget": env.get("budget"),
           "budgetLimited": env.get("budgetLimited"), "steps": env.get("steps"),
           "stopReason": env.get("stopReason", "")}
    if env.get("error"):
        out["error"] = env["error"]
    notes = []
    if status == "credit_limit" and env.get("continuedBy"):
        # Already carried on: a second continue is refused, and the answer
        # that replaces this partial is the other run's.
        notes.append(f"the run stopped at its credit budget and was carried on by run {env['continuedBy']}: "
                     "get_job with that id for its answer")
    elif status == "credit_limit":
        notes.append("the run stopped at its credit budget before it finished: data.partial holds what it had "
                     f"found by then, and continue_agent with id='{rid}' and a new max_credits carries it on "
                     "from where it stopped")
    elif status == "error":
        notes.append("the run failed and error says why; pages and model tokens it used stay charged")
    out["note"] = ". ".join(notes)
    if _cost(out) <= RESULT_BUDGET:
        return out

    whole = f"the full answer is at GET /api/v1/agent/{rid} and via the SDK's get_agent('{rid}')"
    # Only a map that was there is said to be left out.
    dropped = bool(field_sources)

    def shape(kept, value, data_cut):
        cuts = []
        if dropped:
            cuts.append("fieldSources is left out")
        if len(kept) < len(sources):
            cuts.append(f"sources lists the first {len(kept)} of {len(sources)}")
        if data_cut:
            cuts.append("data is the answer's JSON text, cut")
        said = ", ".join(cuts[:-1]) + " and " + cuts[-1] if len(cuts) > 1 else "".join(cuts)
        shaped = {**out, "sources": kept, "data": value,
                  "note": ". ".join(notes + [said + f" to stay inside {RESULT_BUDGET:,} characters; " + whole])}
        shaped.pop("fieldSources")
        return shaped

    if dropped:
        shaped = shape(sources, data, False)
        if _cost(shaped) <= RESULT_BUDGET:
            return shaped
    kept = sources
    if len(sources) > AGENT_SOURCES_CAP:
        kept = sources[:AGENT_SOURCES_CAP]
    # However long their titles, the sources leave half the budget to the answer.
    while kept and _cost(shape(kept, "", True)) > RESULT_BUDGET // 2:
        kept = kept[:len(kept) // 2]
    shaped = shape(kept, data, False)
    if _cost(shaped) <= RESULT_BUDGET:
        return shaped
    # The longest cut that fits, found by halving: a character weighs one to
    # six in the answer's JSON (quotes, backslashes, anything not ASCII), so
    # one measured step can overshoot by most of the answer.
    text = json.dumps(data, ensure_ascii=False)

    def cut_at(n):
        return shape(kept, text[:n] + f"… [{len(text) - n} more characters]", True)

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _cost(cut_at(mid)) <= RESULT_BUDGET:
            lo = mid
        else:
            hi = mid - 1
    return cut_at(lo)


@tool(title="Run a research agent",
      annotations=_acts(),
      description="Hand a research question to an agent that searches the web, maps sites and reads pages on its "
                  "own, then answers with the sources it used.\n"
                  "Use it for questions that need several pages or sites to answer. When you know what to fetch, "
                  "search_web and scrape_urls are quicker and cheaper.\n"
                  "Inputs: prompt says what to find, e.g. 'the monthly price of each plan on example.com'. urls "
                  "gives pages to start from, e.g. ['https://example.com/pricing']. schema shapes the answer, e.g. "
                  "{\"type\": \"object\", \"properties\": {\"plans\": {\"type\": \"array\"}}}; without one the "
                  "answer is {\"text\": ...}. allowed_domains=['example.com'] keeps it to those sites.\n"
                  "Cost: pages are charged as they are read, and a page the site refuses is free; the model's "
                  "tokens at the model provider's price plus 20%. max_credits caps the whole run, 2,000 by "
                  "default; a run that reaches it stops with what it found, and continue_agent carries it on.\n"
                  "Timing: it returns at once with a job, as an agent works for minutes. Call "
                  "get_job(kind='agent', id=...) every 30 to 60 seconds; cancel_job stops it. Calling run_agent "
                  "again with the same inputs in the same ten-minute window returns that run, not a new one.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def run_agent(prompt: Annotated[str, Field(
                  description="What the agent should find out, 1 to 10,000 characters, as a question or a "
                              "task.")],
              urls: Annotated[list[str] | None, Field(
                  description="Up to 20 absolute http(s) URLs for the agent to start from; omit to let it "
                              "search.")] = None,
              schema: Annotated[dict | None, Field(
                  description="A JSON Schema object whose type is object or array; the answer then matches "
                              "it. Omit for a text answer, {\"text\": ...}.")] = None,
              allowed_domains: Annotated[list[str] | None, Field(
                  description="Up to 20 sites the agent stays within, e.g. ['example.com']; omit for the "
                              "whole web.")] = None,
              max_credits: Annotated[int | None, Field(
                  description="The most the run may spend, 1 to 100,000 credits; omit for 2,000.")] = None,
              max_steps: Annotated[int | None, Field(
                  description="The most steps the agent may take, 1 to 100; omit for 40.")] = None) -> AgentOut:
    def go():
        # The window is part of the key: a retry in the same ten minutes lands
        # on the same run, and a deliberate re-run later is a new one.
        key = _key("agent", prompt, urls, schema, allowed_domains, max_credits, max_steps,
                   int(time.time() // 600))
        with _client() as s:
            try:
                run = s.agent(prompt, urls=urls, schema=schema, max_credits=max_credits, max_steps=max_steps,
                              allowed_domains=allowed_domains, idempotency_key=key)
            except MeshArcError as exc:
                # The first request with this key is still being accepted, and
                # this answer carries no run id: asking again shortly gets it.
                # The API says so with code in_flight (an API from before that
                # code said it only in the message); any other 409 is a
                # refusal, and goes back as an error.
                if _key_in_flight(exc):
                    return {"status": "running", "note": AGENT_ACCEPTING}
                raise
            # A replayed key answers with the first stored envelope, which may
            # be long out of date: read the run as it stands.
            run.refresh()
            return _agent_answer(run.envelope)
    return _safe(go)


@tool(title="Continue a stopped agent run",
      annotations=_acts(),
      description="Carry on an agent run that stopped at its credit limit (status credit_limit) with a new budget. "
                  "It starts a new run on the same thread that resumes where the stopped one was, keeps the pages "
                  "it read and answers in full.\n"
                  "Use it when run_agent or get_job answered status 'credit_limit' and data.partial is not enough. "
                  "A run that finished, failed or was cancelled cannot be carried on: ask run_agent again.\n"
                  "Inputs: id is the stopped run's id, from its answer. max_credits and max_steps are the new run's "
                  "own, e.g. max_credits=4000; omitted, they are the stopped run's.\n"
                  "Cost: the pages the stopped run read are not paid for again. New pages are charged as they are "
                  "read, a refused page free, and the model's tokens at the model provider's price plus 20%, "
                  "within max_credits.\n"
                  "Timing: it returns at once with the new run's job, as run_agent does. Call "
                  "get_job(kind='agent', id=...) with the new id every 30 to 60 seconds; cancel_job stops it. "
                  "Calling continue_agent again for a run already carried on returns the run that carried it on, "
                  "not a new one.\n"
                  "Refusals: code conflict for a run that did not stop at its credit limit (or was carried on "
                  "meanwhile, by another call), not_found for an unknown id, expired past its 7-day keep date.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def continue_agent(id: Annotated[str, Field(
                       description="The id of the run that stopped at its credit limit, from run_agent's or "
                                   "get_job's answer.")],
                   max_credits: Annotated[int | None, Field(
                       description="The most the new run may spend, 1 to 100,000 credits; omit for the stopped "
                                   "run's.")] = None,
                   max_steps: Annotated[int | None, Field(
                       description="The most steps the new run may take, 1 to 100; omit for the stopped "
                                   "run's.")] = None) -> AgentOut:
    def go():
        # Keyed as run_agent is: a retry in the same ten minutes lands on the
        # same new run. Later, the stopped run names the run that carried it
        # on, and that run is the answer.
        key = _key("continue_agent", id, max_credits, max_steps, int(time.time() // 600))
        with _client() as s:
            stopped = s.get_agent(id)
            if stopped.continued_by:
                # Already carried on -- by this tool's own earlier call, its
                # key since past the ten-minute window, or elsewhere. A second
                # continue would be refused; the run that carried it on is
                # the answer.
                carried = s.get_agent(stopped.continued_by)
                out = _agent_answer(carried.envelope)
                said = f"run {id} was already carried on by run {stopped.continued_by}; this is that run"
                out["note"] = ". ".join(n for n in (said, out.get("note")) if n)
                return out
            try:
                run = stopped.continue_(max_credits=max_credits, max_steps=max_steps, idempotency_key=key)
            except MeshArcError as exc:
                # Only the key still being accepted is answered as running.
                # The API's other 409s -- a run that did not stop at its
                # limit, or was already carried on -- are refusals about the
                # run, and go back as errors.
                if _key_in_flight(exc):
                    return {"status": "running", "note": AGENT_CONTINUE_ACCEPTING}
                raise
            # A replayed key answers with the first stored envelope: read the
            # new run as it stands.
            run.refresh()
            return _agent_answer(run.envelope)
    return _safe(go)


@tool(title="List all projects",
      annotations=READS_STORED,
      description="List every project in the workspace, the sites it watches over time, each with its id, name, "
                  "host, schedule, page count, coverage, last run and status; and the workspace's plan and the "
                  "credits it has left.\n"
                  "Call it first to find the project_id the other project tools take, to check a site is not "
                  "already watched before create_project, or to see whether there are credits for a large crawl. "
                  "get_project reads one project in full.\n"
                  "Results: every project in one answer, oldest first, with no paging. A key limited to some "
                  "projects sees only those.\n"
                  "Read-only and free: it reads stored data and never fetches.")
def list_projects() -> ProjectsOut:
    def go():
        s = _client()
        out: dict = {"projects": [
            {k: p.get(k) for k in ("id", "name", "seed", "host", "schedule", "pages", "coverage", "lastRun",
                                   "status")}
            for p in s.projects.list()]}
        # The balance rides along so an assistant can check it before a
        # large crawl rather than learn it from a 402. It is an extra: no
        # failure reading it costs the caller the projects.
        try:
            b = s.billing()
            month = b.get("month") or {}
            out["workspace"] = {"plan": (b.get("plan") or {}).get("name"),
                                "creditsLeft": month.get("remaining"),
                                "creditsSpentThisMonth": (month.get("counters") or {}).get("credits"),
                                "oneTimeAllowance": bool(month.get("once"))}
        except Exception as exc:                          # noqa: BLE001
            detail = exc.detail if isinstance(exc, MeshArcError) else type(exc).__name__
            out["workspace"] = {"note": f"the balance could not be read: {detail}"}
        return out
    return _safe(go)


# The settings an assistant is likely to set, with what each means. The
# full list, with defaults, comes back from get_project with no project id;
# this is the vocabulary that turns "only the blog, weekly, rendered" into a
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
    "formats": "list of bodies to keep: 'markdown', 'text', 'cleanHtml', 'rawHtml', 'raw', 'links', 'screenshot', "
               "'json'; markdown is always kept (default ['markdown', 'links'])",
    "only_main_content": "true drops navigation, headers, footers and sidebars from the markdown",
    "include_tags": "CSS selectors to keep, e.g. ['article', '.post']",
    "exclude_tags": "CSS selectors to drop, e.g. ['.comments', '#newsletter']",
    "min_words": "pages shorter than this are recorded but not compared (0 = keep all)",
    "languages": "list of language codes to keep, e.g. ['en']; empty = all",
    "parse_documents": "true reads PDFs, Word and spreadsheet files the crawl meets",
    "respect_robots": "obey robots.txt (default true)",
    "allow_subdomains": "follow links to subdomains of the seed's domain",
    "use_proxy": "route through the residential exits (costs more; for walled sites)",
    "json_schema": "a LIST of fields to fill on every page, each {name, type, required}: type one of 'string' "
                   "(default), 'number', 'date', 'boolean', 'url', 'array'; required defaults to false; a name "
                   "starts with a letter, then letters, digits, _ . - (up to 64, no repeats); at most 40 fields "
                   "(more are dropped). Filled from the page's markup, e.g. "
                   "[{\"name\": \"price\", \"type\": \"number\", \"required\": true}]",
    "llm_extract": "{connection_id, instructions?, only_missing?, max_pages?, max_chars?, max_output_tokens?} or "
                   "null = off (true is refused): a model (an llm connection the workspace added under "
                   "Connectors) fills the json_schema fields the markup could not; does nothing unless json_schema "
                   "has fields. instructions cut at 2000 chars; only_missing default true (false asks for every "
                   "field); max_pages 1-2000, default 500; max_chars 0 = as much as the model holds (default) "
                   "else 2000-400000; "
                   "max_output_tokens 0 = sized to the schema (default) else 200-16000",
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


# A webhook delivery as get_project shows it: whether it arrived and why not,
# without the payload, which repeats the run record.
DELIVERY_FIELDS = ("id", "event", "status", "attempts", "lastStatus", "lastError", "createdAt", "deliveredAt")


@tool(title="Get a project, or the settings",
      annotations=READS_STORED,
      description="Read one project in full, or with no project_id the settings any project can take.\n"
                  "Read a project before update_project to see its current values. Read the settings before "
                  "create_project, update_project or any config argument: keys are exact, and a guessed name is "
                  "refused. list_projects is the lighter way to find a project.\n"
                  "Inputs: project_id is the hex id from list_projects, not a name or URL. An unknown id answers "
                  "404.\n"
                  "Webhooks: a project's answer includes its webhook URL, the events sent there, and the last 10 "
                  "deliveries with their status and error, to see whether the endpoint is receiving them. "
                  "update_project sets them and sends a test.\n"
                  "Secrets: the webhook signing secret is never returned, and chat_url comes back masked.\n"
                  "Read-only and free: it reads stored data and never fetches.")
def get_project(project_id: Annotated[str | None, Field(
                    description="The project's 32-character hex id, from list_projects; omit for the settings "
                                "reference.")] = None) -> ProjectOut:
    def go():
        if project_id:
            with _client() as s:
                project = s.projects.get(project_id)
                cfg = project.get("config") or {}
                hooks: dict = {"url": cfg.get("webhook_url") or "", "events": cfg.get("webhook_events") or []}
                # The deliveries route, not GET .../webhooks: for an admin key
                # that one creates a signing secret when there is none, and a
                # read-only tool must not write. A failure here does not cost
                # the caller the project.
                try:
                    hooks["deliveries"] = [{k: d.get(k) for k in DELIVERY_FIELDS}
                                           for d in s.webhook_deliveries(project_id, limit=10)]
                except MeshArcError as exc:
                    hooks["deliveries"] = None
                    hooks["note"] = f"the deliveries could not be read: {exc.detail}"
                project["webhooks"] = hooks
                return project
        with _client() as s:
            defaults = (s.meta().get("configDefaults") or {})
            return {"settings": [{"key": k, "meaning": v, "default": defaults.get(k)} for k, v in CONFIG_GUIDE.items()],
                    "other_keys": sorted(k for k in defaults if k not in CONFIG_GUIDE),
                    "schedules": ["manual", "hourly", "daily", "weekly"],
                    "note": "include_paths and exclude_paths are globs over the URL path: '/blog/*' is the blog section "
                            "(its index included), '/blog/' is one page. To limit a project to a section, set include_paths "
                            "and nothing else; sitemap_include is for choosing among sitemap files, not pages."}
    return _safe(go)


@tool(title="Create a watched project",
      annotations=_acts(),
      description="Create a project: a site MeshArc re-crawls on a schedule, comparing each run with the last to "
                  "record pages added, modified and removed.\n"
                  "Use it when asked to monitor or track a site; for a one-off read use crawl_site. Check "
                  "list_projects first so the site is not added twice.\n"
                  "Inputs: seed starts a new project, e.g. 'example.com'. crawl_id instead keeps a crawl_site crawl "
                  "from the last day as the first run, with no second fetch; it keeps that crawl's settings, so "
                  "config goes only with seed. The usual config keys are include_paths (one section, e.g. "
                  "['/blog/*']), exclude_paths, max_pages and render_js; get_project with no project_id lists them "
                  "all.\n"
                  "Cost: creating reads robots.txt and the sitemaps but no pages, so it is free. Each run spends "
                  "credits per page, and any schedule but 'manual' runs on its own.\n"
                  "Refusals: an unknown config key, or a plan already at its project limit (code plan_limit). "
                  "Values above the plan's caps are lowered to them.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def create_project(seed: Annotated[str | None, Field(
                       description="The site to watch, as a URL or bare domain, e.g. 'https://example.com/blog/' "
                                   "or 'example.com'; its host is fixed for good. Give this or crawl_id.")] = None,
                   crawl_id: Annotated[str | None, Field(
                       description="A crawl_site answer's crawl_id, to keep that crawl as the project. Give this "
                                   "or seed.")] = None,
                   name: Annotated[str | None, Field(
                       description="A display name; omit for the site's host (or the crawl's own name).")] = None,
                   schedule: Annotated[Schedule, Field(
                       description="How often it re-crawls on its own; 'manual' (default) runs only when "
                                   "start_run is called.")] = "manual",
                   config: Annotated[dict | None, Field(
                       description="Settings as {key: value}, with a seed only; omit for the defaults.")]
                   = None) -> ProjectOut:
    def go():
        if bool(seed) == bool(crawl_id):
            return {"error": "give either seed (a new project) or crawl_id (keep a crawl), not both or neither",
                    "code": "validation"}
        with _client() as s:
            if crawl_id:
                if config:
                    return {"error": "a kept crawl keeps the settings it was crawled with; create the project "
                                     "without config, then change them with update_project", "code": "validation"}
                return s.get_crawl(crawl_id).keep(name=name, schedule=schedule)
            return s.projects.create(seed, name=name, schedule=schedule, config=config)
    return _safe(go)


@tool(title="Update a project's settings",
      annotations=_acts(destructive=True),
      description="Change a project's name, schedule or settings, its webhook among them, and send the webhook a "
                  "test.\n"
                  "Call get_project first to see the current values. To crawl with the new settings now, follow "
                  "with start_run. A project's site cannot change: to watch another site use create_project.\n"
                  "Inputs: project_id from list_projects. Only what you pass changes, and in config only the keys "
                  "given, so config={\"max_pages\": 200} leaves every other setting alone. schedule=\'manual\' stops "
                  "scheduled runs. Valid keys come from get_project with no project_id. A webhook is "
                  "config={\"webhook_url\": \"https://example.com/hook\", \"webhook_events\": [\"run.finished\", "
                  "\"page.changed\"]}; webhook_url=\"\" turns it off. test_webhook=true then queues a test "
                  "run.finished message, and get_project shows whether it was delivered.\n"
                  "Effect: it answers with the updated project. Nothing is fetched and no credits are spent. A "
                  "replaced value is gone, not versioned. "
                  "Changing how markdown is produced (e.g. only_main_content, include_tags, exclude_tags) makes the "
                  "next run a new baseline, recorded rather than compared.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def update_project(project_id: ProjectId,
                   name: Annotated[str | None, Field(description="A new name; omit to leave it.")] = None,
                   schedule: Annotated[Schedule | None, Field(
                       description="A new schedule; omit to leave it.")] = None,
                   config: Annotated[dict | None, Field(
                       description="Settings to change as {key: value}; omit to leave them all.")] = None,
                   test_webhook: Annotated[bool, Field(
                       description="true sends a test run.finished message to the project's webhook URL, after "
                                   "any changes; the URL must be saved. Default false.")] = False
                   ) -> ProjectOut:
    def go():
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if schedule is not None:
            fields["schedule"] = schedule
        if config:
            fields["config"] = config
        if not fields and not test_webhook:
            return {"error": "nothing to change: give a name, a schedule, config or test_webhook"}
        with _client() as s:
            out = s.projects.update(project_id, **fields) if fields else s.projects.get(project_id)
            if test_webhook:
                # After the update, so a URL saved in this same call is the one
                # tested. A refused test does not undo the change just made.
                try:
                    out["webhookTest"] = s.test_webhook(project_id)
                except MeshArcError as exc:
                    if exc.status == 403:
                        # The connection cannot write. Nothing was changed
                        # either (an update needs the same access), so the
                        # standard read_only answer is the honest one.
                        raise
                    out["webhookTest"] = {"error": exc.detail, "status": exc.status}
            return out
    return _safe(go)


# Said when the key behind a call is not an admin's. A connected app never is:
# OAuth grants read and write, never admin, so this is the answer every hosted
# caller gets -- and it has to say where the delete can be done instead.
ADMIN_ONLY = ("deleting a project needs an admin API key. A connected app never holds one, so delete it in the "
              "MeshArc app, or run this server locally with an admin key.")


@tool(title="Delete a project",
      annotations=_acts(destructive=True, idempotent=True, open_world=False),
      description="Delete a project for good, with everything its runs stored.\n"
                  "Use it only when the person asks for the project to go, e.g. to free a slot after create_project "
                  "is refused with plan_limit. To stop a run use cancel_job; to stop scheduled runs set schedule to "
                  "'manual' with update_project.\n"
                  "Inputs: project_id from list_projects. confirm_name must repeat the project's name exactly as "
                  "get_project shows it, or nothing is deleted.\n"
                  "Effect: its runs, pages, change records and webhook history are removed for good, and a run in "
                  "progress stops. Credits already spent stay spent. A second call answers 404.\n"
                  "Needs an admin API key. A connected app (OAuth) never has one: it is refused with code "
                  "admin_only and told where to delete instead.")
def delete_project(project_id: ProjectId,
                   confirm_name: Annotated[str, Field(
                       description="The project's name, exactly as get_project shows it.")]) -> DeleteOut:
    def go():
        s = _client()
        project = s.projects.get(project_id)
        if (project.get("name") or "") != confirm_name:
            return {"error": "confirm_name does not match this project's name, so nothing was deleted; read the "
                             "name with get_project and check with the person first", "code": "validation"}
        try:
            s.projects.delete(project_id)
        except MeshArcError as exc:
            if exc.status == 403 and (exc.code or "") in ("", "forbidden"):
                return {"error": ADMIN_ONLY, "status": 403, "code": "admin_only", "detail": exc.detail}
            raise
        return {"id": project_id, "name": project.get("name"), "deleted": True,
                "note": "its runs, pages, change records and webhook history were removed"}
    return _safe(go)


@tool(title="Start a project run now",
      annotations=_acts(),
      description="Crawl a project's site now with its saved settings, or re-read only the pages you list. The run "
                  "is compared with the one before it.\n"
                  "Use it after create_project or update_project, or whenever fresh results are wanted. For a site "
                  "with no project use crawl_site.\n"
                  "Inputs: project_id from list_projects. urls=['/pricing'] re-reads just those pages, resolved "
                  "against the project's site, and compares them with the last full run; a URL on another site is "
                  "refused before anything runs. wait=true returns the finished run instead of the queued one.\n"
                  "Cost: 1 to 4 credits per page. A run that uses up the credit budget stops and keeps what it "
                  "read.\n"
                  "Refusals: 409 while another run is queued or running (stop it with cancel_job), and 402 with no "
                  "credits left.\n"
                  "Timing: with wait=true, a run that outlasts the connection's time limit comes back as a job for "
                  "get_job. Read the results with list_pages and get_changes.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def start_run(project_id: ProjectId,
              urls: Annotated[list[str] | None, Field(
                  description="Up to 500 pages of the project's site to re-read, as full URLs or paths; omit to "
                              "crawl the whole site.")] = None,
              wait: Annotated[bool, Field(
                  description="true waits for the run to finish (minutes for a large site); false (default) "
                              "returns as soon as it is queued.")] = False) -> RunOut:
    def go():
        s = _client()
        budget = _budget()
        # `is not None`, not truthiness: an empty list goes to the API and is
        # refused there, as recrawl_pages([]) was, rather than quietly becoming
        # a full crawl that spends credits on every page of the site.
        if urls is not None:
            run = s.recrawl(project_id, urls)
            if not wait:
                return run
            try:
                return s.runs.wait(project_id, run["id"], timeout=3600 if budget is None else budget)
            except MeshArcTimeoutError as exc:
                return _still_running("run", getattr(exc, "job_id", "") or run["id"], project_id=project_id)
        if not wait or budget is None:
            return s.runs.start(project_id, wait=wait)
        try:
            return s.runs.start(project_id, wait=True, timeout=budget)
        except MeshArcTimeoutError as exc:
            return _still_running("run", getattr(exc, "job_id", "") or "",
                                  project_id=project_id)
    return _safe(go)


# A run as list_runs shows it: what an assistant needs to pick one, without
# the config, link graph and engine profile a run record also carries.
RUN_FIELDS = ("id", "status", "trigger", "scope", "pagesN", "changedN", "counts", "coverage", "stop",
              "startedAt", "elapsed", "baseline", "rebaselined")


@tool(title="List a project's runs",
      annotations=READS_STORED,
      description="List a project's runs, newest first, with each run's id, status, trigger, pages read and pages "
                  "changed.\n"
                  "Use it to find a run_id for list_pages or get_changes, to check whether a run is still "
                  "going before start_run, or to find the run to stop with cancel_job. get_project shows only the "
                  "last run.\n"
                  "Inputs: project_id from list_projects. limit=5 finds the latest few; runs past the limit cannot "
                  "be paged.\n"
                  "Errors: an unknown project answers 404.\n"
                  "Read-only and free: it reads stored data and never fetches.")
def list_runs(project_id: ProjectId,
              limit: Annotated[int, Field(
                  description="How many runs to return, 1 to 100 (default 25).")] = 25) -> RunsOut:
    """The runs of one project, so a run id can be found without guessing:
    the tools that read a run take one, and get_project shows only the last."""
    def go():
        rows = _client().runs.list(project_id, limit=max(1, min(limit, 100)))
        return {"runs": [{k: r.get(k) for k in RUN_FIELDS} for r in rows]}
    return _safe(go)


@tool(title="List, search or read a run's pages",
      annotations=READS_STORED,
      description="List the pages a project run stored; with q, find the pages whose text or markup matches; with "
                  "url, read one page in full, with its versions across runs.\n"
                  "Use it for what a project's runs stored: what a run covered, which pages say something, one "
                  "page's text. search_web searches the live web instead, get_job reads a crawl_site crawl, "
                  "scrape_urls fetches a live copy, and get_changes lists only what changed.\n"
                  "Inputs: project_id from list_projects. q=\'free trial\' needs both words, q=\'\"free trial\"\' the "
                  "phrase; mode=\'selector\' takes CSS such as 'form#signup' and needs a run that kept html (the "
                  "rawHtml format). url is an address, not a search term: scheme and trailing slash may differ, the "
                  "rest must match, query string included. Give q or url, not both. run_id from list_runs picks "
                  "the run.\n"
                  "Limits: a listing has up to 500 rows, shallowest first, so use q to reach pages past them. A "
                  "page read in full has each body capped at 12,000 characters, and a URL the runs did not store "
                  "answers 404.\n"
                  "Read-only and free: it reads stored data and never fetches.")
def list_pages(project_id: ProjectId,
               run_id: Annotated[str | None, Field(
                   description="A run's id, from list_runs; empty means the latest finished run, or with url the "
                               "newest copy across the project's last 50 runs.")] = None,
               q: Annotated[str | None, Field(
                   description="Text to find, 1 to 200 characters: words or a \"quoted phrase\", or with "
                               "mode='selector' a CSS selector or an XPath starting with / or (. Omit to list "
                               "every page.")] = None,
               mode: Annotated[Literal["content", "selector"], Field(
                   description="With q: 'content' (default) searches the markdown, 'selector' the stored html.")]
               = "content",
               url: Annotated[str | None, Field(
                   description="One page's full URL, as a listing shows it, to read that page in full; omit to "
                               "list.")] = None) -> PagesOut:
    def go():
        if q and url:
            return {"error": "give q (find pages) or url (read one page), not both", "code": "validation"}
        if url:
            # What get_page did until 0.6.0: one page in full, bodies capped.
            return _trim_page(_client().page(project_id, url, run_id))
        if q:
            return _client().search(project_id, q, mode=mode, run_id=run_id)
        r = _client().pages(project_id, run_id)
        r["pages"] = r.get("pages", [])[:500]
        return r
    return _safe(go)


@tool(title="Get what changed in a run",
      annotations=READS_STORED,
      description="Report what changed in a project run against the run just before it, or against any run you "
                  "name: pages added, modified and removed, field changes, and coverage.\n"
                  "Use it to answer 'what changed on the site', or 'since last month' with against. list_pages "
                  "shows every page whether or not it changed, and list_pages with url one page's text and "
                  "versions.\n"
                  "Inputs: project_id from list_projects. run_id from list_runs picks the run; empty means the "
                  "latest finished run. against, another run's id from list_runs, compares the two directly instead "
                  "of with the run just before. A run still going answers 409.\n"
                  "Safeguards: removals are withheld when a run reached under 90% of the site, so a blocked crawl "
                  "never reports pages as gone. A project's first run is a baseline with nothing to compare.\n"
                  "Read-only and free: it reads stored data and never fetches.")
def get_changes(project_id: ProjectId, run_id: RunId = None,
                against: Annotated[str | None, Field(
                    description="Another run's id, from list_runs, to compare with directly; omit to compare with "
                                "the run just before.")] = None) -> ChangesOut:
    def go():
        r = _client().changes(project_id, run_id, against=against)
        ch = r.get("change") or {}
        for k in ("feed", "fields", "withheld"):
            if isinstance(ch.get(k), list):
                ch[k] = ch[k][:200]
        return r
    return _safe(go)


JobKind = Annotated[Literal["crawl", "run", "batch", "agent"], Field(
    description="'crawl' (crawl_site), 'run' (start_run), 'batch' (scrape_urls with several URLs) or 'agent' "
                "(run_agent or continue_agent).")]
JobId = Annotated[str, Field(description="The job's id.")]
JobProject = Annotated[str | None, Field(description="A run's project id; required for a run, ignored otherwise.")]


@tool(title="Follow a long-running job",
      annotations=READS_STORED,
      description="Check on a crawl, run, batch, web search or agent run a tool handed back as a job, and return "
                  "its result once finished.\n"
                  "Call it when a tool answered with status 'running' and a job; repeating that tool is not needed. "
                  "To stop a crawl, run, batch or agent run use cancel_job. For a project's stored pages use "
                  "list_pages.\n"
                  "Inputs: kind, id and project_id come from the 'job' object; a crawl_site crawl_id is a crawl's "
                  "id. For a crawl, url reads one page in full, even mid-crawl, and cursor from the last answer "
                  "reads the next window.\n"
                  "Behaviour: it answers at once and never waits, so call it again every 10 to 30 seconds while the "
                  "status is 'running', or every 30 to 60 for an agent run. A finished crawl answers as crawl_site "
                  "does (up to 50 pages excerpted per window), a batch as scrape_urls, a search as search_web, an "
                  "agent run as run_agent, a run with its record. A scraping search whose pages are still landing "
                  "answers with its results and the job. A crawl expires a day after it started unless kept, then "
                  "answers 404; an agent run is kept 7 days, then answers with status 'expired'.\n"
                  "Read-only and free: it reads stored data and never fetches.")
def get_job(kind: Annotated[Literal["crawl", "run", "batch", "search", "agent"], Field(
                description="'crawl' (crawl_site), 'run' (start_run), 'batch' (scrape_urls with several URLs), "
                            "'search' (search_web) or 'agent' (run_agent or continue_agent).")],
            id: JobId,
            project_id: JobProject = None,
            url: Annotated[str | None, Field(
                description="Crawls only: one page's URL from the crawl's index.")] = None,
            cursor: Annotated[str | None, Field(
                description="Crawls only: the cursor the previous answer returned.")] = None) -> JobOut:
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
        if kind == "search":
            out = s.get_search(id)
            status = out.get("status")
            if status == "error":
                raise MeshArcError(502, out.get("error") or "the search failed", "job_failed")
            if _running(status):
                # A scraping search has its hits before its pages land: those
                # go back now, with the job, rather than a bare "running".
                return _search_result(out, running=True) if out.get("data") else _still_running("search", id)
            return _search_result(out)
        if kind == "agent":
            try:
                run = s.get_agent(id)
            except MeshArcError as exc:
                # Past its keep date the run is gone, which is an answer about
                # the run, not a failure of this call.
                if exc.status == 410:
                    return {"status": "expired", "id": id, "note": AGENT_EXPIRED}
                raise
            return _agent_answer(run.envelope)
        return {"error": f"kind must be crawl, run, batch, search or agent, not {kind!r}", "code": "validation"}
    return _safe(go)


# What a stop means, said once for every kind of job.
CANCEL_NOTE = ("queued work is dropped at once; a page being read finishes first, then the job "
               "stops. Pages already read stay readable, and nothing is deleted.")


@tool(title="Cancel a running job",
      annotations=_acts(destructive=True, idempotent=True, open_world=False),
      description="Stop a crawl, run, batch or agent run that is still going.\n"
                  "Use it when the work should not finish: a crawl started too wide, a run blocking the next "
                  "start_run (which answers 409 meanwhile), a batch of the wrong URLs, an agent run on the wrong "
                  "question. To check a job without stopping it use get_job.\n"
                  "Inputs: kind and id come from the 'job' object, or a crawl_site crawl_id with kind='crawl'. A "
                  "run also needs project_id; list_runs shows which run is still going.\n"
                  "Effect: queued work is dropped and the page in hand finishes; an agent run stops before its "
                  "next step, and the pages and model tokens it used stay charged. Pages already read stay. A stop "
                  "cannot be undone; run the work again to resume. A finished job is left alone, so asking twice is "
                  "harmless.\n"
                  "Needs write access; a read-only connection is refused with code 'read_only'.")
def cancel_job(kind: JobKind, id: JobId, project_id: JobProject = None) -> CancelOut:
    """The way out of a job that should not finish: a crawl started too wide, a
    run that blocks the next one (start_run answers 409 while one is going), a
    batch of the wrong URLs. Every kind stops the same way, through the API's
    own cancel; a job already finished is left as it is and reported with its
    status (a crawl or batch is checked first and nothing is sent; a run's
    cancel goes to the API, which changes nothing on a finished run), so asking
    twice is harmless."""
    def go():
        s = _client()
        if kind == "run":
            if not project_id:
                return {"error": "a run needs its project_id", "code": "validation"}
            r = s.runs.cancel(project_id, id) or {}
            # The API says what it did: cancelled (it never started), cancelling
            # (it stops after the page in hand), or the status of a run that had
            # already ended.
            return {"kind": "run", "id": id, "project_id": project_id,
                    "outcome": r.get("outcome") or "no job", "note": CANCEL_NOTE}
        if kind == "crawl":
            job = s.get_crawl(id)
            status = job.envelope.get("status")
            if not _running(status):
                return {"kind": "crawl", "id": id, "outcome": status,
                        "note": "the crawl had already finished; nothing to stop"}
            job.cancel()
            return {"kind": "crawl", "id": id,
                    "outcome": "cancelled" if status == "queued" else "cancelling", "note": CANCEL_NOTE}
        if kind == "batch":
            # Only the status is wanted: a format the API does not keep a body
            # for returns the rows without markdown, where the default would
            # carry every page of a finished 500-url batch just to read one word.
            status = s.batch(id, formats="none").get("status")
            if not _running(status):
                return {"kind": "batch", "id": id, "outcome": status,
                        "note": "the batch had already finished; nothing to stop"}
            s.cancel_batch(id)
            return {"kind": "batch", "id": id,
                    "outcome": "cancelled" if status == "queued" else "cancelling", "note": CANCEL_NOTE}
        if kind == "agent":
            # The API says what it did: cancelled (queued), cancelling (the
            # step under way finishes first), or a finished run's status.
            answer = s.get_agent(id).cancel() or {}
            return {"kind": "agent", "id": id, "outcome": answer.get("status"), "note": AGENT_CANCEL_NOTE}
        return {"error": f"kind must be crawl, run, batch or agent, not {kind!r}", "code": "validation"}
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
