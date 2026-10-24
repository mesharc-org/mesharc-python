# mesharc

The Python client for the [MeshArc](https://mesharc.dev) API: a URL in, clean content out, and a record of what changed.

- **Scrape** one page or a batch — markdown, text, HTML, links, structured fields, a screenshot.
- **Crawl** a whole site with no project to set up first, and keep it as one if it turns out to be worth watching.
- **Map** what a site declares in its sitemaps before fetching any of it.
- **Search** the web for pages whose URLs you do not know, and read each result's page in the same call.
- **Ask** the agent a question: it searches, reads pages and answers, in the shape of a schema if you give one, with the pages its answer rests on. (Not live on mesharc.dev yet.)
- **Monitor** a search or a question: asked again hourly, daily or weekly, each answer compared with the last, a webhook told when it changed. (Not live on mesharc.dev yet.)
- **Watch** a site over time: projects, scheduled runs, and a change record — pages added, removed, modified, field by field.

Python 3.10 or newer. One dependency (`httpx`). Fully typed.

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

A `webhook=` in the options (`{"url", "events", "metadata", "include"}`) is told about `crawl.started`, `crawl.page` (fifty pages a message) and `crawl.completed`; its signing secret comes back once as `job.webhook_secret`. With `"include": ["markdown"]` the `crawl.page` messages carry each page's markdown too, up to about 1 MB a message: a page that does not fit, or was not stored, comes as `"markdown": null` with `markdownOmitted` (`too_large` or `not_stored`) and a `contentUrl`, an API path read with your key (`job.page(url)` reads the same). It needs `crawl.page` among the events, as it is when `events` is left out.

## Mapping a site

```python
for u in arc.map("https://docs.example.com"):
    print(u["url"], u["lastmod"])

details = arc.map_details("https://www.gov.uk/", search="visa", limit=500)
print(details["totals"], details["creditsUsed"])   # {'files': 29, 'urls': 508431, …} 29
```

A map costs one credit per sitemap file read — most sites are one file.

## Searching the web

```python
out = arc.web_search(
    "python packaging guide",
    limit=5,                            # 1 to 10 results
    freshness="month",                  # hour | day | week | month | year
    include_domains=["python.org"],     # exclude_domains= too; an exclude wins
    scrape=True,                        # each result's page as markdown as well
)

if out["status"] == "blocked":          # every engine refused; nothing was charged
    print(out["attempts"])
else:
    for hit in out["data"]:
        print(hit["position"], hit["title"], hit["url"], hit["snippet"])
        if hit.get("page"):
            print(hit["page"].get("markdown", "")[:200])
print(out["engine"], out["cached"], out["creditsUsed"])

again = arc.get_search(out["id"])       # a search started earlier, by id
for s in arc.searches(q="python"):      # the workspace's searches, newest first
    print(s["id"], s["query"], s["status"], s["resultCount"], s["creditsUsed"])

news = arc.web_search("python release", news=True, freshness="day")   # the engines' news results
for hit in news["data"]:
    print(hit["publisher"], hit["age"], hit["title"])                  # who published it, and how long ago ('20h')
more = arc.web_search("python packaging guide", page=2)               # results 11-20
```

`web_search` waits for the results and returns the whole envelope, the hits under `data`. A search is `queued`, `running`, `done`, `blocked` or `error`: `blocked` — every engine refused — is returned, not raised; `error` raises `MeshArcError`. A search that scrapes stays `running` until its pages land. `scrape=` also takes a dict, sent as given (`{"formats": ["markdown", "links"], "maxCredits": 20}`); `country=` and `lang=` say where and in what language to search; `wait=False` returns at once, and `timeout_s` is how long the API holds the request (60 s by default, 120 at most) before the client polls.

`news=True` searches the engines' news results rather than the web's (one or the other per search); each news hit also carries `publisher` and `age`, as the engine put it (`"20h"`). `page=` is which results page, 1 to 10: page 2 is results 11–20, and each page is its own search with its own charge. **`news=` and `page=` need an API with those features**: they are on the API's feature branch, not yet on mesharc.dev.

A search needs a key that can write, and it spends credits. The results page costs the engine that read it; a results page every engine refused is free. An equal search — same query, `news`, `page`, `country`, `lang`, `freshness` and domains — within an hour of a finished one (ten minutes for news) comes from the cache (`cached` is true) with no charge for the results page. Scraped pages are charged, cached or not.

`arc.search(project_id, q)` is something else: it searches the pages a project has stored, not the web (see below).

## Asking the agent

**The agent route (`/agent`) is not live on mesharc.dev yet**; web search is. The client and the MCP tools are ready for it. The agent's webhook, `connection_id`, `continue_()` and `field_sources` need an API with those features too: they are on the API's feature branch, not yet on mesharc.dev.

The agent takes a question, searches the web, maps sites and reads pages on its own, and answers with the pages it used. Give it a schema and the answer comes back in that shape:

```python
run = arc.agent(
    "What does each plan on example.com cost per month?",
    urls=["https://example.com/pricing"],   # pages to start from, up to 20; omit to let it search
    allowed_domains=["example.com"],        # keep it to these sites, up to 20
    schema={                                # a JSON Schema of type object or array
        "type": "object",
        "properties": {"plans": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "monthly": {"type": "number"}}}}},
    },
    max_credits=500,                        # the most the run may spend: 1 to 100,000, 2,000 by default
    max_steps=20,                           # the most steps it may take: 1 to 100, 40 by default
)

out = run.wait()                            # blocks until it finishes; the envelope
print(out["status"], out["creditsUsed"], out["steps"])
print(run.data)                             # {'plans': [...]}, matching the schema
for src in run.sources:                     # the pages the answer rests on
    print(src["url"], src["title"])
print(run.field_sources)                    # {'plans[0].monthly': {'url': ..., 'pageId': ...}, ...}
```

`agent` returns an `AgentRun` at once, queued or running; the prompt is up to 10,000 characters. `timeout_s=` asks the API to hold the request for the answer (120 s at most). Without a schema the answer is `{"text": ...}`. `field_sources` says where each value of the answer came from: its path (`"plans[0].monthly"`, or `"[2].name"` for a list answer) to `{url, pageId}`, a page the run read.

```python
run = arc.agent(
    "What does each plan on example.com cost per month?",
    webhook={"url": "https://hooks.example.com/mesharc", "events": ["agent.completed", "agent.failed"]},
    connection_id="conn_...",               # run on one of the workspace's own LLM connections
)
print(run.webhook_secret)                   # once: the secret the webhook's messages are signed with
```

`webhook=` is a URL, or `{"url", "events", "metadata"}`, told about `agent.started`, `agent.action`, `agent.completed`, `agent.failed` and `agent.cancelled` (all five by default); its signing secret comes back once, as `run.webhook_secret`; the handle `continue_()` returns carries the stopped run's secret on (the new run keeps the same webhook), and a run reopened with `get_agent` has none. `connection_id=` runs the agent on one of the workspace's own LLM connections — its model, its key, its bill.

To watch it work, follow the trace:

```python
run = arc.agent("Who maintains the httpx library, and where is it developed?")
for event in run.trace():                   # follows the run until it ends
    print(event["seq"], event["kind"], event["text"])
print(run.wait()["data"])                   # {'text': '...'}
```

`kind` is `start`, `resume`, `continue`, `model`, `search`, `fetch`, `render`, `map`, `select`, `extract`, `lookup` (a data provider's record), `propose`, `tool`, `busy` or `finish`. `trace(after=seq)` resumes after an event already seen; `follow=False` yields what exists and returns.

```python
run.cancel()                                # {'id': ..., 'status': 'cancelled'} if it was queued, 'cancelling' if running
run.refresh()                               # the run as it stands now

again = arc.get_agent(run.id)               # a run started earlier, by id
for r in arc.agent_runs(status="done"):     # the workspace's runs, newest first
    print(r["id"], r["status"], r["creditsUsed"], r["prompt"])
from datetime import date
for r in arc.agent_runs(model="openai:gpt-5.4-mini", since="2026-10-01", until=date.today()):
    print(r["id"], r["model"], r["createdAt"])
```

A run is `queued`, `running`, `done`, `error`, `cancelled` or `credit_limit`. `wait()` returns a run that is `done`, `cancelled` or stopped at `credit_limit` — the run reached `max_credits`, and what it had found is under `data["partial"]` — and raises `MeshArcError` 502 `job_failed` for `error`, with the API's text -- and for any other end it does not know, as the Node client does. A cancel stops a queued run at once and a running one before its next step; what it used stays charged. `arc.cancel_agent(id)` does the same by the run's id, with no handle needed.

`agent_runs` filters by `status`, by `model` (as runs name it) and by when the run was made: at or after `since`, before `until`, each an ISO 8601 date or date-time string or a `date` / `datetime`. A date means its midnight UTC; a date-time without a zone is read as UTC (so a naive `datetime.now()` is your local time read as UTC — pass `datetime.now(timezone.utc)` instead).

A run that stopped at `credit_limit` can be carried on with a new budget:

```python
out = run.wait()
if out["status"] == "credit_limit":
    more = run.continue_(max_credits=1_000)   # a new run, on the same thread
    print(more.continues_run_id)              # run.id
    print(more.wait()["data"])                # the full answer, replacing the partial one
```

`continue_()` returns an `AgentRun` on the new run, which resumes from where the stopped one was, keeps the pages it read (not paid for again) and its webhook, whose secret the new handle's `webhook_secret` carries on. `max_credits` and `max_steps` are the new run's own; left out, they are the stopped run's. The stopped run's `continued_by` names the new one once refreshed. A run that did not stop at its credit limit, was already continued or has no saved progress raises `MeshArcError` 409 with code `conflict`; one past its keep date, 410 `expired`.

A run needs a key that can write, and it spends credits: each page it reads is charged as a read (a refused page is free), and the model's tokens at the model provider's price plus 20% — or 1 credit per 1,000 tokens on the workspace's own connection — all within `max_credits`. A run is kept 7 days; after that `get_agent` raises `MeshArcError` with status 410 and code `expired`, and `trace()` ends quietly.

### Follow-ups and conversations

**Not live on mesharc.dev yet:** everything in this section needs an API with these features, which are on the API's feature branch.

```python
first = arc.agent("Which plans does example.com sell?", urls=["https://example.com/pricing"])
first.wait()
nxt = first.follow_up("And which of them include SSO?")   # the same conversation
print(nxt.wait()["data"])

for c in arc.conversations(q="example.com"):              # the latest message first
    print(c["id"], c["messages"], c["title"])
thread = arc.conversation(first.id)                        # every run in it, oldest first, under "runs"
```

A follow-up answers after everything asked and found in the conversation before it, and may read again, free, any page the conversation read. Its request is its own — `follow_up` takes what `agent` takes — and it pays for its own work. Only the conversation's latest message can be followed, once it has finished: `MeshArcError` 409 `conflict` otherwise (the error names the latest), or past 30 messages.

### Saved logins, data providers and proposals

**Not live on mesharc.dev yet:** everything in this section needs an API with these features, which are on the API's feature branch.

```python
run = arc.agent(
    "How many employees does Stripe have, and what was Apple's revenue last year?",
    data_connections=["conn_pdl", "conn_fmp"],   # the workspace's data providers, one per provider
    logins="auto",                               # the saved logins it may sign in with (the default)
    proposals=True,                              # it may propose things to watch
)
```

`data_connections=` names up to two of the workspace's data-provider connections (`arc.connections`, below): People Data Labs for company data, Financial Modeling Prep for financial data. The agent may then look things up on your own key — the provider bills it, MeshArc charges no credits — and each record found is one of `run.sources`, as `{url, title, provider}`, `url` being the request it came from (your key is never in it). The agent reads pages signed out and signs in, page by page, where a page needs the account, with the workspace's saved login for that page's site (`arc.logins`); `logins=` says which it may use (every one by default, `"none"`, or a list of ids), and `login_id=` is the older form of a list of one; logins are switched on per deployment. `proposals=True` lets the agent propose crawling a site or keeping a search or question on a schedule; nothing is made until a member approves it (`arc.proposals`).

## Monitors: a search or a question, asked again on a schedule

**Monitors (`arc.monitors`, the `/monitors` route) need an API with that feature**: it is on the API's feature branch, not yet on mesharc.dev. So do their caps, alerts, records and tone, below.

```python
monitor = arc.monitors.create(
    "search",                                         # or "agent"
    {"query": "mesharc", "freshness": "day"},         # a POST /search or POST /agent body, in the API's own names
    "daily",                                          # hourly | daily | weekly
    name="Mentions",
    webhook="https://hooks.example.com/mesharc",      # told search.changed / agent.changed
)
print(monitor["webhookSecret"])                       # once

for r in arc.monitors.runs(monitor["id"]):            # newest first
    print(r["createdAt"], r["status"], r["changed"], r["summary"])

press = arc.monitors.create("search", {"query": "mesharc", "sources": ["news"]}, "hourly")   # a news monitor
arc.monitors.update(monitor["id"], schedule="weekly")  # only what is not None is sent
arc.monitors.update(monitor["id"], webhook="")         # the empty string removes the webhook
```

Each run is the ordinary search or agent run, charged as one, and is compared with the last run that answered: new, dropped and moved results for a search, the answer's added, changed and removed values for an agent run. A run's `refId` is the search or agent run it made (`arc.get_search` / `arc.get_agent`).

The `request` is the body of a POST /search or POST /agent in the API's own (camelCase) names — `{"query": ..., "includeDomains": [...]}`, `{"prompt": ..., "schema": ..., "maxCredits": ...}` — not `web_search`'s or `agent`'s Python arguments. So a news monitor says `"sources": ["news"]`: `web_search(news=True)` maps its argument to that, and `monitors.create` does not. A monitored search reads its results page only, so `scrape` is refused. `baseline_id=` takes a finished search or agent run with the same request as the first run, not paid for again (for a search, the baseline's `limit` must be at least the monitor's); without it the first run starts now.

| Method | What it does |
|---|---|
| `monitors.create(kind, request, schedule, name=None, webhook=None, baseline_id=None, idempotency_key=None, budget=None, alerts=None, add_to_project=None, rows=None, destination=None, sentiment=None)` | Keep a search or agent request on a schedule |
| `monitors.list(kind=None)` · `monitors.get(id)` | The workspace's monitors, each with its `lastRun`; one monitor |
| `monitors.overview(kind=None)` | Everything on a schedule — projects and monitors — with next and last run, this month's spend, an estimate and caps |
| `monitors.update(id, name=None, schedule=None, status=None, webhook=None, budget=, alerts=, add_to_project=, rows=, destination=, sentiment=)` | Change what is not None; `status` is `active` or `paused`, `webhook=""` removes the webhook |
| `monitors.pause(id)` · `monitors.resume(id)` | Stop and restart the schedule; a slot missed while paused runs once on resuming |
| `monitors.run(id, idempotency_key=None)` | Run it now; 409 `conflict` while a run is under way |
| `monitors.runs(id, limit=25)` | Its runs, with `changed`, `summary` and `diff`, following `next` |
| `monitors.delete(id)` | The monitor and its runs; the searches and agent runs it made stay |
| `monitors.rows(id, active=None, q=None)` · `monitors.row(id, key)` · `monitors.row_events(id, run_id=None, kind=None)` · `monitors.export_rows(id, path, dataset="rows", fmt="csv")` | An agent monitor's records, one record with its history, what moved, and all of it as a file |

```python
plans = arc.monitors.create(
    "agent", {"prompt": "List example.com's plans with their monthly price", "urls": ["https://example.com/pricing"],
              "allowedDomains": ["example.com"]}, "weekly",
    budget={"perRun": 300, "perMonth": 1500},          # credits; 0 or left out is no cap
    alerts={"email": True, "chatUrl": "https://hooks.slack.com/services/..."},
    rows={"path": "plans", "key": "name"},             # follow the answer's list, plan by plan
)
for row in arc.monitors.rows(plans["id"]):
    print(row["key"], row["attrs"], row["sources"])   # its columns, and the page each was read on
for event in arc.monitors.row_events(plans["id"]):    # added, changed (before and after), gone
    print(event["kind"], event["key"])
```

`budget` caps each run (`perRun` lowers an agent monitor's `maxCredits`) and the month (a run due past `perMonth` is skipped, and the webhook hears `budget.reached` once a month). `alerts` sends the workspace's run emails and a chat message (Slack, Discord, Teams or Google Chat incoming webhook) when a run finds something. A search monitor's `add_to_project` adds its new results on that project's site to the project; an agent monitor's `rows` follows a list answer record by record, and `destination` writes the records out after each run. A news monitor's `sentiment={"connectionId": ..., "only": ["negative"]}` scores each run's new results with one of the workspace's LLM connections (1 credit per 1,000 tokens) and keeps chat and email to those tones. `update` replaces `budget`, `alerts` and `sentiment` whole (`{}` for none); `add_to_project=""` and `destination=""` remove them.

Creating and running a monitor spend credits. Every call but `monitors.list`, `monitors.get` and `monitors.runs` needs a key that can write. A webhook's secret comes back once, as `webhookSecret`, on the call that set it. `arc.monitor()` is something else: the workspace's job queue, what is queued and running (see below).

## The agent over a table

**Not live on mesharc.dev yet:** everything in this section needs an API with these features, which are on the API's feature branch.

```python
table = "Company,Website\nAcme,acme.com\nGlobex,globex.com\n"
ask = dict(csv=table, max_credits_per_row=300, urls=["{{Website}}"],
           schema={"type": "object", "properties": {"plan": {"type": "string"}, "price_month": {"type": "number"}}})
print(arc.agent_batches.preview("What does {{Company}} charge for its cheapest plan?", **ask)["estimate"])
batch = arc.agent_batches.start("What does {{Company}} charge for its cheapest plan?", **ask, concurrency=3)
arc.agent_batches.wait(batch["id"])
for row in arc.agent_batches.rows(batch["id"]):
    print(row["values"]["Company"], row["status"], row["data"])
arc.agent_batches.export(batch["id"], "prices.csv")
```

One agent run a row: the prompt and starting URLs filled from the row's columns, written `{{column}}`, and the answer — shaped by `schema`, of type object — the row's new columns. Send the table as `csv` or as `rows`, a list of objects. `max_credits_per_row` is required: each row stops there and pays only for what it used, and the batch never spends more than rows × the cap, or your balance when it started if that was less — a row it can no longer afford is `skipped`, free. Without `allowed_domains` each row stays on its own URLs' sites; `[]` lifts the limit. `preview` checks everything and estimates the cost without starting anything. `login_id` and `data_connections` are as `agent()` takes them, for every row.

| Method | What it does |
|---|---|
| `agent_batches.preview(prompt, csv=, rows=, max_credits_per_row=, ...)` | What would run, the rows that cannot and why, the first prompts, and the estimate; free |
| `agent_batches.start(prompt, ..., webhook=None, idempotency_key=None)` | Start it; `webhookSecret` comes back once, and the webhook hears `agent.batch.finished` once |
| `agent_batches.get(id, offset=0, limit=50, status=None)` · `agent_batches.rows(id, status=None)` · `agent_batches.wait(id)` | One page of rows; every row; block until it ends |
| `agent_batches.list()` · `agent_batches.cancel(id)` · `agent_batches.export(id, path, fmt="csv")` | The batches; stop one; the table with its answers as a file (a CSV cell that would start a formula is written as text) |

A batch is kept 7 days, then `get` raises `MeshArcError` 410 `expired`.

## Triggers, proposals and what a month costs

**Not live on mesharc.dev yet:** everything in this section needs an API with these features, which are on the API's feature branch.

```python
arc.triggers.create(
    {"kind": "project", "id": project["id"]},
    {"type": "agent", "prompt": "Summarise what changed on {{url}}", "maxCredits": 300},
    when={"kinds": ["modified"], "paths": ["/pricing*"]},
    limits={"cooldownMinutes": 60, "maxPerDay": 5},
)
for p in arc.proposals.list():                 # what agents proposed to watch, waiting
    print(p["kind"], p["name"], p["estimate"])
    arc.proposals.approve(p["id"])             # or arc.proposals.dismiss(p["id"])
print(arc.estimate("agent", "weekly", max_credits=300))   # about what a month costs, before saving
```

| Method | What it does |
|---|---|
| `triggers.create(source, action, when=None, limits=None, name=None, status=None)` | Start an agent run, a re-crawl, a monitor run or a re-check when a project's changes or a monitor's run match |
| `triggers.list(source_id=None, target_id=None)` · `triggers.get(id)` · `triggers.update(id, ...)` · `triggers.delete(id)` | The triggers |
| `triggers.fires(id)` · `triggers.preview(id)` | What each source run matched and started (kept 90 days); what the latest run would make of it, firing nothing |
| `proposals.list(status="proposed", run_id=None)` · `proposals.approve(id)` · `proposals.dismiss(id)` | Approve makes the project or monitor (a monitor's first run starts now) |
| `estimate(kind, schedule, project_id=, monitor_id=, max_pages=, max_tier=, max_credits=, month_cap=)` | About how many credits a crawl, search or agent on a schedule costs a month; `upTo` when nothing has run yet |

## Sub-accounts

**Not live on mesharc.dev yet:** everything in this section needs an API with these features, which are on the API's feature branch.

A workspace on Scale or Enterprise can make workspaces of its own for its customers: each with its own keys, projects and data, spending this workspace's credits under a cap of its own. Every call needs an admin key.

```python
acme = arc.sub_accounts.create("Acme Ltd", monthly_cap=50_000, external_id="cust-42", idempotency_key="cust-42")
key = arc.sub_accounts.create_key(acme["id"], name="acme", scopes=["read", "write"])   # key["key"], once: for Acme

with arc.as_sub_account(acme["id"]) as acme_arc:      # your key, acting on Acme: X-MeshArc-Tenant on every request
    acme_arc.scrape("https://example.com")
arc.sub_accounts.update(acme["id"], suspended=True, reason="unpaid")   # refuses its keys, cancels what it runs
```

| Method | What it does |
|---|---|
| `sub_accounts.list()` · `sub_accounts.get(id)` | `{data, allowed, spent, remaining}`; one with its month, spend by kind, charges, and your keys' access |
| `sub_accounts.create(name, monthly_cap=None, external_id=None, idempotency_key=None)` · `sub_accounts.update(id, name=, monthly_cap=, suspended=, reason=)` | Make one; rename, cap (0: none), suspend and lift |
| `sub_accounts.keys(id)` · `sub_accounts.create_key(id, ...)` · `sub_accounts.revoke_key(id, key_id)` | Its own keys, for your customer |
| `as_sub_account(id)` · `MeshArc(key, tenant=id)` | A client whose every request acts on that sub-account |

## Connections and saved logins

**Not live on mesharc.dev yet:** everything in this section needs an API with these features, which are on the API's feature branch.

```python
pdl = arc.connections.create("pdl", name="Company data", secrets={"api_key": "..."})
print(arc.connections.test(pdl["id"]))   # {status: healthy | failing, error, detail}
print(arc.logins.list()["enabled"])      # saved logins: made in the app, from a live browser
```

`connections` is the workspace's connections — destinations, models and data providers: `providers()`, `list()`, `create(provider, name=, config=, secrets=)`, `get(id)`, `update(id, ...)`, `delete(id)` and `test(id)`. Secrets are sent and never returned. `logins` lists the workspace's saved logins (`list()`), and `update(id, name=, expired=)`, `test(id)` and `delete(id)` them; a login is saved in the app, from a live browser, and its session is never returned. Both need an admin key.

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
queue = arc.monitor()       # the job queue: what is queued and running (not arc.monitors)
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
| `expired` | 410 | An agent run past its keep date (7 days) |
| `rate_limited` | 429 | Over the key's rate limit; `X-RateLimit-Reset` says when |
| `internal` | 500 | Quote `request_id` to support |

`request_id` is the id the API put on the response and in its own logs, so a support conversation starts from one string.

Two more cases: a network failure or a request that hits `timeout` raises `MeshArcError` with `status == 0` and `code` `network` or `timeout`; a job the client stopped waiting for raises `MeshArcTimeoutError` — both a `MeshArcError` and a `TimeoutError` — which carries `job_id` so you can poll it later (`arc.get_crawl(id)`, `arc.batch(id)`, `arc.get_search(id)`, `arc.get_agent(id)`).

## Idempotency and timeouts

- `scrape`, `scrape_one`, `crawl`, `web_search`, `agent`, `AgentRun.continue_`, `AgentRun.follow_up`, `monitors.create`, `monitors.run`, `agent_batches.start` and `sub_accounts.create` take `idempotency_key=`: send the same key again within 24 hours and you get the first answer back rather than a second job. The MCP server's `search_web` tool sends none, on purpose: an equal search within the hour of a finished one comes from the cache anyway. Its `run_agent`, `continue_agent` and `create_monitor` tools send their own, made from their inputs and the ten-minute window, so a retry in the same ten minutes lands on the same run and a re-run later is a new one.
- Waiting calls take `poll=` (seconds between polls) and `timeout=` (seconds before `TimeoutError`), and most take `wait=`: `wait=False` returns the envelope at once. The defaults differ: `scrape`, `batch`, `crawl` (and `Crawl.wait`, `Crawl.pages`) and `runs.start` / `runs.wait` poll every 3 s for up to an hour; `scrape_one` and `web_search` every 2 s for up to ten minutes; `map` / `map_details` and `extract` every 2 s for up to five minutes; `AgentRun.wait` and `AgentRun.trace` every 2 s for up to an hour.
- `timeout_s` on a single scrape is how long the API itself holds the request open (60 s by default, 120 at most); a slower page comes back as an id and is polled.
- `MeshArc(..., timeout=150.0)` is the HTTP timeout per request. A request is retried on 429, 502, 503, 504 and network failures when it is safe to repeat — a GET, a DELETE, or a POST with an idempotency key — up to `max_retries` times (2), honouring `Retry-After`.

## Credits

Every response says what it cost: `credits` on a page, `creditsUsed` on a job envelope, `X-MeshArc-Credits` on the HTTP response. A page costs the engine that read it — a plain fetch 1, a render 4 — and a refused page or a 404 costs nothing. A web search's results page costs the engine that read it, and nothing when every engine refused; an equal search (same query, news or web, results page, country, lang, freshness and domains) within an hour of a finished one (ten minutes for news) comes from the cache with no charge for the results page, while the pages it scrapes are charged either way. An agent run pays for each page it reads as a read (a refused page is free) and for the model's tokens at the model provider's price plus 20% (1 credit per 1,000 tokens on the workspace's own LLM connection), within its `max_credits` (2,000 by default); `creditsUsed` counts both. A monitor's runs are charged as the searches and agent runs they are. The schedule and the plans are at [mesharc.dev/docs/billing](https://mesharc.dev/docs/billing).

## The MCP server

The package also ships MeshArc as an MCP server, so Claude Desktop, Claude Code, Cursor and any MCP client can scrape, crawl, map, search the web, hand a question to the agent and read change records as tools. Python 3.10+.

There is a hosted one, so most people need install nothing:

```bash
# Claude Code
claude mcp add --transport http mesharc https://mcp.mesharc.dev/mcp
```

It opens a browser once to sign in to mesharc.dev and approve, then works. Approving issues the app an API key of its own, which you can see and revoke under Settings → API keys; read only unless you allow changes. Any client that takes a remote MCP URL — the Claude.ai and ChatGPT connectors, Claude mobile, Cursor — takes it the same way.

Or run it yourself, over stdio, with your own key:

```bash
pip install "mesharc[mcp]"
MESHARC_API_KEY=mesharc_... mesharc-mcp          # serves over stdio

# Claude Code
claude mcp add mesharc -e MESHARC_API_KEY=mesharc_... -- mesharc-mcp
```

Tools: `scrape_urls`, `map_site`, `crawl_site`, `search_web`, `list_projects`, `get_project`, `create_project`, `update_project`, `delete_project`, `start_run`, `list_runs`, `list_pages`, `get_changes`, `get_job`, `cancel_job`, `run_agent`, `continue_agent`, `create_monitor`, `list_monitors`, `pause_monitor` and `get_monitor_rows` (the last six not live on mesharc.dev yet: they need the API's agent and monitor features, on its feature branch). `create_monitor` keeps a web search or an agent question on a schedule, each run compared with the last; `list_monitors` shows them and their runs, `pause_monitor` pauses and resumes one, and `get_monitor_rows` reads the records an agent monitor follows. `create_project` takes `max_credits_per_run` and `max_credits_per_month`. `get_project` shows a project's webhook and its recent deliveries, and `update_project` sets it and sends it a test (`test_webhook=true`). Every tool is a call through this client, and each one's answer is described by an output schema. A tool that fails answers `{error, status}`, with the API's `code` and `request_id` as well when it gave them.

A result with many pages in it is a map, not the territory: a crawl answers with an index of the pages read (up to 500, fewer if the budget needs the room), an excerpt of as many as a 60,000-character budget pays for (fifty at most), and a count of each page's links rather than the links. Ask for the one page you want on its own with `get_job(kind="crawl", id=…, url=…)`, or carry on through the crawl with `cursor=`. A multi-URL scrape past its budget counts the rest by status and names any that did not come back ok. One page asked for on its own -- `list_pages` with `url`, or `scrape_urls` with a single url (with `full=true` for every format) -- comes back whole, capped at 12,000 characters. A `search_web` answer keeps within the same 60,000 characters, a scraped result's page as an excerpt; `scrape_urls` with `full=true` reads one in full. So does an agent's answer: past the budget its `fieldSources` (where each value of the answer came from) are left out first, then its sources are cut, to the first 50 (fewer if they would take more than half the room), and only then is `data` replaced by its JSON text, cut, with a note saying where the full answer is -- `GET /api/v1/agent/{id}`, or `get_agent(id)` in this client.

Hosted, a connection can be approved read-only, and read-only cuts in a place worth knowing: it reads the whole workspace, but it cannot reach the site. Anything that fetches is a write, because it leaves the workspace and usually spends its credits -- `map_site` reads only a site's sitemaps, at a credit per sitemap file, and it is gated with the rest. `list_projects`, `get_project`, `list_runs`, `list_pages`, `get_changes`, `get_job`, `list_monitors` and `get_monitor_rows` work; the thirteen that fetch or write -- `scrape_urls`, `map_site`, `crawl_site`, `search_web`, `run_agent`, `continue_agent`, `create_monitor`, `pause_monitor`, `create_project`, `update_project`, `delete_project`, `start_run`, `cancel_job` -- answer `{"code": "read_only"}` with what to do about it, until the app is reconnected with 'Also allow changes' ticked (write access). `delete_project` needs more than write: an admin API key, which a connected app never holds, so it only works run locally with such a key.

Hosted, a tool that would hold a connection open for minutes — a crawl, a run you asked to wait for, a multi-URL scrape, a web search — hands back a job after `MESHARC_MCP_WAIT` seconds (25 by default, because many MCP hosts time a tool call out sooner). The work carries on server-side; `get_job` picks it up and `cancel_job` stops a crawl, run, batch or agent run. A search that scrapes, with its results in and its pages still landing, answers with the results and a job for the pages. Run locally, those tools block as they always have.

`run_agent` keeps an agent given starting `urls` on those URLs' sites unless `allowed_domains` says otherwise, as the app does: a page it reads could tell it to go elsewhere. `allowed_domains=[]` lets it read any site, and with no `urls` there is nothing to hold it to. (The client's `agent()` sends what it is given, as the API takes it: no `allowed_domains` is no limit.)

`run_agent` does not wait at all, hosted or local: an agent works for minutes, so it hands back a job at once, with what the run has spent so far. `get_job(kind="agent", id=…)` follows it -- every 30 to 60 seconds -- and answers with the result once it finishes; `cancel_job(kind="agent", id=…)` stops it before its next step, and what it used stays charged. A run is kept 7 days; after that `get_job` answers `status: "expired"`. A retry that lands on a run already finished answers with its result. A run that stopped at its budget (`status: "credit_limit"`, what it had found under `data.partial`) is carried on by `continue_agent(id=…, max_credits=…)`: a new run on the same thread that resumes where it stopped and keeps the pages it read, not paid for again. It hands back the new run's job the same way. A run that did not stop at its limit is refused with code `conflict`; asked again for a run already carried on — a retry past the ten-minute key window included — it answers with the run that carried it on, and starts nothing.

### Hosting it yourself

```bash
MESHARC_MCP_PUBLIC_URL=https://mcp.example.dev/mcp MESHARC_OAUTH_ISSUER=https://api.example.dev MESHARC_INTROSPECT_SECRET=...                      mesharc-mcp --http --host 127.0.0.1 --port 8040
```

The server holds no key. Each request carries the caller's OAuth token, verified against `MESHARC_OAUTH_ISSUER` and then used to make the call, so one process serves many workspaces. `MESHARC_API_KEY` is never read in this mode; if it is set, startup says it is being ignored.

Behind a reverse proxy, note that binding `127.0.0.1` makes the MCP SDK enable DNS-rebinding protection with a localhost-only `Host` allow-list. The server therefore passes its own, built from `MESHARC_MCP_PUBLIC_URL` — without which every proxied request would be refused. Add browser-based clients to the `Origin` allow-list with `MESHARC_MCP_ALLOWED_ORIGINS` (comma-separated).

## Privacy and security

The client talks to one host — the API base URL, `https://api.mesharc.dev` unless `MESHARC_API_URL` or `base_url=` says otherwise — and to nothing else. The key travels only as a bearer header, only over HTTPS. Nothing is written to disk and no telemetry is sent. The client reads only `MESHARC_API_KEY` and `MESHARC_API_URL`; the MCP server in HTTP mode reads `MESHARC_MCP_PUBLIC_URL`, `MESHARC_OAUTH_ISSUER`, `MESHARC_INTROSPECT_SECRET`, `MESHARC_MCP_WAIT` and `MESHARC_MCP_ALLOWED_ORIGINS`, and no key of its own.

What MeshArc keeps about you and about the pages you crawl, and for how long, is in the [privacy policy](https://mesharc.dev/legal/privacy). How the service is secured is on the [security page](https://mesharc.dev/legal/security). To report a vulnerability in this client or in the service, write to security@mesharc.dev rather than opening a public issue — see [SECURITY.md](https://github.com/mesharc-org/mesharc-python/blob/main/SECURITY.md).

## Anything else

The client is a thin wrapper: every method is one API call and returns the API's JSON as a `dict`. The full reference is at [mesharc.dev/docs/api](https://mesharc.dev/docs/api). Call `arc.close()` when you are done, or use the client as a context manager.

- Documentation: [mesharc.dev/docs](https://mesharc.dev/docs)
- Node client: `npm install mesharc` — [mesharc-node](https://github.com/mesharc-org/mesharc-node)
- Issues and pull requests: [mesharc-python](https://github.com/mesharc-org/mesharc-python)
- Questions: hello@mesharc.dev

MIT.
