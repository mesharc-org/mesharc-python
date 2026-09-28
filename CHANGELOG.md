# Changelog

All notable changes to this package are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org).

## Unreleased

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
