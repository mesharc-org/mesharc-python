"""The monitor tools and create_project's caps, against a stand-in client: what each sends, and what it answers."""
import pytest

mcp_mod = pytest.importorskip("mesharc.mcp", reason="needs the mcp extra")

from mesharc import MeshArcError  # noqa: E402


class _Monitors:
    def __init__(self, rows=None, total=None):
        self.made, self.calls, self._rows = [], [], rows or []
        self._total = len(self._rows) if total is None else total

    def create(self, kind, request, schedule, **kw):
        self.made.append((kind, request, schedule, kw))
        return {"id": "m1", "kind": kind, "schedule": schedule, "status": "active", "webhookSecret": "whsec_x"}

    def list(self, kind=None):
        self.calls.append(("list", kind))
        return [{"id": "m1", "kind": "search", "name": "acme", "schedule": "daily", "status": "active",
                 "request": {"query": "acme"}, "nextRunAt": "2026-10-15T00:00:00Z", "spentThisMonth": 8,
                 "budget": {"perRun": 0, "perMonth": 0},
                 "lastRun": {"status": "done", "changed": True, "summary": "2 new", "diff": {"new": ["x"] * 99}}}]

    def get(self, monitor_id):
        return {"id": monitor_id, "kind": "agent", "request": {"prompt": "p"}}

    def runs(self, monitor_id, limit=25):
        for i in range(100):
            yield {"id": f"r{i}", "status": "done", "changed": i == 0, "summary": "s", "credits": 3, "refId": f"a{i}",
                   "diff": {"big": "x" * 1000}, "error": ""}

    def pause(self, monitor_id):
        self.calls.append(("pause", monitor_id))
        return {"id": monitor_id, "status": "paused"}

    def resume(self, monitor_id):
        self.calls.append(("resume", monitor_id))
        return {"id": monitor_id, "status": "active"}

    def rows_page(self, monitor_id, active=None, q=None, offset=0, limit=100):
        self.calls.append(("rows", monitor_id, active, q, limit))
        return {"data": self._rows[:limit], "total": self._total}

    def row_events(self, monitor_id, run_id=None, kind=None, limit=200):
        return [{"kind": "added", "key": "Pro"}][:limit]


class _Projects:
    def __init__(self):
        self.created, self.updated = [], []

    def create(self, seed, name=None, schedule="manual", config=None):
        self.created.append((seed, config))
        return {"id": "p1", "seed": seed}

    def update(self, project_id, **fields):
        self.updated.append((project_id, fields))
        return {"id": project_id, **fields}


class _Kept:
    def keep(self, name=None, schedule=None):
        return {"id": "p9"}


class _Client:
    def __init__(self, monitors=None):
        self.monitors = monitors or _Monitors()
        self.projects = _Projects()

    def get_crawl(self, crawl_id):
        return _Kept()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


@pytest.fixture
def client(monkeypatch):
    c = _Client()
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    return c


def test_a_search_monitor_sends_its_query_news_and_monthly_cap_and_never_its_webhook_secret(client):
    out = mcp_mod.create_monitor("search", "daily", query="acme ltd", news=True, max_credits_per_month=200)
    kind, request, schedule, kw = client.monitors.made[0]
    assert (kind, request, schedule) == ("search", {"query": "acme ltd", "sources": ["news"]}, "daily")
    assert kw["budget"] == {"perMonth": 200} and kw["rows"] is None and kw["idempotency_key"].startswith("mcp-")
    assert "webhookSecret" not in out and out["id"] == "m1" and "first run has started" in out["note"]


def test_an_agent_monitor_stays_on_its_starting_sites_unless_told_and_can_follow_rows(client):
    mcp_mod.create_monitor("agent", "weekly", prompt="Acme's plans", urls=["https://www.Acme.test/pricing"],
                           follow_rows=True, max_credits=300)
    mcp_mod.create_monitor("agent", "weekly", prompt="p", urls=["https://acme.test/"], allowed_domains=[])
    first, second = client.monitors.made
    assert first[1] == {"prompt": "Acme's plans", "urls": ["https://www.Acme.test/pricing"],
                        "allowedDomains": ["acme.test"], "maxCredits": 300}
    assert first[3]["rows"] is True and first[3]["budget"] is None
    assert second[1]["allowedDomains"] == []


def test_a_monitor_with_the_wrong_inputs_for_its_kind_is_refused_before_any_call(client):
    answers = [mcp_mod.create_monitor("search", "daily", prompt="p"),
               mcp_mod.create_monitor("search", "daily", query="q", follow_rows=True),
               mcp_mod.create_monitor("agent", "daily", query="q"),
               mcp_mod.create_monitor("agent", "daily")]
    assert [a["code"] for a in answers] == ["validation"] * 4 and client.monitors.made == []


def test_the_same_monitor_asked_twice_in_ten_minutes_carries_one_key(client):
    mcp_mod.create_monitor("search", "daily", query="q")
    mcp_mod.create_monitor("search", "daily", query="q")
    mcp_mod.create_monitor("search", "hourly", query="q")
    keys = [m[3]["idempotency_key"] for m in client.monitors.made]
    assert keys[0] == keys[1] != keys[2]


def test_list_monitors_shows_what_picks_one_and_drops_the_request_and_the_diff(client):
    out = mcp_mod.list_monitors(kind="search")
    row = out["monitors"][0]
    assert client.monitors.calls == [("list", "search")]
    assert "request" not in row and row["lastRun"] == {"status": "done", "changed": True, "summary": "2 new"}
    assert row["spentThisMonth"] == 8


def test_one_monitor_comes_with_its_latest_runs_each_without_its_diff(client):
    out = mcp_mod.list_monitors(monitor_id="m1", runs=500)["monitor"]
    assert out["request"] == {"prompt": "p"} and len(out["runs"]) == 50
    assert out["runs"][0] == {"id": "r0", "status": "done", "changed": True, "summary": "s", "credits": 3,
                              "refId": "a0"}


def test_pause_monitor_pauses_and_resumes(client):
    assert mcp_mod.pause_monitor("m1")["status"] == "paused"
    assert mcp_mod.pause_monitor("m1", resume=True)["status"] == "active"
    assert client.monitors.calls == [("pause", "m1"), ("resume", "m1")]


def test_get_monitor_rows_keeps_its_answer_within_its_size_and_says_how_many_are_left(monkeypatch):
    rows = [{"key": f"k{i}", "attrs": {"text": "x" * 2000}} for i in range(100)]
    c = _Client(_Monitors(rows=rows, total=140))
    monkeypatch.setattr(mcp_mod, "_client", lambda: c)
    out = mcp_mod.get_monitor_rows("m1", active=False, q="pro", limit=9999, changes=True)
    assert c.monitors.calls[0] == ("rows", "m1", False, "pro", 500)
    assert 0 < len(out["rows"]) < 100 and out["total"] == 140
    assert out["note"].startswith(f"{len(out['rows'])} of 140 records shown")
    assert out["events"] == [{"kind": "added", "key": "Pro"}]


def test_get_monitor_rows_of_an_unknown_monitor_is_an_error_not_a_crash(monkeypatch):
    class _Gone(_Monitors):
        def rows_page(self, *a, **kw):
            raise MeshArcError(404, "monitor not found", "not_found")

    monkeypatch.setattr(mcp_mod, "_client", lambda: _Client(_Gone()))
    out = mcp_mod.get_monitor_rows("nope")
    assert (out["status"], out["code"]) == (404, "not_found")


def test_create_project_puts_its_caps_in_the_config_and_sets_them_on_a_kept_crawl(client):
    mcp_mod.create_project(seed="example.com", config={"max_pages": 50}, max_credits_per_run=100,
                           max_credits_per_month=0)
    assert client.projects.created == [("example.com", {"max_pages": 50, "max_credits_per_run": 100,
                                                        "max_credits_per_month": 0})]
    mcp_mod.create_project(seed="example.org")
    assert client.projects.created[1] == ("example.org", None)
    out = mcp_mod.create_project(crawl_id="c1", max_credits_per_month=500)
    assert client.projects.updated == [("p9", {"config": {"max_credits_per_month": 500}})] and out["id"] == "p9"
