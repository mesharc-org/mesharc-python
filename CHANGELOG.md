# Changelog

All notable changes to this package are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org).

## Unreleased

### Changed

- Requires Python 3.10 or newer. Python 3.9 reached its end of life in October 2025.

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
