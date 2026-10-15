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
from typing import Any, Dict, Iterable, Iterator, List, Literal, Optional, Union
from urllib.parse import parse_qs, urlparse

import httpx

__version__ = "0.6.0"
__all__ = ["MeshArc", "MeshArcError", "MeshArcTimeoutError", "Crawl", "AgentRun"]
TENANT_HEADER = "X-MeshArc-Tenant"

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
    # "cancelling" is a table whose rows are still stopping: not finished yet.
    return status in ("queued", "running", "cancelling")


def _cursor_of(next_url: str) -> str:
    return (parse_qs(urlparse(next_url).query).get("cursor") or [""])[0]


def _iso(when: Union[str, date]) -> str:
    """A date or date-time as the API reads it: ISO 8601, a string as given."""
    return when.isoformat() if isinstance(when, date) else when


def _paged(http: Any, path: str, params: Dict[str, Any]) -> Iterator[Json]:
    """Every row of a cursor-paged list: ``params`` on every request, and
    the cursor from each page's ``next`` link until there is none."""
    cursor = ""
    while True:
        page = http("GET", path, params={**params, **({"cursor": cursor} if cursor else {})})
        for row in page.get("data") or []:
            yield row
        cursor = _cursor_of(page["next"]) if page.get("next") else ""
        if not cursor:
            return


def _cancel_agent(http: Any, run_id: str) -> Json:
    return http("DELETE", f"/agent/{run_id}")


def _some(**fields: Any) -> Json:
    """The fields that are not None, as a request body or query: what was
    left out is left to the API's default (or, in a PATCH, left as it is)."""
    return {k: v for k, v in fields.items() if v is not None}


def _agent_body(prompt: str, urls: Optional[Iterable[str]], schema: Optional[Json], max_credits: Optional[int],
                max_steps: Optional[int], allowed_domains: Optional[Iterable[str]], webhook: Union[str, Json, None],
                connection_id: Optional[str], login_id: Optional[str], data_connections: Optional[Iterable[str]],
                proposals: Optional[bool], timeout_s: float, logins: Union[str, Iterable[str], None] = None) -> Json:
    """A POST /agent body (and a follow-up's) in the API's own names, naming only what was given."""
    body: Json = {"prompt": prompt}
    body.update(_some(schema=schema, maxCredits=max_credits, maxSteps=max_steps, webhook=webhook,
                      connectionId=connection_id, loginId=login_id, proposals=proposals, logins=_logins(logins)))
    if urls is not None:
        body["urls"] = list(urls)
    if allowed_domains is not None:
        body["allowedDomains"] = list(allowed_domains)
    if data_connections is not None:
        body["dataConnections"] = list(data_connections)
    if timeout_s > 0:
        body["timeout"] = min(timeout_s, 120)
    return body


def _logins(logins: Union[str, Iterable[str], None]) -> Union[str, List[str], None]:
    """``logins`` as the API takes it: "auto", "none", or a list of login ids."""
    return logins if logins is None or isinstance(logins, str) else list(logins)


def _offset_rows(http: Any, path: str, params: Dict[str, Any], limit: int) -> Iterator[Json]:
    """Every row of an offset-paged list, ``limit`` at a time, until a page
    comes back short."""
    offset = 0
    while True:
        page = http("GET", path, params={**params, "offset": offset, "limit": limit})
        rows = page.get("data") or []
        for row in rows:
            yield row
        if len(rows) < limit:
            return
        offset += len(rows)


def _download(http: Any, method: str, path: str, dest: str, **kw: Any) -> str:
    """Stream an API file to ``dest``. Returns ``dest``."""
    with http.stream(method, path, **kw) as r:
        if r.status_code >= 400:
            r.read()
            raise http._error(r)
        with open(dest, "wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
    return dest


class _Http:
    """The transport: one httpx client, bearer auth, JSON errors, retries."""

    def __init__(self, api_key: str, base_url: str, timeout: float, max_retries: int, tenant: str = "") -> None:
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": f"mesharc-python/{__version__}"}
        if tenant:
            # Every request acts on that sub-account of the key's workspace.
            headers[TENANT_HEADER] = tenant
        self._c = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", headers=headers, timeout=timeout)
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
               idempotency_key: Optional[str] = None, budget: Optional[Json] = None, alerts: Optional[Json] = None,
               add_to_project: Optional[str] = None, rows: Union[bool, Json, None] = None,
               destination: Optional[str] = None, sentiment: Union[Literal[False], Json, None] = None) -> Json:
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

        ``budget`` is ``{"perRun", "perMonth"}`` in credits (0 or left out:
        no cap): ``perRun`` lowers an agent monitor's ``maxCredits``, and a
        run due past ``perMonth`` is skipped, the webhook told
        ``budget.reached`` once a month. ``alerts`` is ``{"email",
        "chatUrl"}``: the workspace's run emails and a Slack, Discord, Teams
        or Google Chat incoming webhook. ``add_to_project`` (a search
        monitor's) adds its new results on that project's site to the
        project. ``rows`` (an agent monitor's) follows a list answer record
        by record -- ``True``, or ``{"path", "key", "confirmAfter"}`` -- read
        with ``rows()``; ``destination`` writes those records out after each
        run. ``sentiment`` (a news monitor's) is ``{"connectionId", "only"}``:
        each run's new results scored positive, neutral or negative by that
        LLM connection, on your key at 1 credit per 1,000 tokens.

        Spends credits and needs a key that can write. Returns the monitor.
        """
        body: Json = {"kind": kind, "request": request, "schedule": schedule}
        body.update(_some(name=name, webhook=webhook, baselineId=baseline_id, budget=budget, alerts=alerts,
                          addToProject=add_to_project, rows=rows, destination=destination, sentiment=sentiment))
        return self._h("POST", "/monitors", json=body, idempotency_key=idempotency_key)

    def list(self, kind: Optional[str] = None) -> List[Json]:
        """The workspace's monitors, newest first, each with its last run under
        ``lastRun``. ``kind`` keeps ``search`` or ``agent`` ones."""
        return self._h("GET", "/monitors", params={"kind": kind} if kind else None)["data"]

    def get(self, monitor_id: str) -> Json:
        """One monitor, with its last run."""
        return self._h("GET", f"/monitors/{monitor_id}")

    def update(self, monitor_id: str, name: Optional[str] = None, schedule: Optional[str] = None,
               status: Optional[str] = None, webhook: Union[str, Json, None] = None,
               budget: Optional[Json] = None, alerts: Optional[Json] = None, add_to_project: Optional[str] = None,
               rows: Union[bool, Json, None] = None, destination: Optional[str] = None,
               sentiment: Union[Literal[False], Json, None] = None) -> Json:
        """Change a monitor's ``name``, ``schedule``, ``status`` (``active`` or
        ``paused``), ``webhook`` or any setting ``create`` takes. Only the
        ones that are not None are sent; ``webhook=""`` removes the webhook,
        and a new one's secret comes back once, as ``webhookSecret``.
        ``budget``, ``alerts`` and ``sentiment`` are replaced whole (``{}``
        for none); ``add_to_project=""`` and ``destination=""`` remove them;
        a new ``rows`` path or key starts the records again. Needs a key that
        can write."""
        body = _some(name=name, schedule=schedule, status=status, webhook=webhook, budget=budget, alerts=alerts,
                     addToProject=add_to_project, rows=rows, destination=destination, sentiment=sentiment)
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
        yield from _paged(self._h, f"/monitors/{monitor_id}/runs", {"limit": limit})

    def delete(self, monitor_id: str) -> Json:
        """The monitor and its runs. The searches and agent runs it made stay
        in the workspace's history; an agent run it has under way is
        stopped. Returns ``{id, deleted: true}``."""
        return self._h("DELETE", f"/monitors/{monitor_id}")

    def overview(self, kind: Optional[str] = None) -> Json:
        """Everything on a schedule -- projects and monitors -- as the Monitor
        screen lists it: each with its next and last run, this month's
        spend, an estimate of the month and its caps; ``counts`` by kind.
        ``kind`` keeps ``crawl``, ``search`` or ``agent``."""
        return self._h("GET", "/monitors/overview", params=_some(kind=kind) or None)

    def rows(self, monitor_id: str, active: Optional[bool] = None, q: Optional[str] = None,
             limit: int = 100) -> Iterator[Json]:
        """The records an agent monitor's answers list, as they stand, each
        with the page every value was read on. ``active=False`` lists the
        records gone; ``q`` keeps those holding it. ``limit`` is the page
        size (1,000 at most)."""
        params = _some(active=None if active is None else str(bool(active)).lower(), q=q)
        yield from _offset_rows(self._h, f"/monitors/{monitor_id}/rows", params, limit)

    def rows_page(self, monitor_id: str, active: Optional[bool] = None, q: Optional[str] = None,
                  offset: int = 0, limit: int = 100) -> Json:
        """One page of ``rows()``, as the API answers it: ``{data, total}``,
        ``total`` counting every record that matches."""
        params = _some(active=None if active is None else str(bool(active)).lower(), q=q)
        return self._h("GET", f"/monitors/{monitor_id}/rows", params={**params, "offset": offset, "limit": limit})

    def row(self, monitor_id: str, key: str) -> Json:
        """One record: what it is now, the page each value was read on, and every event that touched it."""
        return self._h("GET", f"/monitors/{monitor_id}/rows/{key}")

    def row_events(self, monitor_id: str, run_id: Optional[str] = None, kind: Optional[str] = None,
                   limit: int = 200) -> List[Json]:
        """What moved, newest first: records added, changed (one entry per
        column, both values), gone and withheld. ``run_id`` keeps one
        monitor run's; ``kind`` one kind."""
        return self._h("GET", f"/monitors/{monitor_id}/rows/events",
                       params={"limit": limit, **_some(run_id=run_id, kind=kind)})["data"]

    def export_rows(self, monitor_id: str, path: str, dataset: str = "rows", fmt: str = "csv") -> str:
        """Its records (``rows``) or everything that moved (``row-events``)
        to ``path``, as csv or jsonl. A CSV cell that would start a formula
        is written as text. Returns ``path``."""
        return _download(self._h, "GET", f"/monitors/{monitor_id}/rows/export", path,
                         params={"dataset": dataset, "format": fmt})


class _AgentBatches:
    """The agent over every row of a table: ``arc.agent_batches``.

    One agent run a row, its prompt (and starting URLs) filled from the
    row's columns, written ``{{column}}``, and its answer the row's new
    columns. Each row pays only for what it uses, up to
    ``max_credits_per_row``; the batch never spends more than rows x that
    cap, or the balance when that is less -- a row it cannot afford is
    skipped, free.
    """

    def __init__(self, http: _Http) -> None:
        self._h = http

    @staticmethod
    def _body(prompt: str, csv: Optional[str], rows: Optional[List[Json]], max_credits_per_row: Optional[int],
              urls: Optional[Iterable[str]], schema: Optional[Json], max_steps: Optional[int],
              allowed_domains: Optional[Iterable[str]], concurrency: Optional[int], name: Optional[str],
              connection_id: Optional[str], login_id: Optional[str],
              data_connections: Optional[Iterable[str]], logins: Union[str, Iterable[str], None] = None) -> Json:
        body: Json = {"prompt": prompt}
        body.update(_some(csv=csv, rows=rows, maxCreditsPerRow=max_credits_per_row, schema=schema,
                          maxSteps=max_steps, concurrency=concurrency, name=name, connectionId=connection_id,
                          loginId=login_id, logins=_logins(logins)))
        if urls is not None:
            body["urls"] = list(urls)
        if allowed_domains is not None:
            body["allowedDomains"] = list(allowed_domains)
        if data_connections is not None:
            body["dataConnections"] = list(data_connections)
        return body

    def preview(self, prompt: str, csv: Optional[str] = None, rows: Optional[List[Json]] = None,
                max_credits_per_row: Optional[int] = None, urls: Optional[Iterable[str]] = None,
                schema: Optional[Json] = None, max_steps: Optional[int] = None,
                allowed_domains: Optional[Iterable[str]] = None, concurrency: Optional[int] = None,
                name: Optional[str] = None, connection_id: Optional[str] = None, login_id: Optional[str] = None,
                data_connections: Optional[Iterable[str]] = None, logins: Union[str, Iterable[str], None] = None) -> Json:
        """What ``start`` would do, without starting it or spending anything:
        the rows that can run and those that cannot (and why), the first
        prompts as their rows will ask them, and an estimate against the
        balance."""
        return self._h("POST", "/agent/batch/preview", json=self._body(
            prompt, csv, rows, max_credits_per_row, urls, schema, max_steps, allowed_domains, concurrency, name,
            connection_id, login_id, data_connections, logins))

    def start(self, prompt: str, csv: Optional[str] = None, rows: Optional[List[Json]] = None,
              max_credits_per_row: Optional[int] = None, urls: Optional[Iterable[str]] = None,
              schema: Optional[Json] = None, max_steps: Optional[int] = None,
              allowed_domains: Optional[Iterable[str]] = None, concurrency: Optional[int] = None,
              name: Optional[str] = None, connection_id: Optional[str] = None, login_id: Optional[str] = None,
              data_connections: Optional[Iterable[str]] = None, webhook: Union[str, Json, None] = None,
              idempotency_key: Optional[str] = None, logins: Union[str, Iterable[str], None] = None) -> Json:
        """Start one agent run a row of the table.

        The table is ``csv`` (its first line names the columns) or ``rows``,
        a list of objects. ``prompt`` and ``urls`` name columns as
        ``{{column}}``; ``schema`` (type ``object``) gives the answer's
        columns. ``max_credits_per_row`` is required by the API. Without
        ``allowed_domains`` each row is held to its own URLs' sites; ``[]``
        lifts the limit. ``concurrency`` rows run at once (3 by default).
        ``logins``, ``login_id`` and ``data_connections`` are as ``agent()``
        takes them, for every row. ``webhook`` is told ``agent.batch.finished`` once; its
        secret comes back once, as ``webhookSecret``.

        Spends credits and needs a key that can write. Returns the batch,
        with ``counts`` by row status; ``wait()`` waits for it.
        """
        body = self._body(prompt, csv, rows, max_credits_per_row, urls, schema, max_steps, allowed_domains,
                          concurrency, name, connection_id, login_id, data_connections, logins)
        if webhook is not None:
            body["webhook"] = webhook
        return self._h("POST", "/agent/batch", json=body, idempotency_key=idempotency_key)

    def get(self, batch_id: str, offset: int = 0, limit: int = 50, status: Optional[str] = None) -> Json:
        """A batch and one page of its rows in table order (200 at most), each
        with its status, answer, sources, credits and run. ``status`` keeps
        one status. Raises ``MeshArcError`` 410 (``expired``) past its keep
        date."""
        return self._h("GET", f"/agent/batch/{batch_id}",
                       params={"offset": offset, "limit": limit, **_some(status=status)})

    def rows(self, batch_id: str, status: Optional[str] = None, limit: int = 200) -> Iterator[Json]:
        """Every row of a batch, in table order; ``status`` keeps one status."""
        offset = 0
        while True:
            page = self.get(batch_id, offset=offset, limit=limit, status=status)
            rows = page.get("data") or []
            for row in rows:
                yield row
            if not page.get("more") or not rows:
                return
            offset += len(rows)

    def wait(self, batch_id: str, poll: float = 5.0, timeout: float = 3600) -> Json:
        """Block until every row has ended. Returns the batch (its first rows under ``data``)."""
        deadline = time.time() + timeout
        while True:
            batch = self.get(batch_id, limit=1)
            if not _running(batch.get("status")):
                return batch
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"agent batch {batch_id} is still {batch.get('status')} after {timeout}s",
                                          batch_id)
            time.sleep(poll)
            self._h.pace()

    def list(self, limit: int = 25) -> Iterator[Json]:
        """The workspace's batches, newest first, without their rows."""
        yield from _paged(self._h, "/agent/batch", {"limit": limit})

    def cancel(self, batch_id: str) -> Json:
        """Stop it: rows not started are cancelled, running ones stop before
        their next step. What rows used stays charged; their answers are kept."""
        return self._h("DELETE", f"/agent/batch/{batch_id}")

    def export(self, batch_id: str, path: str, fmt: str = "csv") -> str:
        """The table as sent, each answer value a new column, then each row's
        credits, sources, error and run, to ``path`` as csv or jsonl. A CSV
        cell that would start a formula is written as text. Returns ``path``."""
        return _download(self._h, "GET", f"/agent/batch/{batch_id}/export", path, params={"format": fmt})


class _Triggers:
    """A change or a new result starts work: ``arc.triggers``."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    def create(self, source: Json, action: Json, when: Optional[Json] = None, limits: Optional[Json] = None,
               name: Optional[str] = None, status: Optional[str] = None) -> Json:
        """Start work when a source matches.

        ``source`` is ``{"kind": "project" | "monitor", "id"}``. ``when`` is
        a project's ``{kinds, paths, fields, minWords, significant}`` or a
        monitor's ``{events}``. ``action`` is ``{"type": "agent", "prompt",
        ...}`` (a template: ``{{url}}``, ``{{before}}``, ``{{after}}``,
        ``{{changes}}``, ``{{source}}``), ``{"type": "recrawl"}``, or
        ``{"type": "monitor" | "recheck", "monitorId"}``. ``limits`` is
        ``{cooldownMinutes, maxPerDay, perMonth}``. It looks from now on.
        Needs a key that can write."""
        body: Json = {"source": source, "action": action}
        body.update(_some(when=when, limits=limits, name=name, status=status))
        return self._h("POST", "/triggers", json=body)

    def list(self, source_id: Optional[str] = None, target_id: Optional[str] = None) -> List[Json]:
        """The workspace's triggers, newest first; ``source_id`` keeps one
        project's or monitor's, ``target_id`` those whose action runs a monitor."""
        return self._h("GET", "/triggers", params=_some(source_id=source_id, target_id=target_id) or None)["data"]

    def get(self, trigger_id: str) -> Json:
        return self._h("GET", f"/triggers/{trigger_id}")

    def update(self, trigger_id: str, name: Optional[str] = None, when: Optional[Json] = None,
               action: Optional[Json] = None, limits: Optional[Json] = None, status: Optional[str] = None) -> Json:
        """Its name, condition, action, limits or status (``active`` /
        ``paused``); only what is not None is sent. Resumed, it looks from then on."""
        return self._h("PATCH", f"/triggers/{trigger_id}",
                       json=_some(name=name, when=when, action=action, limits=limits, status=status))

    def delete(self, trigger_id: str) -> Json:
        """The trigger and its firing history. What it started stays."""
        return self._h("DELETE", f"/triggers/{trigger_id}")

    def fires(self, trigger_id: str, limit: int = 25) -> Iterator[Json]:
        """Its firing history, newest first: what each source run matched, and
        what it started or why it was skipped. Kept 90 days."""
        yield from _paged(self._h, f"/triggers/{trigger_id}/fires", {"limit": limit})

    def preview(self, trigger_id: str) -> Json:
        """What the source's latest run would make of it -- what matches, and
        an agent action's prompt as it would be sent. Fires nothing."""
        return self._h("POST", f"/triggers/{trigger_id}/preview")


class _Proposals:
    """What agents proposed to watch, for a member to approve: ``arc.proposals``."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    def list(self, status: str = "proposed", run_id: Optional[str] = None) -> List[Json]:
        """Newest first: those waiting (``proposed``, the default), the
        ``approved``, or all (``""``); ``run_id`` keeps one agent run's."""
        return self._h("GET", "/proposals", params={"status": status, **_some(run_id=run_id)})["data"]

    def approve(self, proposal_id: str) -> Json:
        """Make what was proposed: the project, or the monitor, whose first
        run starts now. Returns ``{proposal, made: {kind, id}}``. Raises
        ``MeshArcError`` 409 when it was decided already, 402 at the project
        limit or with no credits left."""
        return self._h("POST", f"/proposals/{proposal_id}/approve")

    def dismiss(self, proposal_id: str) -> Json:
        """Not wanted: the proposal is deleted."""
        return self._h("POST", f"/proposals/{proposal_id}/dismiss")


class _SubAccounts:
    """Workspaces this one makes for its own customers: ``arc.sub_accounts``.

    Each has its own keys, projects and data, spends this workspace's
    credits under a cap of its own, and is on this workspace's plan (Scale
    and Enterprise). Acting inside one: ``arc.as_sub_account(id)``. Every
    call needs an admin key.
    """

    def __init__(self, http: _Http) -> None:
        self._h = http

    def list(self) -> Json:
        """``{data, allowed, spent, remaining}``: each sub-account with this
        month's spend, how many the plan allows, and what they spent in all."""
        return self._h("GET", "/orgs/children")

    def create(self, name: str, monthly_cap: Optional[int] = None, external_id: Optional[str] = None,
               idempotency_key: Optional[str] = None) -> Json:
        """A sub-account; ``monthly_cap`` in credits (0 or None: no cap of
        its own), ``external_id`` your own id for it, echoed back."""
        return self._h("POST", "/orgs/children", idempotency_key=idempotency_key,
                       json={"name": name, **_some(monthlyCap=monthly_cap, externalId=external_id)})

    def get(self, child_id: str) -> Json:
        """One sub-account: its month, its spend by kind, its latest charges,
        and which of your keys acted on it these 30 days."""
        return self._h("GET", f"/orgs/children/{child_id}")

    def update(self, child_id: str, name: Optional[str] = None, monthly_cap: Optional[int] = None,
               suspended: Optional[bool] = None, reason: Optional[str] = None) -> Json:
        """Rename it, set its cap (0: none), or suspend it -- which refuses its
        keys and cancels what it has running -- and lift that."""
        return self._h("PATCH", f"/orgs/children/{child_id}",
                       json=_some(name=name, monthlyCap=monthly_cap, suspended=suspended, reason=reason))

    def keys(self, child_id: str) -> List[Json]:
        return self._h("GET", f"/orgs/children/{child_id}/keys")

    def create_key(self, child_id: str, name: str = "default", scopes: Optional[List[str]] = None,
                   expires_in_days: Optional[int] = None, rpm: Optional[int] = None) -> Json:
        """A key of the sub-account's own, for your customer: it reaches that
        workspace and nothing else. The plaintext is under ``key``, once."""
        return self._h("POST", f"/orgs/children/{child_id}/keys",
                       json={"name": name, **_some(scopes=scopes, expires_in_days=expires_in_days, rpm=rpm)})

    def revoke_key(self, child_id: str, key_id: str) -> None:
        self._h("DELETE", f"/orgs/children/{child_id}/keys/{key_id}")


class _Connections:
    """The workspace's connections -- destinations, models and data
    providers: ``arc.connections``. Secrets are sent, never returned.
    Creating, changing, testing and deleting need an admin key."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    def providers(self) -> Json:
        """The catalogue: each provider's kind, fields, secrets and note."""
        return self._h("GET", "/connections/providers")

    def list(self) -> List[Json]:
        return self._h("GET", "/connections")["connections"]

    def create(self, provider: str, name: Optional[str] = None, config: Optional[Json] = None,
               secrets: Optional[Json] = None) -> Json:
        """A connection: ``provider`` from ``providers()`` (``"pdl"`` and
        ``"fmp"`` are the data providers the agent reads, with
        ``secrets={"api_key": ...}``). Test it with ``test()``."""
        return self._h("POST", "/connections", json={"provider": provider, **_some(name=name, config=config,
                                                                                    secrets=secrets)})

    def get(self, connection_id: str) -> Json:
        return self._h("GET", f"/connections/{connection_id}")

    def update(self, connection_id: str, name: Optional[str] = None, config: Optional[Json] = None,
               secrets: Optional[Json] = None) -> Json:
        return self._h("PATCH", f"/connections/{connection_id}", json=_some(name=name, config=config, secrets=secrets))

    def delete(self, connection_id: str) -> None:
        self._h("DELETE", f"/connections/{connection_id}")

    def test(self, connection_id: str) -> Json:
        """Reach the provider with the stored credentials: ``{status, error, detail}``."""
        return self._h("POST", f"/connections/{connection_id}/test")


class _Logins:
    """The workspace's saved logins: ``arc.logins``. A login is saved in the
    app, from a live browser; here it is listed, renamed, expired, tested
    and deleted. Its session is never returned. Every call needs an admin key."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    def list(self) -> Json:
        """``{data, enabled, why}``: each login's site, status and use, and
        whether logins are switched on where this API runs."""
        return self._h("GET", "/logins")

    def update(self, login_id: str, name: Optional[str] = None, expired: Optional[bool] = None) -> Json:
        """Rename it; ``expired=True`` stops using it now."""
        return self._h("PATCH", f"/logins/{login_id}", json=_some(name=name, expired=expired))

    def delete(self, login_id: str) -> None:
        """Delete it: projects and runs that name it stop, saying so."""
        self._h("DELETE", f"/logins/{login_id}")

    def test(self, login_id: str) -> Json:
        """One plain fetch of the site's front page with it: ``{status, finalUrl, loginPage, reason}``. Free."""
        return self._h("POST", f"/logins/{login_id}/test")


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
        """What the answer rests on: pages, as ``{url, title, pageId}``, and
        data providers' records, as ``{url, title, provider}`` -- ``url`` the
        request it came from."""
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

    @property
    def follows_run_id(self) -> Optional[str]:
        """The message this one follows in its conversation (``follow_up()``); None for its first."""
        return self.envelope.get("followsRunId") or None

    @property
    def followed_by(self) -> Optional[str]:
        """The follow-up asked after this message; None until one is."""
        return self.envelope.get("followedBy") or None

    def refresh(self) -> AgentRun:
        """Read the run as it stands now. Returns the handle."""
        self.envelope = self._h("GET", f"/agent/{self.id}")
        return self

    def wait(self, poll: float = 2.0, timeout: float = 3600) -> Json:
        """Block until the run finishes. Returns the envelope.

        A run that is ``done``, ``cancelled`` or stopped at ``credit_limit``
        (with what it had under ``data.partial``) is returned. Any other end
        -- ``error``, or a status this client does not know -- raises
        ``MeshArcError`` 502 ``job_failed`` with the API's text, as the Node
        client does.
        """
        deadline = time.time() + timeout
        while True:
            status = self.refresh().status
            if status in ("done", "cancelled", "credit_limit"):
                return self.envelope
            if not _running(status):
                raise MeshArcError(502, self.envelope.get("error") or f"agent {status}", "job_failed")
            if time.time() >= deadline:
                raise MeshArcTimeoutError(f"agent {self.id} is still {status} after {timeout}s", self.id)
            time.sleep(poll)
            self._h.pace()

    def cancel(self) -> Json:
        """Stop the run. Returns the API's answer, ``{id, status}``: a queued
        run is ``cancelled``, a running one ``cancelling`` (it stops before
        its next step), a finished one keeps its status. ``refresh()``
        reads where it ended."""
        return _cancel_agent(self._h, self.id)

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

    def follow_up(self, prompt: str, urls: Optional[Iterable[str]] = None, schema: Optional[Json] = None,
                  max_credits: Optional[int] = None, max_steps: Optional[int] = None,
                  allowed_domains: Optional[Iterable[str]] = None, webhook: Union[str, Json, None] = None,
                  connection_id: Optional[str] = None, login_id: Optional[str] = None,
                  data_connections: Optional[Iterable[str]] = None, proposals: Optional[bool] = None,
                  timeout_s: float = 0, idempotency_key: Optional[str] = None,
                  logins: Union[str, Iterable[str], None] = None) -> AgentRun:
        """Ask a follow-up in this run's conversation; returns its ``AgentRun``.

        It answers after everything asked and found in the conversation
        before it, and may read again, free, any page the conversation read.
        Its request is its own, as ``agent()`` takes one, and it pays for its
        own work. This run must be the conversation's latest message, and
        finished: ``MeshArcError`` 409 (``conflict``) otherwise -- the error
        names the latest -- or past 30 messages; 410 (``expired``) past its
        keep date.
        """
        body = _agent_body(prompt, urls, schema, max_credits, max_steps, allowed_domains, webhook, connection_id,
                           login_id, data_connections, proposals, timeout_s, logins)
        return AgentRun(self._h, self._h("POST", f"/agent/{self.id}/follow-up", json=body,
                                         idempotency_key=idempotency_key))

    def trace(self, after: int = 0, follow: bool = True, poll: float = 2.0,
              timeout: float = 3600) -> Iterator[Json]:
        """What the agent did, step by step, as ``{seq, t, kind, text, ...}``.

        ``kind`` is start, resume, continue, model, search, fetch, render,
        map, select, extract, lookup (a data provider's), propose, tool,
        busy or finish. ``after`` is the ``seq``
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
    test environments; the hosted API needs nothing there. ``tenant`` is
    the id of one of the workspace's sub-accounts: every request then acts
    on it (``as_sub_account`` makes such a client from this one).
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT, max_retries: int = DEFAULT_MAX_RETRIES,
                 tenant: Optional[str] = None) -> None:
        key = api_key or os.environ.get("MESHARC_API_KEY") or ""
        if not key:
            raise ValueError("An API key is required: MeshArc('mesharc_...') or set MESHARC_API_KEY.")
        base = base_url or os.environ.get("MESHARC_API_URL") or DEFAULT_BASE
        # What as_sub_account() makes its client with.
        self._key, self._base, self._timeout, self._max_retries = key, base, timeout, max_retries
        self.tenant: str = tenant or ""
        self._h = _Http(key, base, timeout, max_retries, tenant=self.tenant)
        self.projects = _Projects(self._h)
        self.runs = _Runs(self._h)
        self.monitors = _Monitors(self._h)
        self.agent_batches = _AgentBatches(self._h)
        self.triggers = _Triggers(self._h)
        self.proposals = _Proposals(self._h)
        self.sub_accounts = _SubAccounts(self._h)
        self.connections = _Connections(self._h)
        self.logins = _Logins(self._h)

    def as_sub_account(self, sub_account_id: str) -> "MeshArc":
        """A client on the same key that acts on one of this workspace's
        sub-accounts: each request carries ``X-MeshArc-Tenant``, and is the
        sub-account's, with the key's own scopes and rate limit. Only a key
        of the sub-account's parent, not held to some projects, may do this;
        any other id is a 404. Each use is logged. Close it when done."""
        return MeshArc(self._key, self._base, self._timeout, self._max_retries, tenant=sub_account_id)

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
        yield from _paged(self._h, "/search", {"limit": limit, **({"q": q} if q else {})})

    def agent(self, prompt: str, urls: Optional[Iterable[str]] = None, schema: Optional[Json] = None,
              max_credits: Optional[int] = None, max_steps: Optional[int] = None,
              allowed_domains: Optional[Iterable[str]] = None, webhook: Union[str, Json, None] = None,
              connection_id: Optional[str] = None, timeout_s: float = 0,
              idempotency_key: Optional[str] = None, login_id: Optional[str] = None,
              data_connections: Optional[Iterable[str]] = None, proposals: Optional[bool] = None,
              logins: Union[str, Iterable[str], None] = None) -> AgentRun:
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

        The workspace's saved logins need no picking: the agent reads pages
        signed out, as the public sees them, and signs in page by page
        where a page needs the account, with the login for that page's
        site -- so one run can read a site's public pages signed out and
        its account pages signed in. ``logins`` says which it may use --
        ``"none"``, or a list of login ids to keep it to; left out, the
        API's ``"auto"``, every one -- and ``login_id`` is the older form
        of a list of one.
        ``data_connections`` names up to two of the workspace's data
        providers (one per provider): the agent may then look up company
        data (People Data Labs) or financial data (Financial Modeling Prep)
        on your own key, at no MeshArc credits, each record a source.
        ``proposals=True`` lets it propose things to watch, which wait in
        ``arc.proposals`` for a member to approve.

        Returns an ``AgentRun`` at once, queued or running, unless
        ``timeout_s`` asks the API to hold the request for the answer (120
        at most); ``AgentRun.wait()`` waits for it. ``AgentRun.follow_up()``
        asks the next question in the same conversation.
        """
        body = _agent_body(prompt, urls, schema, max_credits, max_steps, allowed_domains, webhook, connection_id,
                           login_id, data_connections, proposals, timeout_s, logins)
        return AgentRun(self._h, self._h("POST", "/agent", json=body, idempotency_key=idempotency_key))

    def conversations(self, q: Optional[str] = None, limit: int = 25) -> Iterator[Json]:
        """The workspace's agent conversations, the latest message first, each
        ``{id, title, messages, status, lastRunId, creditsUsed, ...}``; ``q``
        keeps those whose first question holds it. Kept 7 days after the
        last message. A table's rows are not listed."""
        yield from _paged(self._h, "/agent/conversations", {"limit": limit, **_some(q=q)})

    def conversation(self, thread_id: str) -> Json:
        """One conversation with every run in it, oldest first under ``runs``,
        each as ``get_agent`` reads one. Raises ``MeshArcError`` 410 past its
        keep date."""
        return self._h("GET", f"/agent/conversations/{thread_id}")

    def estimate(self, kind: str, schedule: str, project_id: Optional[str] = None,
                 monitor_id: Optional[str] = None, max_pages: Optional[int] = None, max_tier: Optional[str] = None,
                 max_credits: Optional[int] = None, month_cap: Optional[int] = None) -> Json:
        """About how many credits scheduled work costs a month, before it is
        saved: a ``crawl`` (``project_id``, or ``max_pages`` and ``max_tier``
        for one not made yet), a ``search`` or an ``agent`` (``monitor_id``,
        or ``max_credits``), on an ``hourly``, ``daily`` or ``weekly``
        schedule, held to ``month_cap``. ``upTo`` says the figure is the most
        a run could cost, nothing having run yet."""
        return self._h("GET", "/estimate", params={"kind": kind, "schedule": schedule, **_some(
            projectId=project_id, monitorId=monitor_id, maxPages=max_pages, maxTier=max_tier,
            maxCredits=max_credits, monthCap=month_cap)})

    def get_agent(self, run_id: str) -> AgentRun:
        """A handle on an agent run started earlier or elsewhere.

        A run past its keep date (7 days by default) is gone: the API
        answers 410 and this raises ``MeshArcError`` with status 410 and
        code ``expired``.
        """
        return AgentRun(self._h, self._h("GET", f"/agent/{run_id}"))

    def cancel_agent(self, run_id: str) -> Json:
        """Stop an agent run by its id, with no handle needed: what
        ``AgentRun.cancel()`` does. Returns ``{id, status}``: ``cancelled``
        for a queued run, ``cancelling`` for a running one (it stops before
        its next step), a finished run's own status otherwise -- one past its
        keep date included. Pages and model tokens it used stay charged."""
        return _cancel_agent(self._h, run_id)

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
        yield from _paged(self._h, "/agent", {"limit": limit, **filters})

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
            return _download(self._h, "POST", f"/projects/{project_id}/export", path,
                             json={"dataset": dataset, "format": fmt, "run_id": run_id, "urls": list(urls)})
        params: Dict[str, Any] = {"dataset": dataset, "format": fmt}
        if run_id:
            params["run_id"] = run_id
        return _download(self._h, "GET", f"/projects/{project_id}/export", path, params=params)

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
