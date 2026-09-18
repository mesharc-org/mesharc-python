# mesharc

The Python client for the [MeshArc](https://mesharc.dev) API: a URL in, clean content out, and a record of what changed.

- **Scrape** one page or a batch — markdown, text, HTML, links, structured fields, a screenshot.
- **Crawl** a whole site with no project to set up first, and keep it as one if it turns out to be worth watching.
- **Map** what a site declares in its sitemaps before fetching any of it.
- **Watch** a site over time: projects, scheduled runs, and a change record — pages added, removed, modified, field by field.

Python 3.9 or newer. One dependency (`httpx`). Fully typed.

## Install

```bash
pip install mesharc
```

## Authentication

Every call needs an API key. Create one in the app under **Settings → API keys** — it is shown once — and give it to the client, or put it in `MESHARC_API_KEY` and construct the client with nothing:

```python
from mesharc import MeshArc

arc = MeshArc("mesharc_...")
# or, with MESHARC_API_KEY in the environment:
arc = MeshArc()
# options: MeshArc(api_key, timeout=150.0, max_retries=2)
```

The key is only ever sent as a bearer header to `api.mesharc.dev`.

A key carries the scopes it was made with (`read`, `write`, `admin`), optionally a set of projects it may see, an expiry and a rate limit. A route the key may not use answers `403`; a project it may not see answers `404`.

## Quick start

```python
from mesharc import MeshArc

arc = MeshArc("mesharc_...")

page = arc.scrape("https://example.com/pricing")
print(page["markdown"])
print(page["verdict"], page["method"], page["credits"])   # ok crawler 1
```

`scrape` holds the request open until the page comes back (60 s by default), so there is nothing to poll for an ordinary page.

## Reading pages

### One page

```python
page = arc.scrape("https://quotes.toscrape.com/js/", config={"render_js": "always"})
```

`config` is any setting a project takes, by its API name (`render_js`, `only_main_content`, `formats`, `max_tier`, `wait_for_selector`, `actions`, …). The full list, with defaults, is at [mesharc.dev/docs/configuration](https://mesharc.dev/docs/configuration).

```python
page = arc.scrape_one(
    "https://example.com/",
    formats="markdown,text,cleanHtml",     # which bodies to return
    timeout_s=120,                         # how long the API holds the request (120 max)
    idempotency_key="pricing-2026-09-18",  # the same key returns the first answer for 24 h
)
```

### Many pages

A list of URLs is a batch: grouped by host, fetched in parallel where the config allows, and returned as one row per URL.

```python
batch = arc.scrape(["https://a.com/", "https://b.com/x"], config={"concurrency": 4})
for row in batch["pages"]:
    print(row["url"], row["httpStatus"], row["verdict"], row["credits"])
```

`arc.scrape(urls, wait=False)` returns the batch id at once; `arc.batch(id, wait=True)` finishes it later. Pass `webhook_url=` to be told instead of polling (`batch.finished`, signed with a secret returned once).

### What a page looks like

Every page row carries the same fields, whether it came from a scrape, a crawl or a project:

| Field | Meaning |
|---|---|
| `markdown`, `text`, `cleanHtml`, `html`, `links`, `fields`, `screenshot` | The bodies you asked for |
| `httpStatus` | The status the site answered with |
| `verdict` | `ok`, `thin` (short, but a page), `blocked` (refused, or a 404), `skipped` |
| `errorCode` | `OK`, or what went wrong: `BLOCKED`, `NOT_FOUND`, `TIER_LIMIT`, `CAPTCHA`, `LOGIN_REQUIRED`, `RATE_LIMITED`, `TIMEOUT` … |
| `shape`, `warnings`, `signals` | `listing` / `table` / `form` for a short page whose markup says what it is; `short`; why the judge decided as it did |
| `method`, `tier`, `climbedTo` | The engine that read it (`crawler`, `tls`, `minted`, `browser`, `browser-residential`, …), its tier, and how far a refused page climbed |
| `credits`, `billedAs` | What it cost; the rung it is priced at when not the one that fetched it |
| `words`, `language`, `head`, `reason`, `crawledAt` | Size, language, the head fields, the judge's sentence, when |

A page the site refused costs 0, and so does a 404.

## Crawling a site

```python
job = arc.crawl(
    "https://docs.example.com",
    limit=200,                          # page budget
    maxDepth=3,                         # link hops from the seed
    includePaths=["/docs/*"],
    crawlMode="sitemap_first",          # what the sitemap declares first, then links
    maxTier="browser",                  # how far a refused page may climb
    scrapeOptions={"formats": ["markdown", "links"]},
    config={"crawl_delay_ms": 500},     # any project setting, directly
)

for page in job.pages():                # follows the cursor while the crawl runs
    print(page["url"], page["words"])
print(job.status, job.envelope["counts"], job.envelope["creditsUsed"])
```

`crawl` returns a handle immediately; `job.pages()` yields pages as they land and ends when the crawl does. `wait=True` blocks until it finishes; `job.wait()`, `job.refresh()`, `job.cancel()` do what they say; `arc.get_crawl(id)` reattaches to a crawl started elsewhere.

A one-shot crawl expires after 30 days. If the site is worth watching:

```python
project = job.keep(name="Docs", schedule="weekly")
```

A `webhook=` in the options (`{"url", "events", "metadata"}`) is told about `crawl.started`, `crawl.page` (fifty pages a message) and `crawl.completed`; its signing secret comes back once as `job.webhook_secret`.

## Mapping a site

```python
for u in arc.map("https://docs.example.com"):
    print(u["url"], u["lastmod"])

details = arc.map_details("https://www.gov.uk/", search="visa", limit=500)
print(details["totals"], details["creditsUsed"])   # {'files': 29, 'urls': 508431, …} 29
```

A map costs one credit per sitemap file read — most sites are one file.

## Watching a site: projects and runs

```python
project = arc.projects.create(
    "https://docs.example.com",
    name="Docs",
    schedule="weekly",                                        # manual | hourly | daily | weekly
    config={"max_pages": 300, "include_paths": ["/docs/*"]},
)

run = arc.runs.start(project["id"], wait=True)                # the first run
# ...a week later, or arc.runs.start again: the second run produces the change record

record = arc.changes(project["id"])
print(record["change"]["counts"])   # {'added': …, 'removed': …, 'modified': …, 'withheld': …}

diff = arc.page_diff(project["id"], "https://docs.example.com/pricing")
```

| Method | What it does |
|---|---|
| `projects.list()` · `projects.get(id)` · `projects.update(id, name=, schedule=, retention=, config=)` · `projects.delete(id)` | The projects |
| `runs.list(project_id)` · `runs.start(project_id, wait=)` · `runs.wait(project_id, run_id)` · `runs.get(project_id, run_id)` · `runs.cancel(project_id, run_id)` | Runs |
| `pages(project_id, run_id=None)` · `page(project_id, url, run_id=None)` | The pages of a run; one page in full |
| `changes(project_id, run_id=None)` · `page_diff(project_id, url, run_id=None)` | The change record; one page's word-level diff |
| `search(project_id, q, mode="content" \| "selector", run_id=None)` | Which pages say this (words, `"phrases"`) or contain this (CSS / XPath) |
| `recrawl(project_id, urls)` | Fetch these pages again, now |
| `sources(project_id)` | The seed, sitemap, URL list, feeds and patterns with what the last run found through each |
| `export(project_id, path, dataset="pages", fmt="jsonl", run_id=None, urls=None)` | Stream a dataset (`pages`, `markdown`, `changes`, `fields`, `sitemap`) as `jsonl` or `csv` to a file |

```python
arc.export(project["id"], "pages.csv", dataset="pages", fmt="csv")
```

## The workspace

```python
me = arc.me()               # the workspace, its plan and limits, credits used and remaining, what this key may do
usage = arc.usage()         # pages per day, this month by engine
monitor = arc.monitor()     # what is queued and running
meta = arc.meta()           # verdict meanings, engine costs, the config defaults
keys = arc.keys()
key = arc.create_key("ci", scopes=["read", "write"], projects=[project["id"]], expires_in_days=90)   # key["key"], once
arc.revoke_key(key["id"])
```

## Errors

Every failure raises `MeshArcError`:

```python
from mesharc import MeshArc, MeshArcError

try:
    arc.crawl("https://example.com", limit=1_000_000)
except MeshArcError as exc:
    print(exc.status, exc.code, exc.detail, exc.request_id)
```

| `code` | Status | Meaning |
|---|---|---|
| `validation` | 400 / 422 | Something in the request is wrong; `detail` says what |
| `unauthorized` | 401 | No key, or a revoked or expired one |
| `plan_limit` | 402 | The plan does not include this, or the credits are spent |
| `forbidden` | 403 | The key's scopes do not allow it |
| `not_found` | 404 | No such thing — or not one this key may see |
| `conflict` | 409 | The request contradicts current state |
| `rate_limited` | 429 | Over the key's rate limit; `X-RateLimit-Reset` says when |
| `internal` | 500 | Quote `request_id` to support |

`request_id` is the id the API put on the response and in its own logs, so a support conversation starts from one string.

Two more cases: a network failure or a request that hits `timeout` raises `MeshArcError` with `status == 0` and `code` `network` or `timeout`; a job the client stopped waiting for raises `MeshArcTimeoutError` — both a `MeshArcError` and a `TimeoutError` — which carries `job_id` so you can poll it later (`arc.get_crawl(id)`, `arc.batch(id)`).

## Idempotency and timeouts

- `scrape`, `scrape_one` and `crawl` take `idempotency_key=`: send the same key again within 24 hours and you get the first answer back rather than a second job.
- Waiting calls take `wait=`, `poll=` (seconds between polls) and `timeout=` (seconds before `TimeoutError`). `wait=False` returns the envelope at once; the default polls every 3 s for up to an hour.
- `timeout_s` on a single scrape is how long the API itself holds the request open (60 s by default, 120 at most); a slower page comes back as an id and is polled.
- `MeshArc(..., timeout=150.0)` is the HTTP timeout per request. A request is retried on 429, 502, 503, 504 and network failures when it is safe to repeat — a GET, a DELETE, or a POST with an idempotency key — up to `max_retries` times (2), honouring `Retry-After`.

## Credits

Every response says what it cost: `credits` on a page, `creditsUsed` on a job envelope, `X-MeshArc-Credits` on the HTTP response. A page costs the engine that read it — a plain fetch 1, a render 4 — and a refused page or a 404 costs nothing. The schedule and the plans are at [mesharc.dev/docs/billing](https://mesharc.dev/docs/billing).

## The MCP server

The package also ships MeshArc as an MCP server, so Claude Desktop, Claude Code, Cursor and any MCP client can scrape, crawl, map and read change records as tools. Python 3.10+.

```bash
pip install "mesharc[mcp]"
MESHARC_API_KEY=mesharc_... mesharc-mcp          # serves over stdio

# Claude Code
claude mcp add mesharc -e MESHARC_API_KEY=mesharc_... -- mesharc-mcp
```

Tools: `scrape_urls`, `extract_url`, `map_site`, `crawl_site`, `keep_crawl_as_project`, `list_projects`, `create_project`, `start_run`, `list_pages`, `get_page`, `get_changes`, `search_pages`, `recrawl_pages`. Every tool is a call through this client, trimmed where a body would swamp a context window (markdown is capped per page; ask for one page to get all of it).

## Anything else

The client is a thin wrapper: every method is one API call and returns the API's JSON as a `dict`. The full reference is at [mesharc.dev/docs/api](https://mesharc.dev/docs/api). Call `arc.close()` when you are done, or use the client as a context manager.

- Documentation: [mesharc.dev/docs](https://mesharc.dev/docs)
- Node client: `npm install mesharc` — [mesharc-node](https://github.com/Siddharth-DWT/mesharc-node)
- Issues and pull requests: [mesharc-python](https://github.com/Siddharth-DWT/mesharc-python)
- Questions: hello@mesharc.dev

MIT.
