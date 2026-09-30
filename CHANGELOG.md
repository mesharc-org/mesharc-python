# Changelog

All notable changes to this package are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org).

## Unreleased

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
