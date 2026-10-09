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
from datetime import date
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union
from urllib.parse import parse_qs, urlparse

import httpx

__version__ = "0.6.0"
__all__ = ["MeshArc", "MeshArcError", "MeshArcTimeoutError", "Crawl", "AgentRun"]

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


def _iso(when: Union[str, date]) -> str:
    """A date or date-time as the API reads it: ISO 8601, a string as given."""
    return when.isoformat() if isinstance(when, date) else when


class _Http:
    """The transport: one httpx client, bearer auth, JSON errors, retries."""

    def __init__(self, api_key: str, base_url: str, timeout: float, max_retries: int) -> None:
        self._c = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": f"mesharc-python/{__version__}"},
            timeout=timeout,
        )
        self._max_retries = max(0, int(max_retries))
        # What the last response said about the key's rate limit, so a
        # polling loop can slow down before it is refused rather than after.
        self._remaining: Optional[int] = None
        self._reset_at: float = 0.0

    def _note_limits(self, r: httpx.Response) -> None:
        try:
            if "X-RateLimit-Remaining" in r.headers:
                self._remaining = int(r.headers["X-RateLimit-Remaining"])
                self._reset_at = time.monotonic() + float(r.headers.get("X-RateLimit-Reset") or 0)
        except ValueError:
            pass

    def pace(self, floor: int = 3) -> None:
        """Wait out the window when the key is nearly out of requests. A
        wait loop that polls every few seconds would otherwise spend a
        small plan's minute on polling and be refused for the call that
        matters."""
        if self._remaining is not None and self._remaining <= floor:
            left = self._reset_at - time.monotonic()
            if left > 0:
                time.sleep(min(left, 60.0))
            self._remaining = None

    def __call__(self, method: str, path: str, idempotency_key: Optional[str] = None, **kw: Any) -> Any:
        if idempotency_key:
            kw.setdefault("headers", {})["Idempotency-Key"] = str(idempotency_key)
        # A GET or DELETE is safe to repeat; a POST only when it carries an idempotency key.
        repeatable = method in ("GET", "DELETE") or bool(idempotency_key)
        attempt = 0
        while True:
            # Out of requests this minute: wait for the window rather than
            # send a call that will only be refused.
            self.pace(floor=0)
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
            self._note_limits(r)
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
            self._h.pace()

    def cancel(self, project_id: str, run_id: str) -> Json:
        return self._h("POST", f"/projects/{project_id}/runs/{run_id}/cancel")


class _Monitors:
    """A search or an agent request, kept and run again on a schedule: ``arc.monitors``.

    Not to be confused with ``arc.monitor()``, which is the workspace's job queue.
    """

    def __init__(self, http: _Http) -> None:
        self._h = http

    def create(self, kind: str, request: Json, schedule: str, name: Optional[str] = None,
               webhook: Union[str, Json, None] = None, baseline_id: Optional[str] = None,
               idempotency_key: Optional[str] = None) -> Json:
        """Keep a search or an agent request and run it on a schedule.

        ``kind`` is ``search`` or ``agent``; ``request`` is the body of a
        POST /search or POST /agent in the API's own (camelCase) names
        (``{"query": ..., "freshness": "day"}``, ``{"prompt": ...,
        "schema": ..., "maxCredits": ...}``), checked as those check it;
        ``schedule`` is ``hourly``, ``daily`` or ``weekly``. ``name`` is the
        query or prompt when left out. A monitored search reads its results
        page only, so ``scrape`` is refused. A news monitor's request says
        so in the API's name, ``"sources": ["news"]``: ``web_search(news=True)``
        maps its argument to that, and ``create`` does not. Each run is the
        ordinary search or agent run, charged as one, and is compared with
        the last run that answered: new, dropped and moved results, or the
        answer's added, changed and removed values.

        ``webhook`` is a URL, or ``{"url", "events", "metadata"}``, told
        ``search.changed`` / ``agent.changed`` when a run found something
        different; the secret its messages are signed with comes back once,
        as ``webhookSecret``. ``baseline_id`` is a finished search or agent
        run with the same request, taken as the first run and not paid for
        again; without it the first run starts now. A search baseline's
        ``limit`` must be at least the monitor's.

        Spends credits and needs a key that can write. Returns the monitor.
        """
        body: Json = {"kind": kind, "request": request, "schedule": schedule}
        for key, value in (("name", name), ("webhook", webhook), ("baselineId", baseline_id)):
            if value is not None:
                body[key] = value
        return self._h("POST", "/monitors", json=body, idempotency_key=idempotency_key)

    def list(self, kind: Optional[str] = None) -> List[Json]:
        """The workspace's monitors, newest first, each with its last run under
        ``lastRun``. ``kind`` keeps ``search`` or ``agent`` ones."""
        return self._h("GET", "/monitors", params={"kind": kind} if kind else None)["data"]

    def get(self, monitor_id: str) -> Json:
        """One monitor, with its last run."""
        return self._h("GET", f"/monitors/{monitor_id}")

    def update(self, monitor_id: str, name: Optional[str] = None, schedule: Optional[str] = None,
               status: Optional[str] = None, webhook: Union[str, Json, None] = None) -> Json:
        """Change a monitor's ``name``, ``schedule``, ``status`` (``active`` or
        ``paused``) or ``webhook``. Only the ones that are not None are sent;
        ``webhook=""`` removes the webhook, and a new one's secret comes back
        once, as ``webhookSecret``. Needs a key that can write."""
        body: Json = {key: value for key, value in (("name", name), ("schedule", schedule), ("status", status),
                                                    ("webhook", webhook)) if value is not None}
        return self._h("PATCH", f"/monitors/{monitor_id}", json=body)

    def pause(self, monitor_id: str) -> Json:
        """No more scheduled runs until it is resumed; a run under way finishes."""
        return self._h("POST", f"/monitors/{monitor_id}/pause")

    def resume(self, monitor_id: str) -> Json:
        """Scheduled again: a slot that passed while it was paused runs once, at the next tick."""
        return self._h("POST", f"/monitors/{monitor_id}/resume")

    def run(self, monitor_id: str, idempotency_key: Optional[str] = None) -> Json:
        """Run it now, paused or not; the schedule counts on from this run.

        Spends credits and needs a key that can write. Returns the new run,
        ``running`` (``error`` with the reason under ``error`` when it could
        not start); its ``refId`` is the search or agent run it made
        (``get_search`` / ``get_agent``). Raises ``MeshArcError`` 409 (code
        ``conflict``) while a run is under way.
        """
        return self._h("POST", f"/monitors/{monitor_id}/run", idempotency_key=idempotency_key)

    def runs(self, monitor_id: str, limit: int = 25) -> Iterator[Json]:
        """Every run of a monitor, newest first, each with what changed since
        the run before that answered (``changed``, ``summary``, ``diff``).
        ``limit`` is the page size; follows the ``next`` link until the
        list ends.
        """
        cursor = ""
        while True:
            params: Dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            page = self._h("GET", f"/monitors/{monitor_id}/runs", params=params)
            for row in page.get("data") or []:
                yield row
            cursor = _cursor_of(page["next"]) if page.get("next") else ""
            if not cursor:
                return

    def delete(self, monitor_id: str) -> Json:
        """The monitor and its runs. The searches and agent runs it made stay
        in the workspace's history; an agent run it has under way is
        stopped. Returns ``{id, deleted: true}``."""
        return self._h("DELETE", f"/monitors/{monitor_id}")


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
            self._h.pace()

    def pages(self, formats: str = "markdown", limit: int = 25, wait: bool = True, poll: float = 3.0,
              timeout: float = 3600, cursor: Optional[str] = None) -> Iterator[Json]:
        """Every page of the crawl, oldest first.

        While the crawl runs this waits for more pages rather than
        stopping; ``wait=False`` yields what exists and returns.
        ``cursor`` resumes from where an earlier walk stopped, which is what
        a caller that read the first few hundred pages and wants the rest
        asks with -- the value is the one the API's ``next`` link carries.
        """
        deadline = time.time() + timeout
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
            self._h.pace()

    def page(self, url: str) -> Json:
        """One page of this crawl in full: markdown, html, head, fields.

        ``pages()`` is how you walk the crawl; this is how you come back for
        the whole of a single page once you know which one you want. The URL
        is matched under either scheme and with or without a trailing slash.
        """
        return self._h("GET", f"/crawl/{self.id}/page", params={"url": url})

    def keep(self, name: Optional[str] = None, schedule: Optional[str] = None,
             retention: Optional[str] = None) -> Json:
        """Make this one-shot crawl a project. Its run and pages are already in place."""
        body = {k: v for k, v in (("name", name), ("schedule", schedule), ("retention", retention)) if v}
        return self._h("POST", f"/crawl/{self.id}/keep", json=body)

    def cancel(self) -> Json:
        self._h("DELETE", f"/crawl/{self.id}")
        self.envelope["status"] = "cancelled"
        return self.envelope


class AgentRun:
    """An agent run started by ``arc.agent(prompt)``: a handle on the job.

    ``wait()`` blocks until it finishes and returns the envelope, the answer
    under ``data``; ``trace()`` yields what the agent does as it does it;
    ``cancel()`` stops it; ``continue_()`` carries on a run that stopped at
    its credit limit. ``envelope`` is the last envelope seen.
    """

    def __init__(self, http: _Http, envelope: Json) -> None:
        self._h = http
        self.id: str = envelope["id"]
        self.envelope: Json = envelope
        # Kept apart from the envelope: the API sends it once, on the POST,
        # and a refresh() would otherwise lose it.
        self._webhook_secret: str = envelope.get("webhookSecret") or ""

    def __repr__(self) -> str:
        return f"<AgentRun {self.id[:8]} {self.envelope.get('status')}>"

    @property
    def status(self) -> str:
        return self.envelope.get("status", "queued")

    @property
    def data(self) -> Any:
        """The answer: JSON matching the schema, ``{"text": ...}`` without
        one, ``{"partial": ...}`` when the run hit its credit limit."""
        return self.envelope.get("data")

    @property
    def sources(self) -> List[Json]:
        """The pages the answer rests on, as ``{url, title, pageId}``."""
        return self.envelope.get("sources") or []

    @property
    def field_sources(self) -> Json:
        """Where each value of ``data`` came from: its path (``"plans[0].price"``,
        or ``"[2].name"`` for a list answer) to ``{url, pageId}``, a page
        this run read. ``{}`` when the API said nothing."""
        return self.envelope.get("fieldSources") or {}

    @property
    def webhook_secret(self) -> str:
        """Returned once, at creation: the secret the run's webhook messages
        are signed with. A run made by ``continue_()`` carries on its
        stopped run's webhook and secret, so its handle has the stopped
        handle's. ``""`` for a run started without a webhook, or one
        reopened with ``get_agent``."""
        return self.envelope.get("webhookSecret") or self._webhook_secret

    @property
    def continues_run_id(self) -> Optional[str]:
        """The run this one carried on, when it was made by ``continue_()``."""
        return self.envelope.get("continuesRunId") or None

    @property
    def continued_by(self) -> Optional[str]:
        """The run that carried this one on: its answer replaces this one's partial."""
        return self.envelope.get("continuedBy") or None

    def refresh(self) -> AgentRun:
        """Read the run as it stands now. Returns the handle."""
        self.envelope = self._h("GET", f"/agent/{self.id}")
        return self

    def wait(self, poll: float = 2.0, timeout: float = 3600) -> Json:
        """Block until the run finishes. Returns the envelope.

        A run that is ``done``, ``cancelled`` or stopped at ``credit_limit``
        (with what it had under ``data.partial``) is returned; ``error``
        raises ``MeshArcError`` with the API's text.
        """
        deadline = time.time() + timeout
        while True:
            status = self.refresh().status
            if status == "error":
                raise MeshArcError(502, self.envelope.get("error") or f"agent {status}", "job_failed")
            if not _running(status):
                return self.envelope
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"agent {self.id} is still {status} after {timeout}s", self.id)
            time.sleep(poll)
            self._h.pace()

    def cancel(self) -> Json:
        """Stop the run. Returns the API's answer, ``{id, status}``: a queued
        run is ``cancelled``, a running one ``cancelling`` (it stops before
        its next step), a finished one keeps its status. ``refresh()``
        reads where it ended."""
        return self._h("DELETE", f"/agent/{self.id}")

    def continue_(self, max_credits: Optional[int] = None, max_steps: Optional[int] = None, timeout_s: float = 0,
                  idempotency_key: Optional[str] = None) -> AgentRun:
        """Carry on a run that stopped at ``credit_limit``, with a new budget.

        Returns an ``AgentRun`` on the new run, queued or running unless
        ``timeout_s`` asks the API to hold the request (120 at most). It
        resumes from where this one stopped, keeps the pages it read (not
        paid for again) and its webhook, whose secret the new handle's
        ``webhook_secret`` carries on from this one, and answers in full; its
        ``continues_run_id`` is this run, and this run's ``continued_by``
        the new one once refreshed. ``max_credits`` and ``max_steps`` are
        the new run's own; left out, they are this run's.

        Spends credits and needs a key that can write. Raises
        ``MeshArcError`` 409 (code ``conflict``) for a run that did not stop
        at its credit limit, was already continued or has no saved
        progress, 404 for no such run and 410 (``expired``) past its keep
        date.
        """
        body: Json = {}
        if max_credits is not None:
            body["maxCredits"] = max_credits
        if max_steps is not None:
            body["maxSteps"] = max_steps
        if timeout_s > 0:
            body["timeout"] = min(timeout_s, 120)
        run = AgentRun(self._h, self._h("POST", f"/agent/{self.id}/continue", json=body,
                                        idempotency_key=idempotency_key))
        # The API carries the webhook and its secret over to the new run, but
        # the continue answer does not repeat the secret: keep this run's.
        run._webhook_secret = run._webhook_secret or self.webhook_secret
        return run

    def trace(self, after: int = 0, follow: bool = True, poll: float = 2.0,
              timeout: float = 3600) -> Iterator[Json]:
        """What the agent did, step by step, as ``{seq, t, kind, text, ...}``.

        ``kind`` is start, resume, continue, model, search, fetch, render,
        map, select, extract, tool, busy or finish. ``after`` is the ``seq``
        of the last event already seen, to resume after it. With ``follow``
        this keeps reading until the run ends, polling every ``poll``
        seconds; ``follow=False`` yields what exists and returns. A run
        that has expired ends the walk quietly.
        """
        deadline = time.time() + timeout
        while True:
            live = True
            if follow:
                # Read the status before the trace, so the events written
                # just before the run ended are drained, not missed.
                try:
                    live = _running(self.refresh().status)
                except MeshArcError as exc:
                    if exc.status == 410:
                        return
                    raise
            while True:
                try:
                    page = self._h("GET", f"/agent/{self.id}/trace", params={"after": after, "limit": 500})
                except MeshArcError as exc:
                    if exc.status == 410:
                        return
                    raise
                rows = page.get("data") or []
                for event in rows:
                    yield event
                last = page.get("last")
                moved = last is not None and last != after
                if last is not None:
                    after = last
                if len(rows) < 500 or not moved:
                    break
            if not follow or not live:
                return
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"agent {self.id} is still {self.status} after {timeout}s", self.id)
            time.sleep(poll)
            self._h.pace()


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
        self.monitors = _Monitors(self._h)

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
            self._h.pace()

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
            self._h.pace()
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
            self._h.pace()

    def cancel_batch(self, batch_id: str) -> None:
        """Stop a scrape: every run of a batch, or a one-URL scrape still going.
        Queued pages are dropped; a page being read finishes first, and pages
        already read stay readable."""
        self._h("DELETE", f"/scrape/{batch_id}")

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
            self._h.pace()
            out = self._h("GET", f"/map/{out['id']}", params=params or None)
        if out.get("status") != "done":
            raise MeshArcError(502, out.get("error") or "no sitemap could be read", "job_failed")
        return out

    def web_search(self, query: str, limit: Optional[int] = None, country: Optional[str] = None,
                   lang: Optional[str] = None, freshness: Optional[str] = None,
                   include_domains: Optional[Iterable[str]] = None, exclude_domains: Optional[Iterable[str]] = None,
                   scrape: Union[bool, Json, None] = None, destination: Optional[str] = None, wait: bool = True,
                   timeout_s: Optional[float] = None, poll: float = 2.0, timeout: float = 600,
                   idempotency_key: Optional[str] = None, news: bool = False, page: Optional[int] = None) -> Json:
        """Search the web. Returns the whole envelope, the hits under ``data``.

        Needs an API key that can write, and spends credits: a results page
        every engine refused is free, an equal search (same query, ``news``,
        ``page``, ``country``, ``lang``, ``freshness`` and domains) within the
        hour of a finished one -- ten minutes for news -- comes from the
        cache with no results-page charge, and scraped pages are charged
        even then. ``news=True`` searches the engines' news results instead
        of the web's, and each hit also carries ``publisher`` and ``age``
        (as the engine put it, e.g. ``"20h"``). ``page`` is which results
        page, 1 to 10: page 2 is results 11-20, each page its own search
        and its own charge. ``freshness`` is ``hour``, ``day``,
        ``week``, ``month`` or ``year``. ``scrape=True`` also fetches each
        hit as markdown; a dict such as ``{"formats": ["markdown", "links"],
        "maxCredits": 20}`` is sent as given. The formats are ``markdown``,
        ``text``, ``rawHtml``, ``cleanHtml``, ``links``, ``raw``,
        ``screenshot`` and ``json``. A search that scrapes stays ``running``
        until its pages land, with the hits already in ``data``.

        A ``blocked`` search (every engine refused) is returned, not
        raised; ``error`` raises ``MeshArcError``. ``timeout_s`` is how
        long the API holds the request open (60 by default when waiting,
        120 at most); a slower search is then polled every ``poll`` seconds
        for up to ``timeout`` seconds. ``wait=False`` returns at once,
        queued or running, unless ``timeout_s`` is given.
        """
        body: Json = {"query": query}
        if news:
            body["sources"] = ["news"]
        for key, value in (("limit", limit), ("page", page), ("country", country), ("lang", lang),
                           ("freshness", freshness), ("destination", destination)):
            if value is not None:
                body[key] = value
        if include_domains is not None:
            body["includeDomains"] = list(include_domains)
        if exclude_domains is not None:
            body["excludeDomains"] = list(exclude_domains)
        if scrape is True:
            body["scrape"] = {"formats": ["markdown"]}
        elif isinstance(scrape, dict):
            body["scrape"] = scrape
        body["timeout"] = timeout_s if timeout_s is not None else (60 if wait else 0)
        out = self._h("POST", "/search", json=body, idempotency_key=idempotency_key)
        if not wait:
            return out
        deadline = time.time() + timeout
        while True:
            if out.get("status") in ("done", "blocked"):
                return out
            if not _running(out.get("status")):
                raise MeshArcError(502, out.get("error") or f"search {out.get('status')}", "job_failed")
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"search {out['id']} is still {out['status']} after {timeout}s", out["id"])
            time.sleep(poll)
            self._h.pace()
            out = self._h("GET", f"/search/{out['id']}")

    def get_search(self, search_id: str) -> Json:
        """A web search started earlier, as ``web_search`` returns it."""
        return self._h("GET", f"/search/{search_id}")

    def searches(self, q: Optional[str] = None, limit: int = 25) -> Iterator[Json]:
        """Every web search of the workspace, as summaries.

        ``q`` filters the list by query; ``limit`` is the page size, sent
        again with ``q`` on every page. Follows the ``next`` link until the
        list ends.
        """
        cursor = ""
        while True:
            params: Dict[str, Any] = {"limit": limit}
            if q:
                params["q"] = q
            if cursor:
                params["cursor"] = cursor
            page = self._h("GET", "/search", params=params)
            for row in page.get("data") or []:
                yield row
            cursor = _cursor_of(page["next"]) if page.get("next") else ""
            if not cursor:
                return

    def agent(self, prompt: str, urls: Optional[Iterable[str]] = None, schema: Optional[Json] = None,
              max_credits: Optional[int] = None, max_steps: Optional[int] = None,
              allowed_domains: Optional[Iterable[str]] = None, webhook: Union[str, Json, None] = None,
              connection_id: Optional[str] = None, timeout_s: float = 0,
              idempotency_key: Optional[str] = None) -> AgentRun:
        """Hand a question to the agent: it searches, reads pages and answers.

        Needs an API key that can write, and spends credits: pages as they
        are read (a refused page is free) and the model's tokens at the
        model provider's price plus 20%; ``max_credits`` caps the run (2000
        by default on the API). ``connection_id`` runs it on one of the
        workspace's own LLM connections instead -- its model, its key, its
        bill -- and the tokens then cost 1 credit per 1,000.
        ``urls`` are pages to start from, ``allowed_domains`` keeps the run
        to those sites, ``max_steps`` caps its turns (40 by default).
        ``schema`` is a JSON Schema whose type is ``object`` or ``array``;
        the answer under ``data`` then matches it, and without one it is
        ``{"text": ...}``.

        ``webhook`` is a URL, or ``{"url", "events", "metadata"}``, told
        about ``agent.started``, ``agent.action``, ``agent.completed``,
        ``agent.failed`` and ``agent.cancelled`` (all five by default); the
        secret its messages are signed with comes back once, as
        ``AgentRun.webhook_secret``.

        Returns an ``AgentRun`` at once, queued or running, unless
        ``timeout_s`` asks the API to hold the request for the answer (120
        at most); ``AgentRun.wait()`` waits for it.
        """
        body: Json = {"prompt": prompt}
        if urls is not None:
            body["urls"] = list(urls)
        if schema is not None:
            body["schema"] = schema
        if max_credits is not None:
            body["maxCredits"] = max_credits
        if max_steps is not None:
            body["maxSteps"] = max_steps
        if allowed_domains is not None:
            body["allowedDomains"] = list(allowed_domains)
        if webhook is not None:
            body["webhook"] = webhook
        if connection_id is not None:
            body["connectionId"] = connection_id
        if timeout_s > 0:
            body["timeout"] = min(timeout_s, 120)
        return AgentRun(self._h, self._h("POST", "/agent", json=body, idempotency_key=idempotency_key))

    def get_agent(self, run_id: str) -> AgentRun:
        """A handle on an agent run started earlier or elsewhere.

        A run past its keep date (7 days by default) is gone: the API
        answers 410 and this raises ``MeshArcError`` with status 410 and
        code ``expired``.
        """
        return AgentRun(self._h, self._h("GET", f"/agent/{run_id}"))

    def agent_runs(self, status: Optional[str] = None, limit: int = 25, model: Optional[str] = None,
                   since: Union[str, date, None] = None, until: Union[str, date, None] = None) -> Iterator[Json]:
        """Every agent run of the workspace, as summaries.

        ``status`` filters the list (queued, running, done, error,
        cancelled, credit_limit); ``model`` keeps one model's runs, named
        as runs name it (``"openai:gpt-5.4-mini"``); ``since`` and ``until``
        keep the runs made at or after ``since`` and before ``until``, each
        an ISO 8601 date or date-time string or a ``date`` / ``datetime``.
        A date means its midnight UTC; a date-time without a zone is read
        as UTC (so a naive ``datetime.now()`` is your local time read as
        UTC).
        ``limit`` is the page size, sent again with the filters on every
        page. Follows the ``next`` link until the list ends.
        """
        filters: Dict[str, Any] = {k: v for k, v in (("status", status), ("model", model),
                                                     ("since", _iso(since) if since else None),
                                                     ("until", _iso(until) if until else None)) if v}
        cursor = ""
        while True:
            params: Dict[str, Any] = {"limit": limit, **filters}
            if cursor:
                params["cursor"] = cursor
            page = self._h("GET", "/agent", params=params)
            for row in page.get("data") or []:
                yield row
            cursor = _cursor_of(page["next"]) if page.get("next") else ""
            if not cursor:
                return

    def pages(self, project_id: str, run_id: Optional[str] = None) -> Json:
        """The pages of a run (the latest finished run by default)."""
        return self._h("GET", f"/projects/{project_id}/pages", params={"run_id": run_id} if run_id else None)

    def page(self, project_id: str, url: str, run_id: Optional[str] = None) -> Json:
        """One page in full: bodies, head fields, structured fields, versions."""
        params: Dict[str, Any] = {"url": url}
        if run_id:
            params["run_id"] = run_id
        return self._h("GET", f"/projects/{project_id}/pages/content", params=params)

    def changes(self, project_id: str, run_id: Optional[str] = None, against: Optional[str] = None) -> Json:
        """The change record of a run against the run before it, or with
        ``against`` against that run instead: two runs compared directly,
        computed when asked and stored nowhere."""
        params: Dict[str, Any] = {}
        if run_id:
            params["run_id"] = run_id
        if against:
            params["against"] = against
        return self._h("GET", f"/projects/{project_id}/changes", params=params or None)

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

    def webhook_deliveries(self, project_id: str, limit: int = 50) -> List[Json]:
        """A project's webhook deliveries, newest first: each one's event,
        status (queued, retrying, delivered or failed), attempts and last error."""
        out = self._h("GET", "/webhooks/deliveries", params={"projectId": project_id, "limit": limit})
        return list(out.get("deliveries") or [])

    def test_webhook(self, project_id: str) -> Json:
        """Queue a test ``run.finished`` message to the project's webhook URL,
        so the endpoint can be checked now. The API refuses it with 400 when no
        webhook URL is saved."""
        return self._h("POST", f"/projects/{project_id}/webhooks/test")

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

    def billing(self) -> Json:
        """The plan, this month's spend and what is left to spend
        (``month.remaining``, None for no limit), and the charges behind it."""
        return self._h("GET", "/me/billing")

    def monitor(self) -> Json:
        """What is queued and running in the workspace: the job queue, not ``arc.monitors``."""
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
