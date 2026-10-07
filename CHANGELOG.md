# Changelog

All notable changes to this package are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org).

## Unreleased

## 0.3.3 - unreleased

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
