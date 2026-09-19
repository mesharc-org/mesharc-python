"""mesharc: the Python client for the MeshArc API.

    from mesharc import MeshArc
    arc = MeshArc("mesharc_...")                # or MeshArc() with MESHARC_API_KEY set

    page = arc.scrape("https://example.com/pricing")            # one URL, waited for
    batch = arc.scrape(["https://a.com/", "https://b.com/x"])   # many URLs, one row each

    job = arc.crawl("https://docs.example.com", limit=200)      # a site, no project needed
    for page in job.pages():                                    # pages as they land
        print(page["url"], page["words"])
    project = job.keep(name="Docs", schedule="weekly")          # keep it, if it is worth watching

    for entry in arc.map("https://docs.example.com"):           # what the site declares
        print(entry["url"], entry["lastmod"])

Every method is one API call (or a poll loop where ``wait`` applies) and
returns the API's JSON unwrapped, so the API reference at
https://mesharc.dev/docs/api applies to every return value. Refused
requests raise ``MeshArcError``; a job the client stopped waiting for
raises ``MeshArcTimeoutError``.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union
from urllib.parse import parse_qs, urlparse

import httpx

__version__ = "0.1.2"
__all__ = ["MeshArc", "MeshArcError", "MeshArcTimeoutError", "Crawl"]

DEFAULT_BASE = "https://api.mesharc.dev"
DEFAULT_TIMEOUT = 150.0
DEFAULT_MAX_RETRIES = 2
RETRY_STATUSES = frozenset({429, 502, 503, 504})

Json = Dict[str, Any]


class MeshArcError(Exception):
    """A request the API refused, or a job that ended in error.

    ``status`` is the HTTP status (0 for a network failure or a timeout),
    ``code`` the machine-readable reason (validation, unauthorized,
    plan_limit, not_found, rate_limited, ...) and ``request_id`` the id
    the API logged the request under -- quote it to support.
    """

    def __init__(self, status: int, detail: Any, code: str = "", request_id: str = "") -> None:
        super().__init__(f"{status}: {detail}" + (f" [{request_id}]" if request_id else ""))
        self.status = status
        self.detail = detail
        self.code = code
        self.request_id = request_id


class MeshArcTimeoutError(MeshArcError, TimeoutError):
    """A job that was still running when the client stopped waiting.

    ``job_id`` names the job; poll it later with the matching ``get``
    method. Catchable as either ``MeshArcError`` or ``TimeoutError``.
    """

    def __init__(self, message: str, job_id: str = "") -> None:
        super().__init__(0, message, "timeout")
        self.job_id = job_id


def _running(status: Any) -> bool:
    return status in ("queued", "running")


def _cursor_of(next_url: str) -> str:
    return (parse_qs(urlparse(next_url).query).get("cursor") or [""])[0]


class _Http:
    """The transport: one httpx client, bearer auth, JSON errors, retries."""

    def __init__(self, api_key: str, base_url: str, timeout: float, max_retries: int) -> None:
        self._c = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": f"mesharc-python/{__version__}"},
            timeout=timeout,
        )
        self._max_retries = max(0, int(max_retries))

    def __call__(self, method: str, path: str, idempotency_key: Optional[str] = None, **kw: Any) -> Any:
        if idempotency_key:
            kw.setdefault("headers", {})["Idempotency-Key"] = str(idempotency_key)
        # A GET or DELETE is safe to repeat; a POST only when it carries an idempotency key.
        repeatable = method in ("GET", "DELETE") or bool(idempotency_key)
        attempt = 0
        while True:
            try:
                r = self._c.request(method, path, **kw)
            except httpx.TimeoutException as exc:
                if repeatable and attempt < self._max_retries:
                    attempt += 1
                    time.sleep(self._backoff(attempt))
                    continue
                raise MeshArcError(0, f"request timed out: {exc}", "timeout") from exc
            except httpx.HTTPError as exc:
                if repeatable and attempt < self._max_retries:
                    attempt += 1
                    time.sleep(self._backoff(attempt))
                    continue
                raise MeshArcError(0, f"network error: {exc}", "network") from exc
            if r.status_code in RETRY_STATUSES and repeatable and attempt < self._max_retries:
                attempt += 1
                time.sleep(self._retry_after(r) or self._backoff(attempt))
                continue
            if r.status_code >= 400:
                raise self._error(r)
            if r.status_code == 204 or not r.content:
                return None
            return r.json()

    @staticmethod
    def _backoff(attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1)) + random.random() * 0.25

    @staticmethod
    def _retry_after(r: httpx.Response) -> Optional[float]:
        header = r.headers.get("Retry-After")
        if not header:
            return None
        try:
            return max(0.0, float(header))
        except ValueError:
            return None

    @staticmethod
    def _error(r: httpx.Response) -> MeshArcError:
        code, request_id = "", r.headers.get("X-Request-Id", "")
        try:
            body = r.json()
            detail = body.get("error") or body.get("detail") or r.text[:200]
            code = body.get("code", "") or ""
            request_id = body.get("request_id") or request_id
        except Exception:  # noqa: BLE001 - a non-JSON body is its own detail
            detail = r.text[:200]
        return MeshArcError(r.status_code, detail, code, request_id)

    def stream(self, method: str, path: str, **kw: Any) -> Any:
        return self._c.stream(method, path, **kw)

    def close(self) -> None:
        self._c.close()


class _Projects:
    def __init__(self, http: _Http) -> None:
        self._h = http

    def list(self) -> List[Json]:
        return self._h("GET", "/projects")

    def create(self, seed: str, name: Optional[str] = None, schedule: str = "manual", retention: str = "90d",
               config: Optional[Json] = None) -> Json:
        body: Json = {"seed": seed, "schedule": schedule, "retention": retention}
        if name:
            body["name"] = name
        if config:
            body["config"] = config
        return self._h("POST", "/projects", json=body)

    def get(self, project_id: str) -> Json:
        return self._h("GET", f"/projects/{project_id}")

    def update(self, project_id: str, **fields: Any) -> Json:
        return self._h("PATCH", f"/projects/{project_id}", json=fields)

    def delete(self, project_id: str) -> None:
        self._h("DELETE", f"/projects/{project_id}")


class _Runs:
    def __init__(self, http: _Http) -> None:
        self._h = http

    def list(self, project_id: str, limit: int = 25) -> List[Json]:
        return self._h("GET", f"/projects/{project_id}/runs", params={"limit": limit})

    def get(self, project_id: str, run_id: str) -> Json:
        return self._h("GET", f"/projects/{project_id}/runs/{run_id}")

    def start(self, project_id: str, wait: bool = False, poll: float = 3.0, timeout: float = 3600) -> Json:
        run = self._h("POST", f"/projects/{project_id}/runs", json={"trigger": "api"})
        return self.wait(project_id, run["id"], poll, timeout) if wait else run

    def wait(self, project_id: str, run_id: str, poll: float = 3.0, timeout: float = 3600) -> Json:
        deadline = time.time() + timeout
        while True:
            run = self.get(project_id, run_id)
            if run["status"] != "running" and not run.get("queued"):
                return run
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"run {run_id} is still {run['status']} after {timeout}s", run_id)
            time.sleep(poll)

    def cancel(self, project_id: str, run_id: str) -> Json:
        return self._h("POST", f"/projects/{project_id}/runs/{run_id}/cancel")


class Crawl:
    """A crawl started by ``arc.crawl(url)``: a handle on a running job.

    ``wait()`` blocks until it finishes; ``pages()`` yields pages as they
    land, following the cursor, and ends when the job does; ``keep()``
    turns the one-shot crawl into a project; ``cancel()`` stops it.
    """

    def __init__(self, http: _Http, envelope: Json) -> None:
        self._h = http
        self.id: str = envelope["id"]
        self.url: str = envelope.get("url", "")
        self.project_id: str = envelope.get("projectId", "")
        #: Returned once, at creation: the secret the crawl's webhook messages are signed with.
        self.webhook_secret: str = envelope.get("webhookSecret", "")
        self.envelope: Json = envelope

    def __repr__(self) -> str:
        return f"<Crawl {self.id[:8]} {self.url} {self.envelope.get('status')}>"

    @property
    def status(self) -> str:
        return self.envelope.get("status", "queued")

    def refresh(self, formats: str = "markdown") -> Json:
        """The envelope as it stands now, without its pages."""
        self.envelope = self._h("GET", f"/crawl/{self.id}", params={"limit": 1, "formats": formats})
        return self.envelope

    def wait(self, poll: float = 3.0, timeout: float = 3600, formats: str = "markdown") -> Json:
        """Block until the crawl finishes. Returns the envelope."""
        deadline = time.time() + timeout
        while True:
            e = self.refresh(formats)
            if not _running(e["status"]):
                return e
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"crawl {self.id} is still {e['status']} after {timeout}s", self.id)
            time.sleep(poll)

    def pages(self, formats: str = "markdown", limit: int = 25, wait: bool = True, poll: float = 3.0,
              timeout: float = 3600) -> Iterator[Json]:
        """Every page of the crawl, oldest first.

        While the crawl runs this waits for more pages rather than
        stopping; ``wait=False`` yields what exists and returns.
        """
        deadline = time.time() + timeout
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"formats": formats, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            page = self._h("GET", f"/crawl/{self.id}", params=params)
            self.envelope = {k: v for k, v in page.items() if k != "data"}
            for row in page["data"]:
                yield row
            # The cursor marks where this page ended, so a running crawl is never re-read from the top.
            cursor = page.get("cursor") or cursor
            if page.get("next"):
                cursor = _cursor_of(page["next"]) or cursor
                continue
            if not wait or not _running(page["status"]):
                return
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"crawl {self.id} is still {page['status']} after {timeout}s", self.id)
            time.sleep(poll)

    def keep(self, name: Optional[str] = None, schedule: Optional[str] = None,
             retention: Optional[str] = None) -> Json:
        """Make this one-shot crawl a project. Its run and pages are already in place."""
        body = {k: v for k, v in (("name", name), ("schedule", schedule), ("retention", retention)) if v}
        return self._h("POST", f"/crawl/{self.id}/keep", json=body)

    def cancel(self) -> Json:
        self._h("DELETE", f"/crawl/{self.id}")
        self.envelope["status"] = "cancelled"
        return self.envelope


class MeshArc:
    """One client, one API key, one workspace.

    ``api_key`` falls back to the ``MESHARC_API_KEY`` environment variable.
    ``timeout`` is the HTTP timeout per request in seconds; ``max_retries``
    how many times a request that is safe to repeat is retried on 429,
    502, 503, 504 or a network failure. ``base_url`` is for MeshArc's own
    test environments; the hosted API needs nothing there.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT, max_retries: int = DEFAULT_MAX_RETRIES) -> None:
        key = api_key or os.environ.get("MESHARC_API_KEY") or ""
        if not key:
            raise ValueError("An API key is required: MeshArc('mesharc_...') or set MESHARC_API_KEY.")
        base = base_url or os.environ.get("MESHARC_API_URL") or DEFAULT_BASE
        self._h = _Http(key, base, timeout, max_retries)
        self.projects = _Projects(self._h)
        self.runs = _Runs(self._h)

    # ------------------------------------------------------------ one or many URLs

    def extract(self, url: str, config: Optional[Json] = None, wait: bool = True, poll: float = 2.0,
                timeout: float = 300) -> Json:
        """One URL with every format, as the app's playground reads it."""
        body: Json = {"url": url}
        if config:
            body["config"] = config
        job = self._h("POST", "/playground/extract", json=body)
        if not wait:
            return job
        deadline = time.time() + timeout
        while True:
            r = self._h("GET", f"/playground/{job['id']}")
            if not _running(r["status"]):
                return r
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"extraction {job['id']} is still {r['status']} after {timeout}s", job["id"])
            time.sleep(poll)

    def scrape(self, urls: Union[str, Iterable[str]], config: Optional[Json] = None,
               webhook_url: Optional[str] = None, formats: str = "markdown", wait: bool = True,
               poll: float = 3.0, timeout: float = 3600, idempotency_key: Optional[str] = None) -> Json:
        """URLs in, their content out; no project.

        One URL (a string) returns the page itself: the API holds the
        request open until the page comes back. A list is a batch and
        returns the finished batch, one row per URL, unless ``wait`` is
        False.
        """
        if isinstance(urls, str):
            return self.scrape_one(urls, config=config, formats=formats, wait=wait, poll=poll,
                                   timeout=timeout, idempotency_key=idempotency_key)
        body: Json = {"urls": list(urls)}
        if config:
            body["config"] = config
        if webhook_url:
            body["webhook_url"] = webhook_url
        batch = self._h("POST", "/scrape", json=body, idempotency_key=idempotency_key)
        if not wait:
            return batch
        return self.batch(batch["id"], formats=formats, wait=True, poll=poll, timeout=timeout)

    def scrape_one(self, url: str, config: Optional[Json] = None, formats: str = "markdown", wait: bool = True,
                   timeout_s: int = 60, poll: float = 2.0, timeout: float = 600,
                   idempotency_key: Optional[str] = None) -> Json:
        """One URL, waited for. Returns the page; ``wait=False`` returns the job envelope.

        ``timeout_s`` is how long the API holds the request open for the
        page (60 by default, 120 at most); a slower page comes back as a
        job, which is then polled every ``poll`` seconds for up to
        ``timeout`` seconds.
        """
        body: Json = {"url": url, "formats": formats, "timeout": timeout_s}
        if config:
            body["config"] = config
        out = self._h("POST", "/scrape", json=body, idempotency_key=idempotency_key)
        if not wait:
            return out
        deadline = time.time() + timeout
        while True:
            if out.get("status") == "done":
                return (out.get("data") or [{}])[0]
            if not _running(out.get("status")):
                raise MeshArcError(502, out.get("error") or f"scrape {out.get('status')}", "job_failed")
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"scrape {out['id']} is still {out['status']} after {timeout}s", out["id"])
            time.sleep(poll)
            out = self._h("GET", f"/scrape/{out['id']}", params={"formats": formats})

    def batch(self, batch_id: str, formats: str = "markdown", wait: bool = False, poll: float = 3.0,
              timeout: float = 3600) -> Json:
        """A batch started earlier. With ``wait``, returns once every row is in."""
        deadline = time.time() + timeout
        while True:
            r = self._h("GET", f"/scrape/{batch_id}", params={"formats": formats})
            if not wait or not _running(r["status"]):
                return r
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"batch {batch_id} is still {r['status']} after {timeout}s", batch_id)
            time.sleep(poll)

    # ---------------------------------------------------------------- a whole site

    def crawl(self, url: str, wait: bool = False, poll: float = 3.0, timeout: float = 3600,
              idempotency_key: Optional[str] = None, **opts: Any) -> Crawl:
        """Crawl a site once, with no project to set up first.

        Returns a ``Crawl`` handle as soon as the job is queued; ``wait=True``
        blocks until it finishes. ``opts`` are the request's own names --
        ``limit``, ``maxDepth``, ``includePaths``, ``excludePaths``,
        ``crawlMode``, ``maxAge``, ``maxTier``, ``scrapeOptions``,
        ``webhook`` -- and ``config`` takes any project setting directly.
        """
        job = Crawl(self._h, self._h("POST", "/crawl", json={"url": url, **opts}, idempotency_key=idempotency_key))
        if wait:
            job.wait(poll=poll, timeout=timeout)
        return job

    def get_crawl(self, crawl_id: str) -> Crawl:
        """A handle on a crawl started earlier or elsewhere."""
        return Crawl(self._h, self._h("GET", f"/crawl/{crawl_id}", params={"limit": 1}))

    def map(self, url: str, **opts: Any) -> List[Json]:
        """Every URL a site declares in its sitemaps, as ``{url, lastmod, changefreq, source, file, section}``."""
        return self.map_details(url, **opts)["data"]

    def map_details(self, url: str, search: Optional[str] = None, limit: Optional[int] = None,
                    timeout_s: int = 10, poll: float = 2.0, timeout: float = 300, **opts: Any) -> Json:
        """A map with everything the API said about it: how the sitemaps
        were found, the totals, what robots.txt allowed, and the URLs
        under ``data``. ``timeout_s`` is how long the API waits for the
        sitemap tree before answering with a job (10 by default, 60 at
        most), which is then polled.
        """
        body: Json = {"url": url, "timeout": timeout_s, **opts}
        if search:
            body["search"] = search
        if limit:
            body["limit"] = limit
        out = self._h("POST", "/map", json=body)
        params = {k: v for k, v in (("search", search), ("limit", limit)) if v}
        deadline = time.time() + timeout
        while out.get("status") == "running":
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"map {out['id']} is still reading {url} after {timeout}s", out["id"])
            time.sleep(poll)
            out = self._h("GET", f"/map/{out['id']}", params=params or None)
        if out.get("status") != "done":
            raise MeshArcError(502, out.get("error") or "no sitemap could be read", "job_failed")
        return out

    # ---------------------------------------------------------- what a project holds

    def pages(self, project_id: str, run_id: Optional[str] = None) -> Json:
        """The pages of a run (the latest finished run by default)."""
        return self._h("GET", f"/projects/{project_id}/pages", params={"run_id": run_id} if run_id else None)

    def page(self, project_id: str, url: str, run_id: Optional[str] = None) -> Json:
        """One page in full: bodies, head fields, structured fields, versions."""
        params: Dict[str, Any] = {"url": url}
        if run_id:
            params["run_id"] = run_id
        return self._h("GET", f"/projects/{project_id}/pages/content", params=params)

    def changes(self, project_id: str, run_id: Optional[str] = None) -> Json:
        """The change record of a run against the run before it."""
        return self._h("GET", f"/projects/{project_id}/changes", params={"run_id": run_id} if run_id else None)

    def page_diff(self, project_id: str, url: str, run_id: Optional[str] = None) -> Json:
        """The word-level diff of one page against the run before."""
        params: Dict[str, Any] = {"url": url}
        if run_id:
            params["run_id"] = run_id
        return self._h("GET", f"/projects/{project_id}/changes/page", params=params)

    def search(self, project_id: str, q: str, mode: str = "content", run_id: Optional[str] = None) -> Json:
        """Which pages say this (``content``: words, "phrases") or contain this (``selector``: CSS or XPath)."""
        return self._h("POST", f"/projects/{project_id}/pages/search", json={"mode": mode, "q": q, "run_id": run_id})

    def recrawl(self, project_id: str, urls: Iterable[str]) -> Json:
        """Fetch these pages again now, as a scoped run."""
        return self._h("POST", f"/projects/{project_id}/pages/recrawl", json={"urls": list(urls)})

    def sources(self, project_id: str) -> Json:
        """The seed, sitemap, URL list, feeds and patterns, with what the last run found through each."""
        return self._h("GET", f"/projects/{project_id}/sources")

    def export(self, project_id: str, path: str, dataset: str = "pages", fmt: str = "jsonl",
               run_id: Optional[str] = None, urls: Optional[Iterable[str]] = None) -> str:
        """Stream a dataset (pages, markdown, changes, fields, sitemap) to ``path`` as jsonl or csv.

        ``urls`` restricts the export to those pages. Returns ``path``.
        """
        if urls:
            ctx = self._h.stream("POST", f"/projects/{project_id}/export",
                                 json={"dataset": dataset, "format": fmt, "run_id": run_id, "urls": list(urls)})
        else:
            params: Dict[str, Any] = {"dataset": dataset, "format": fmt}
            if run_id:
                params["run_id"] = run_id
            ctx = self._h.stream("GET", f"/projects/{project_id}/export", params=params)
        with ctx as r:
            if r.status_code >= 400:
                r.read()
                raise self._h._error(r)
            with open(path, "wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
        return path

    # --------------------------------------------------------------------- workspace

    def me(self) -> Json:
        """The workspace, its plan and limits, and what this key may do."""
        return self._h("GET", "/me")

    def keys(self) -> List[Json]:
        return self._h("GET", "/me/keys")

    def create_key(self, name: str = "default", scopes: Optional[List[str]] = None,
                   projects: Optional[List[str]] = None, expires_in_days: Optional[int] = None,
                   rpm: Optional[int] = None) -> Json:
        """A new API key. The plaintext is in the response under ``key``, once."""
        body: Json = {"name": name}
        for key, value in (("scopes", scopes), ("projects", projects),
                           ("expires_in_days", expires_in_days), ("rpm", rpm)):
            if value is not None:
                body[key] = value
        return self._h("POST", "/me/keys", json=body)

    def revoke_key(self, key_id: str) -> None:
        self._h("DELETE", f"/me/keys/{key_id}")

    def usage(self) -> Json:
        return self._h("GET", "/me/usage")

    def monitor(self) -> Json:
        return self._h("GET", "/me/monitor")

    def meta(self) -> Json:
        """Verdict meanings, engine costs, the configuration defaults and the ladder."""
        return self._h("GET", "/meta")

    def close(self) -> None:
        self._h.close()

    def __enter__(self) -> "MeshArc":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
