# Changelog

All notable changes to this package are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org).

## Unreleased

### Added

- **The client hands a question to the agent, which searches, reads pages and answers on its own.** `agent(prompt, urls=, schema=, max_credits=, max_steps=, allowed_domains=, timeout_s=0, idempotency_key=)` returns an `AgentRun` at once; `timeout_s` asks the API to hold the request for the answer, 120 s at most. `AgentRun.wait(poll=2.0, timeout=3600)` returns the envelope once the run is `done`, `cancelled` or `credit_limit` (what it had found under `data.partial`) and raises `MeshArcError` for `error`; `refresh()` reads it as it stands, `cancel()` stops it (`cancelled` when queued, `cancelling` when running, which stops before its next step), and `trace(after=0, follow=True)` yields what the agent does, step by step. `data` is JSON matching the schema, or `{"text": ...}` without one, and `sources` the pages it rests on. `get_agent(id)` reopens a run, and raises `MeshArcError` 410 with code `expired` once it is past its 7 days; `agent_runs(status=None, limit=25)` walks the workspace's runs, following `next`. It needs a key that can write, and spends credits: pages as they are read (a refused page is free) and the model's tokens at the model provider's price plus 20%, within `max_credits` (2,000 by default). **This needs an API that has the `/agent` route**, which is not live on mesharc.dev yet.
- **An agent run can report to a webhook and run on the workspace's own model.** `agent()` takes `webhook=` (a URL, or `{url, events, metadata}`, told about `agent.started`, `agent.action`, `agent.completed`, `agent.failed` and `agent.cancelled`) and `connection_id=` (one of the workspace's LLM connections, whose tokens then cost 1 credit per 1,000). `AgentRun.webhook_secret` is the secret the messages are signed with, returned once on the POST and kept by the handle past `refresh()`, and by the handle `continue_()` returns; `""` without a webhook. **These need an API with the agent's webhook and connection features**, which are on the API's feature branch, not yet on mesharc.dev.
- **`AgentRun.field_sources` says where each value of the answer came from**: its path (`"plans[0].price"`, `"[2].name"` for a list answer) to `{url, pageId}`, a page the run read; `{}` when the API said nothing. **This needs an API with field sources**, on the API's feature branch, not yet on mesharc.dev.
- **`AgentRun.continue_(max_credits=None, max_steps=None, timeout_s=0, idempotency_key=None)` carries on a run that stopped at `credit_limit`**, with a new budget. It returns an `AgentRun` on the new run, which resumes from where the stopped one was, keeps the pages it read (not paid for again) and its webhook, and answers in full. The API carries the webhook's secret over without repeating it, so the new handle's `webhook_secret` is the stopped handle's. `continues_run_id` and `continued_by` link the two runs. A run that did not stop at its limit, was already continued or has no saved progress raises `MeshArcError` 409 `conflict`; one past its keep date, 410 `expired`. **This needs an API that has the `/agent/{id}/continue` route**, on the API's feature branch, not yet on mesharc.dev.
- `agent_runs()` also filters by `model=` and by `since=` / `until=` (an ISO 8601 string, or a `date` / `datetime`, sent as ISO 8601), sent on every page like `status`; an empty string sends no filter. A date means its midnight UTC; a date-time without a zone is read as UTC, so a naive `datetime.now()` is local time read as UTC.
- **`web_search(news=True)` searches the engines' news results**, each hit with `publisher` and `age`; `page=` (1 to 10) reads results further in, page 2 being results 11-20. Both come last in the signature, after `idempotency_key`, so no existing call changes meaning. An equal news search comes from the cache for ten minutes, not an hour. **These need an API with news and results-page search**, on the API's feature branch, not yet on mesharc.dev.
- **Monitors: a search or an agent request, run again on a schedule and compared with the last run that answered**, under `arc.monitors` as projects are under `arc.projects`:

  | Method | What it does |
  |---|---|
  | `monitors.create(kind, request, schedule, name=None, webhook=None, baseline_id=None, idempotency_key=None)` | Keep a search or agent request on a schedule |
  | `monitors.list(kind=None)` · `monitors.get(id)` | The workspace's monitors, each with its `lastRun`; one monitor |
  | `monitors.update(id, name=None, schedule=None, status=None, webhook=None)` | Sends only what is not None; `webhook=""` removes the webhook |
  | `monitors.pause(id)` · `monitors.resume(id)` | Stop and restart the schedule |
  | `monitors.run(id, idempotency_key=None)` | Run it now; 409 `conflict` while a run is under way |
  | `monitors.runs(id, limit=25)` | Its runs, with what changed, following `next` |
  | `monitors.delete(id)` | The monitor and its runs |

  Each returns the API's JSON. `request` is a POST /search or POST /agent body in the API's own (camelCase) names, so a news monitor says `"sources": ["news"]` (`web_search(news=True)` maps to that; `monitors.create` does not); a monitored search reads its results page only, so `scrape` is refused; a search baseline's `limit` must be at least the monitor's. The webhook hears `search.changed` / `agent.changed`, and its secret comes back once as `webhookSecret`. Creating and running a monitor spend credits, and every call but the three reads needs a key that can write. `arc.monitor()`, the workspace's job queue, is unchanged. **This needs an API that has the `/monitors` route**, which is on the API's feature branch and not yet on mesharc.dev.
- **`run_agent`, a sixteenth MCP tool, so an assistant can hand a research question to the agent.** It needs write access; a read-only connection is answered `{"code": "read_only"}`. It returns at once with a job, hosted or local, and a retry that lands on a finished run gets its result; `get_job` and `cancel_job` take `kind="agent"` to follow and stop it, and a run past its 7 days answers `status: "expired"`. A finished answer carries `fieldSources`, where each value of `data` came from, and keeps within 60,000 characters: `fieldSources` is left out first, then its sources are cut to the first 50, then `data` to its JSON text, with a note saying where the full answer is. The model's tokens are charged at the model provider's price plus 20%. It sends its own idempotency key, made from its inputs and the ten-minute window, so a retry in the same ten minutes lands on the same run; a 409 saying that key is still being accepted (code `in_flight`, or an older API's message naming the Idempotency-Key) is answered as running, and any other 409 as an error. An output schema describes its answer. **This needs an API that has the `/agent` route**, which is not live on mesharc.dev yet.
- **`continue_agent`, a seventeenth MCP tool, carries on an agent run that stopped at its credit limit.** `continue_agent(id, max_credits=None, max_steps=None)` starts a new run on the same thread, which resumes where the stopped one was and keeps the pages it read (not paid for again); left out, the budget and steps are the stopped run's. It needs write access; a read-only connection is answered `{"code": "read_only"}`. It returns at once with the new run's job, as `run_agent` does, for `get_job` and `cancel_job` with `kind="agent"`, and sends its own idempotency key, made from its inputs and the ten-minute window. A run already carried on (a retry past that window included) is answered with the run that carried it on, and nothing new is started. A run that did not stop at its limit is answered with code `conflict`, an unknown one `not_found`, one past its 7 days `expired`. A `credit_limit` answer's note now points at `continue_agent` with the run's id, or at the run that already carried it on. **This needs an API that has the `/agent/{id}/continue` route**, which is not live on mesharc.dev yet.

## 0.6.0 - 2026-10-07

Two MCP tool names change, so **this breaks anything that calls `web_search` or `get_page` by name**; both still do what they did, under the new names below. The Python client's API is unchanged apart from two additions: `web_search()` keeps its name there.

### Changed

- **`web_search` is now `search_web`**, a verb first like every other tool (`get_page`, `list_runs`, `start_run`). Glama's grader marked the set down for the one name that was not.
- **`get_page` is folded into `list_pages`**: `list_pages(project_id, url=...)` reads one stored page in full, with its versions, exactly as `get_page` did; without `url` it lists, and with `q` it searches. Giving `url` and `q` together is refused. That keeps the server at fifteen tools, inside the band Glama scores in full, and removes a pair it named as easy to confuse.
- `update_project` is no longer marked idempotent or closed-world: with `test_webhook=true` it sends a message to the project's webhook endpoint, and a repeat sends another.

### Added

- **Webhooks, managed through the project tools.** `get_project` shows a project's webhook URL, its events, and the last ten deliveries with their status (queued, retrying, delivered or failed), attempts and last error, without the payload or the signing secret. It reads them from the deliveries list, which writes nothing. `update_project` already set `webhook_url` and `webhook_events` through `config`; its description now says so, and `test_webhook=true` queues a test `run.finished` message after any changes. A refused test (no URL saved) is reported under `webhookTest` and does not undo the change.
- **Any two runs compared.** `get_changes(project_id, run_id, against=...)` compares a run with any other run of the project, "what changed since last month", where it could only compare a run with the one just before. The API already offered it (`?against=`); the client's `changes()` takes `against=` too. Nothing is stored.
- **The credit balance before a large crawl.** `list_projects` answers with `workspace: {plan, creditsLeft, creditsSpentThisMonth, oneTimeAllowance}` beside the projects, read from `GET /me/billing`, so an assistant can check before it spends rather than learn it from a 402. A balance that cannot be read is a note, never a failed call.
- **`MeshArc.webhook_deliveries(project_id, limit=50)`**, **`MeshArc.test_webhook(project_id)`** and **`MeshArc.billing()`** in the client, over `GET /webhooks/deliveries`, `POST /projects/{id}/webhooks/test` and `GET /me/billing`.

## 0.5.0 - 2026-10-07

### Added

- **The client searches the web, for pages whose URLs you do not know.** `web_search(query, limit=, country=, lang=, freshness=, include_domains=, exclude_domains=, scrape=, ...)` returns the whole envelope, the hits under `data`; `scrape=True` also fetches each hit as markdown. `get_search(id)` reopens a search started earlier, and `searches(q=None, limit=25)` walks the workspace's searches, following `next`. A search is `queued`, `running`, `done`, `blocked` or `error`: `blocked` (every engine refused) is returned, `error` raises `MeshArcError`. It needs a key that can write, and spends credits: a results page every engine refused is free, an equal search (same query, `country`, `lang`, `freshness` and domains) within an hour of a finished one comes from the cache with no results-page charge, and scraped pages are always charged. `search()` is unchanged and is still the project search. **This needs an API that has the `/search` route**, which is live on mesharc.dev.
- **`web_search`, a sixteenth MCP tool, so an assistant can find pages before it reads them.** It needs write access; a read-only connection is answered `{"code": "read_only"}`. Its answer keeps within 60,000 characters, a scraped result's page as an excerpt, with `scrape_urls(full=true)` to read one in full, and an output schema describes it. Hosted, a search still going when `MESHARC_MCP_WAIT` runs out comes back as a job, and a scraping search whose pages are still landing returns its results with a job for the pages. It sends no idempotency key, by design: the API's one-hour cache already answers an equal search.
- `get_job` takes `kind="search"`, for a search `web_search` handed back. `cancel_job` does not: the API has no way to stop a search.

### Changed

- **MCP error answers carry the API's `code` and `request_id`**, alongside `error` and `status`, so an assistant can tell one refusal from another and a support conversation can start from the request id. Both were dropped before; each is included when the API gave it.
- The read-only answer names the box to tick on reconnecting, 'Also allow changes', and says "search inside a project" where it lists what a read-only connection can still do -- searching the web is not among them.

### Fixed

- **The settings guide (`get_project` with no `project_id`) described two settings in shapes the API refuses**, so an assistant following it wrote configs that failed. `json_schema` is a list of `{name, type, required}` fields, not a JSON schema. `llm_extract` is an object with a `connection_id` (or `null` for off); `true` was refused by the API. `formats` now lists `raw`.

## 0.4.0 - 2026-10-07

The MCP server goes from nineteen tools to fifteen. **This breaks anything that calls the five removed tool names**; each one's call is still there, as an argument of the sibling it overlapped. The Python client's API is unchanged.

### Changed

- **Five tools are folded into their siblings.** Each still makes the same API call as before:

  | Was | Now |
  |---|---|
  | `extract_url(url, config)` | `scrape_urls([url], config, full=true)` |
  | `keep_crawl_as_project(crawl_id, name, schedule)` | `create_project(crawl_id=..., name, schedule)` |
  | `describe_project_config()` | `get_project()` with no `project_id` |
  | `recrawl_pages(project_id, urls)` | `start_run(project_id, urls=[...])`, which can now also `wait` |
  | `search_pages(project_id, q, mode, run_id)` | `list_pages(project_id, run_id, q=..., mode=...)` |

  Why: assistants confused `scrape_urls` with `extract_url` for a single URL. Glama's grader also marked the set down for its size, scoring 19 tools as "slightly heavy" against its 3 to 15 band.
- **A mistake an assistant can now make is refused with a reason, not guessed at.** Examples: both `seed` and `crawl_id`, `config` with a kept crawl, and `full=true` with more than one URL.
- **`list_projects` answers `{projects: [...]}`** instead of a bare list, so its answer can carry an output schema.
- Every description was rewritten. Each one now:
  - says where each input comes from, with an example;
  - says what the call costs;
  - names its refusals.

  Text that is now in the output schema was taken out.

### Added

- **Output schemas on every tool.** Each describes the fields of its answer, including the error fields.
  - They describe the answer without filtering it: every field is optional, and a field the API adds later comes through as it is. No answer can fail validation, including an error.
  - Clients that read `structuredContent` get the same JSON as the text block.
- **`delete_project(project_id, confirm_name)`.** It deletes a project and everything its runs stored.
  - `confirm_name` must repeat the project's name exactly, or nothing is deleted.
  - The API allows it only for an admin key. OAuth connections hold read and write, never admin, so the hosted server refuses it with `code: admin_only` and says where to delete instead. It only works when the server runs locally with an admin API key.

## 0.3.4 - 2026-10-07

### Added

- **`cancel_job(kind, id, project_id=None)`**, an eighteenth MCP tool: it stops a crawl, run or batch that is still going. It's the counterpart to `get_job`.
  - Until now an assistant had no way out of a job that should not finish. `start_run` and `recrawl_pages` answer 409 while a run is going, and nothing could stop that run.
  - It uses the API's existing cancels: `POST /projects/{id}/runs/{run_id}/cancel`, `DELETE /crawl/{id}` and `DELETE /scrape/{id}`.
  - Queued work is dropped at once, and a page being read finishes first. Pages already read stay readable, and nothing is deleted.
  - A job that already finished is left as it is and reported with its status, so asking twice is harmless.
  - It needs write access and is marked destructive (a stop cannot be resumed).
- **`list_runs(project_id, limit=25)`**, a nineteenth MCP tool. It lists a project's runs newest first, so an assistant can find a run id for `list_pages`, `get_page`, `get_changes` or `search_pages`, see whether a run is still going before `start_run`, or pick the one to stop. Until now only `get_project`'s last run and `start_run`'s answer gave a run id. It's read-only, over the existing `GET /projects/{id}/runs`, and keeps only the fields that pick a run (no config, link graph or engine profile).
- **`MeshArc.cancel_batch(batch_id)`**: stops a scrape batch, or a one-URL scrape still going. It's the client's counterpart to `Crawl.cancel()` and `runs.cancel()`.

### Changed

- `start_run`, `recrawl_pages` and `get_job` say that `cancel_job` stops a running job; `cancel_job` and the `run_id` parameters point to `list_runs`. The server's instructions and the README name them too.

## 0.3.3 - 2026-10-07

### Changed

- **Tool descriptions are written in short lines, one fact each.** Glama's grader scored 0.3.2's tools A (4.6/5). The points it took off were for clauses chained by semicolons, and for descriptions that added nothing about the inputs beyond the schema. Each description now keeps its purpose and when-to-use lines, and adds:
  - **Inputs:** how the inputs work together. Examples: `formats` only picks which stored bodies come back; path globs match the path, never the host; a `get_page` url must match apart from scheme and trailing slash; `recrawl_pages` resolves '/pricing' against the project's site.
  - **Access:** whether a read-only connection may call it, and that errors come back as `{error, status}`.
  - **Returns and limits:** the answer's shape and its limits. Examples: `list_pages` returns up to 500 rows, shallowest first; `list_projects` returns everything, oldest first; `get_changes` answers 409 for a run still going; `get_job` answers 404 once a crawl has expired.
- Apart from the fix below, behaviour is unchanged. Only the text an assistant reads differs.

### Fixed

- `list_projects` returns each project's `status` (draft, crawling, healthy or failing). It asked the API for `health`, which no project carries, so every row said `health: null`.

## 0.3.2 - 2026-10-06

### Changed

- **Every MCP tool now says what an assistant needs to choose and call it.** An assistant picks a tool from its definition alone, and no parameter of the seventeen tools had a description; directories that grade definitions (Glama's Tool Definition Quality Score) graded the tools C. Each tool now has:
  - a title;
  - a description of what it does, when to use it, and which sibling to use instead;
  - what it costs in credits and what comes back;
  - its refusals (404, 409 while a run is going, the plan's project limit).
  
  Every parameter is described: where an id comes from, what leaving it empty means, the valid range.
- **Each tool declares the MCP hints** `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`, so a client can tell the eight tools that only read what the workspace stored from the nine that reach a site or change the workspace. `update_project` is the only one marked destructive, because it replaces values.
- **Fixed choices are enums in the schema**: `get_job`'s `kind` (crawl, run, batch), `search_pages`'s `mode` (content, selector), and `schedule` (manual, hourly, daily, weekly). Behaviour is otherwise unchanged. A value outside these is now refused by the server before the call, where it used to be refused by the API (or, for `kind`, by `get_job` itself).

## 0.3.1 - 2026-10-06

### Fixed

- **A multi-page result is no longer unloadable.** A finished 200-page crawl came back as 2.2 million characters: every outbound link of every page was passed through untouched -- 1.19 MB of it, more than all the markdown put together -- and the 12,000-character body cap applied to each of the fifty pages returned. Clients refused to load it, and no context window would have held it. A crawl or multi-URL scrape now answers with an index of the pages read (`url`, `title`, `words`, `status`, up to 500 rows; hosted, the walk also stops when `MESHARC_MCP_WAIT` runs out), an excerpt of the first fifty, and a link *count* in place of each link list. The whole answer is budgeted at 60,000 characters rather than each page at 12,000, and the budget is kept: the index is paid for first, then as many excerpts as the rest will pay for above a 600-character floor -- fewer excerpts as a crawl grows, not thinner ones, which is what let a 500-page crawl out at 101,399 characters in an earlier draft of this release.
- `crawl_site` waits for the crawl to finish before walking it. Walked while it ran, a short first batch put the cursor past rows the walk had not read.
- A multi-URL scrape past its budget no longer drops urls without a word: the index lists what fits, and `rest` counts the others by status and names those that did not come back ok. It is weighed like a crawl, with its per-url run list summarised as a count by status, so it keeps to 60,000 characters too.
- Single-page tools are unchanged and are how you read anything in full: `extract_url`, `get_page`, and `scrape_urls` with one URL still return the whole page at the 12,000-character cap.
- A project's webhook signing secret no longer reaches an assistant. The API returns `webhookSecret` with a project, which is right for a program that will verify signatures with it, and wrong for a tool answer that passes through an AI app into a model's context. It is dropped in the one place every tool's answer goes through, replaced by `webhookSecretNote` saying where to see it, so a tool added later cannot leak it either. The API and the SDK still return it.

### Added

- `get_job(kind, id, project_id=None, url=None, cursor=None)`. `url` returns one page of a crawl on its own, each body up to 12,000 characters -- readable while the crawl is still running, because a page that has been crawled is stored -- and drops its link list. `cursor`, from the crawl result, returns the next window of pages.
- `Crawl.page(url)` (`GET /crawl/{id}/page`), the API route behind that. The URL matches under either scheme and with or without a trailing slash. **This needs an API that has the route**: 0.3.1 requires the Seam-be deploy that added it.
- `Crawl.pages(..., cursor=...)` resumes a page walk from where an earlier one stopped instead of starting at the top.

### Changed

- `describe_project_config` lists `max_tier: 'auto'` (as high as the plan allows), which the API accepts and the guide left out -- so an assistant reading the guide would never set it.
- A tool refused for want of scope now says so. Hosted, a read-only connection asking for anything that fetches or writes got the API's `this needs the member role` -- true, and nothing an assistant can act on, since it does not know what a role is or that the person who approved the connection chose it. It now answers `{"code": "read_only"}` explaining that read-only covers what the workspace has stored but not fetching a new page -- those tools reach the site or alter the workspace, and most of them spend credits -- and that reconnecting with write access is the fix. The API's own words are kept under `detail`, the other 403s (suspended, email_unverified, mfa_required) are untouched, and stdio passes the API's answer through because it cannot see its key's scopes.

## 0.3.0 - 2026-09-30

### Added

- **The MCP server speaks HTTP, with OAuth.** `mesharc-mcp --http --host H --port P` serves streamable HTTP as an OAuth 2.1 resource server, so a client can be added by URL instead of installed. Stdio stays the default and is unchanged: `mesharc-mcp` on its own behaves exactly as before.
- In HTTP mode every request acts as the caller who sent it. The server holds no key of its own, verifies each bearer token against the API's `/oauth/introspect`, and **never** reads `MESHARC_API_KEY` -- it refuses to build a client without a caller's token, because `MeshArc()` would otherwise fall back to that variable and every caller would act inside the operator's workspace. If the variable is set, startup says it is being ignored.
- `get_job(kind, id, project_id=None)`, a seventeenth tool, to follow a crawl, run or batch a long tool handed back.
- `MESHARC_MCP_WAIT` (default 25s) bounds how long a long tool holds a connection in HTTP mode. `crawl_site`, `start_run(wait=true)`, a multi-URL `scrape_urls` and `keep_crawl_as_project` return `{status: "running", job: {...}}` when the budget runs out, and the job goes on server-side. Stdio keeps blocking, where the caller is a local process that asked for the answer.
- `MESHARC_MCP_ALLOWED_ORIGINS` adds browser-based MCP clients to the Origin allow-list by configuration.

### Changed

- The `mcp` extra pins `mcp>=2.2,<3`. 2.2 is where the resource-server APIs this uses settled, and `validate_token_resource` is set explicitly rather than left to change default in 3.0.
- Idempotency keys are scoped to the OAuth grant in HTTP mode, not to the server process. Two workspaces asking for the same URL are two jobs, and a restart no longer makes a new prefix.

### Fixed

- HTTP mode passes its own `transport_security`. Binding `127.0.0.1` makes the SDK enable DNS-rebinding protection with a localhost-only `Host` allow-list, so behind a reverse proxy -- where the `Host` is the public domain -- every request would have been refused. Origins are allow-listed for the same reason: a present `Origin` that is not listed is refused, which a browser-based client would have hit and a server-to-server one would not.
- HTTP mode checks the introspection secret at startup and exits naming it. A wrong secret makes every token look invalid, so the service would otherwise have started clean and then refused everything with nothing pointing at the cause.

## 0.2.0 - 2026-09-28

### Added

- The client paces itself against the API key's rate limit. When a response's `X-RateLimit-Remaining` header shows the key is almost out of requests, the next request and the waiting loops (`wait=True`) hold until the window resets instead of being refused.
- MCP server: `describe_project_config` (every project setting, its meaning and default), `get_project` and `update_project`.

### Changed

- Requires Python 3.10 or newer. Python 3.9 reached its end of life in October 2025.
- MCP server: one API client for the server's lifetime, so the rate-limit window carries across tool calls.
- MCP server: `scrape_urls` and `crawl_site` send an idempotency key, so a retried tool call reuses the same job instead of starting another.
- MCP server: `scrape_urls` and `extract_url` read PDFs, Word files and spreadsheets unless `parse_documents` is false.
- MCP server: clearer descriptions of `include_paths` and `exclude_paths`, and of what `config` can hold.

### Fixed

- The `mcp` extra requires `mcp` 2.0 or newer. The MCP server uses the `MCPServer` class that `mcp` 2.0 introduced, so with an older `mcp` installed it could not start.

## 0.1.3

- `SECURITY.md`: how to report a vulnerability, and what the client does with your data.
- README section on privacy and security; Privacy and Security links on the PyPI page.
- `LICENSE`, `SECURITY.md` and `CHANGELOG.md` ship in the source distribution.

## 0.1.2

- The MCP server ships in the package: `pip install "mesharc[mcp]"`, then `mesharc-mcp`.
- Requests time out (150 s by default; `timeout=`) and are retried on 429, 502, 503, 504 and network failures when safe to repeat (`max_retries=`, default 2), honouring `Retry-After`.
- `MeshArcTimeoutError` for a job the client stopped waiting for; it carries `job_id` and is both a `MeshArcError` and a `TimeoutError`.
- Network and timeout failures raise `MeshArcError` with status 0 and code `network` / `timeout`.
- Type hints on the public API (the package is `py.typed`).
- `export` raises the API's error, with its code and request id, when the export is refused.

## 0.1.1

- The README, reorganised; the client needs only an API key.

## 0.1.0

- First release.
