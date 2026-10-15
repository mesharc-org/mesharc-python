"""The client's follow-ups, tables, monitors in full, triggers, proposals, estimate, sub-accounts,
connections and saved logins, against a scripted transport: no network, no key."""

import json

import httpx
import pytest

from mesharc import TENANT_HEADER, MeshArc, MeshArcError, MeshArcTimeoutError, __version__

from test_client import scripted


def body(req):
    return json.loads(req.content) if req.content else None


def run(**over):
    return {"id": "r1", "kind": "agent", "status": "queued", **over}


# -- the agent ---------------------------------------------------------------------------

def test_an_agent_run_names_its_login_data_providers_and_proposals_only_when_given():
    arc = scripted(run(), run(id="r2"))
    arc.agent("p")
    arc.agent("p", login_id="L1", data_connections=("c1", "c2"), proposals=True)
    assert body(arc.calls[0]) == {"prompt": "p"}
    assert body(arc.calls[1]) == {"prompt": "p", "loginId": "L1", "dataConnections": ["c1", "c2"], "proposals": True}


def test_logins_need_no_picking_and_are_sent_only_to_change_that():
    arc = scripted(run(), run(id="r2"), run(id="r3"), {"valid": 1})
    arc.agent("p")
    arc.agent("p", logins="none")
    arc.agent("p", logins=(i for i in ["l1", "l2"]))
    arc.agent_batches.preview("about {{co}}", csv="co\nAcme\n", max_credits_per_row=10, logins=["l1"])
    assert "logins" not in body(arc.calls[0])
    assert [body(c)["logins"] for c in arc.calls[1:]] == ["none", ["l1", "l2"], ["l1"]]


def test_a_follow_up_posts_its_own_request_to_the_runs_conversation():
    arc = scripted(run(status="done"), run(id="r2", followsRunId="r1"))
    first = arc.get_agent("r1")
    nxt = first.follow_up("and their prices?", max_credits=300, data_connections=["c1"], timeout_s=500,
                          idempotency_key="f-1")
    req = arc.calls[1]
    assert (req.method, req.url.path, req.headers["Idempotency-Key"]) == ("POST", "/api/v1/agent/r1/follow-up", "f-1")
    assert body(req) == {"prompt": "and their prices?", "maxCredits": 300, "dataConnections": ["c1"], "timeout": 120}
    assert (nxt.id, nxt.envelope["followsRunId"]) == ("r2", "r1")
    assert (nxt.follows_run_id, nxt.followed_by, first.follows_run_id) == ("r1", None, None)


def test_a_follow_up_of_a_message_that_is_not_the_latest_is_a_409():
    arc = scripted(run(status="done"), (409, {"error": "this is not the conversation's latest message; follow run r3",
                                              "code": "conflict"}))
    with pytest.raises(MeshArcError) as exc:
        arc.get_agent("r1").follow_up("x")
    assert (exc.value.status, exc.value.code) == (409, "conflict")


def test_conversations_follow_the_cursor_and_keep_the_filter():
    arc = scripted({"data": [{"id": "t1"}], "next": "/api/v1/agent/conversations?limit=1&cursor=1&q=acme"},
                   {"data": [{"id": "t2"}], "next": None}, {"id": "t1", "runs": []})
    assert [c["id"] for c in arc.conversations(q="acme", limit=1)] == ["t1", "t2"]
    assert dict(arc.calls[1].url.params) == {"limit": "1", "q": "acme", "cursor": "1"}
    assert arc.conversation("t1") == {"id": "t1", "runs": []}
    assert arc.calls[2].url.path == "/api/v1/agent/conversations/t1"


# -- tables --------------------------------------------------------------------------------

def test_a_table_is_previewed_and_started_with_the_apis_names():
    arc = scripted({"valid": 2}, {"id": "b1", "status": "queued", "webhookSecret": "whsec"})
    kw = dict(csv="co,site\nAcme,acme.test\n", max_credits_per_row=50, urls=["{{site}}"], allowed_domains=[],
              data_connections=["c1"], login_id="L1")
    arc.agent_batches.preview("about {{co}}", **kw)
    out = arc.agent_batches.start("about {{co}}", webhook="https://hook.test/", idempotency_key="b-1", **kw)
    want = {"prompt": "about {{co}}", "csv": "co,site\nAcme,acme.test\n", "maxCreditsPerRow": 50,
            "urls": ["{{site}}"], "allowedDomains": [], "dataConnections": ["c1"], "loginId": "L1"}
    assert (arc.calls[0].url.path, body(arc.calls[0])) == ("/api/v1/agent/batch/preview", want)
    assert (arc.calls[1].url.path, body(arc.calls[1])) == ("/api/v1/agent/batch", {**want, "webhook": "https://hook.test/"})
    assert arc.calls[1].headers["Idempotency-Key"] == "b-1"
    assert out["webhookSecret"] == "whsec"


def test_a_tables_rows_are_read_page_by_page_until_there_is_no_more():
    arc = scripted({"id": "b1", "data": [{"row": 0}, {"row": 1}], "more": "/api/v1/agent/batch/b1?offset=2"},
                   {"id": "b1", "data": [{"row": 2}]})
    assert [r["row"] for r in arc.agent_batches.rows("b1", status="done", limit=2)] == [0, 1, 2]
    assert dict(arc.calls[1].url.params) == {"offset": "2", "limit": "2", "status": "done"}


def test_waiting_for_a_table_polls_until_it_ends_and_times_out_plainly(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    arc = scripted({"id": "b1", "status": "running"}, {"id": "b1", "status": "done"})
    assert arc.agent_batches.wait("b1", poll=0)["status"] == "done"
    # A table being cancelled still has rows stopping: it is waited for until it has ended.
    arc = scripted({"id": "b1", "status": "cancelling"}, {"id": "b1", "status": "cancelled"})
    assert arc.agent_batches.wait("b1", poll=0)["status"] == "cancelled"
    assert len(arc.calls) == 2
    arc = scripted({"id": "b1", "status": "running"})
    with pytest.raises(MeshArcTimeoutError) as exc:
        arc.agent_batches.wait("b1", poll=0, timeout=0)
    assert exc.value.job_id == "b1"


def test_an_expired_table_is_a_410_and_a_table_downloads_to_a_file(tmp_path):
    arc = scripted((410, {"error": "past its keep date", "code": "expired"}))
    with pytest.raises(MeshArcError) as exc:
        arc.agent_batches.get("b1")
    assert (exc.value.status, exc.value.code) == (410, "expired")
    arc = scripted()
    arc._h._c = httpx.Client(base_url="https://api.mesharc.dev/api/v1", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"co,plan\nAcme,Pro\n")))
    path = arc.agent_batches.export("b1", str(tmp_path / "t.csv"))
    assert open(path, "rb").read() == b"co,plan\nAcme,Pro\n"


def test_cancelling_and_listing_tables():
    arc = scripted({"id": "b1", "status": "cancelling"}, {"data": [{"id": "b1"}], "next": None})
    assert arc.agent_batches.cancel("b1")["status"] == "cancelling"
    assert arc.calls[0].method == "DELETE"
    assert [b["id"] for b in arc.agent_batches.list()] == ["b1"]


# -- monitors --------------------------------------------------------------------------------

def test_a_monitor_is_made_and_changed_with_its_caps_alerts_records_and_tone():
    arc = scripted({"id": "m1"}, {"id": "m1"})
    arc.monitors.create("agent", {"prompt": "p"}, "daily", budget={"perRun": 100, "perMonth": 2000},
                        alerts={"email": True}, rows={"path": "plans", "key": "name"}, destination="d1")
    arc.monitors.update("m1", budget={}, add_to_project="", sentiment=False)
    assert body(arc.calls[0]) == {"kind": "agent", "request": {"prompt": "p"}, "schedule": "daily",
                                  "budget": {"perRun": 100, "perMonth": 2000}, "alerts": {"email": True},
                                  "rows": {"path": "plans", "key": "name"}, "destination": "d1"}
    # {} and "" and False are sent: they clear a setting. Only None is left out.
    assert body(arc.calls[1]) == {"budget": {}, "addToProject": "", "sentiment": False}


def test_a_monitors_records_are_paged_by_offset_and_its_events_and_one_record_read():
    arc = scripted({"data": [{"key": "a"}, {"key": "b"}], "total": 3}, {"data": [{"key": "c"}], "total": 3},
                   {"data": [{"kind": "added"}]}, {"key": "a", "events": []}, {"data": [], "counts": {}})
    assert [r["key"] for r in arc.monitors.rows("m1", active=False, limit=2)] == ["a", "b", "c"]
    assert dict(arc.calls[0].url.params) == {"active": "false", "offset": "0", "limit": "2"}
    assert arc.monitors.row_events("m1", run_id="mr1") == [{"kind": "added"}]
    assert dict(arc.calls[2].url.params) == {"limit": "200", "run_id": "mr1"}
    assert arc.monitors.row("m1", "a")["key"] == "a"
    assert arc.monitors.overview(kind="agent") == {"data": [], "counts": {}}
    assert arc.calls[4].url.path == "/api/v1/monitors/overview"


# -- triggers, proposals, estimate ----------------------------------------------------------------

def test_a_trigger_is_made_listed_changed_and_its_fires_followed():
    arc = scripted({"id": "t1"}, {"data": [{"id": "t1"}]}, {"id": "t1", "status": "paused"},
                   {"data": [{"id": "f1"}], "next": "/api/v1/triggers/t1/fires?limit=1&cursor=1"},
                   {"data": [{"id": "f2"}], "next": None}, {"matched": 1}, {"id": "t1", "deleted": True})
    arc.triggers.create({"kind": "project", "id": "p1"}, {"type": "recrawl"}, limits={"maxPerDay": 2})
    assert body(arc.calls[0]) == {"source": {"kind": "project", "id": "p1"}, "action": {"type": "recrawl"},
                                  "limits": {"maxPerDay": 2}}
    assert arc.triggers.list(source_id="p1") == [{"id": "t1"}]
    assert dict(arc.calls[1].url.params) == {"source_id": "p1"}
    arc.triggers.update("t1", status="paused")
    assert (arc.calls[2].method, body(arc.calls[2])) == ("PATCH", {"status": "paused"})
    assert [f["id"] for f in arc.triggers.fires("t1", limit=1)] == ["f1", "f2"]
    assert arc.triggers.preview("t1") == {"matched": 1}
    assert arc.triggers.delete("t1")["deleted"] is True


def test_proposals_are_listed_waiting_by_default_and_approved_or_dismissed():
    arc = scripted({"data": [{"id": "pr1"}]}, {"data": []}, {"made": {"kind": "monitor", "id": "m1"}},
                   (409, {"error": "already decided", "code": "conflict"}))
    assert arc.proposals.list() == [{"id": "pr1"}]
    assert dict(arc.calls[0].url.params) == {"status": "proposed"}
    arc.proposals.list(status="", run_id="r1")
    assert dict(arc.calls[1].url.params) == {"status": "", "run_id": "r1"}
    assert arc.proposals.approve("pr1")["made"]["id"] == "m1"
    with pytest.raises(MeshArcError) as exc:
        arc.proposals.dismiss("pr1")
    assert exc.value.code == "conflict"


def test_the_estimate_sends_the_apis_names_and_only_what_was_given():
    arc = scripted({"perMonth": 120, "upTo": True})
    assert arc.estimate("agent", "weekly", max_credits=300, month_cap=0)["upTo"] is True
    assert dict(arc.calls[0].url.params) == {"kind": "agent", "schedule": "weekly", "maxCredits": "300",
                                             "monthCap": "0"}


# -- sub-accounts -----------------------------------------------------------------------------------

def test_sub_accounts_are_made_capped_suspended_and_given_keys():
    arc = scripted({"id": "s1"}, {"id": "s1", "suspended": True}, {"id": "k1", "key": "mesharc_x"}, (204, None))
    arc.sub_accounts.create("Acme", monthly_cap=5000, external_id="cust-1", idempotency_key="s-1")
    assert body(arc.calls[0]) == {"name": "Acme", "monthlyCap": 5000, "externalId": "cust-1"}
    assert arc.calls[0].headers["Idempotency-Key"] == "s-1"
    arc.sub_accounts.update("s1", suspended=True, reason="unpaid")
    assert body(arc.calls[1]) == {"suspended": True, "reason": "unpaid"}
    assert arc.sub_accounts.create_key("s1", scopes=["read", "write"])["key"] == "mesharc_x"
    assert body(arc.calls[2]) == {"name": "default", "scopes": ["read", "write"]}
    assert arc.sub_accounts.revoke_key("s1", "k1") is None
    assert (arc.calls[3].method, arc.calls[3].url.path) == ("DELETE", "/api/v1/orgs/children/s1/keys/k1")


def test_a_client_for_a_sub_account_sends_the_tenant_header_on_every_request_and_the_parent_does_not():
    arc = MeshArc("mesharc_parent", base_url="https://api.test")
    child = arc.as_sub_account("s1")
    try:
        assert (child.tenant, child._h._c.headers.get(TENANT_HEADER), arc._h._c.headers.get(TENANT_HEADER)) == \
            ("s1", "s1", None)
        assert child._h._c.headers["Authorization"] == "Bearer mesharc_parent"
        assert str(child._h._c.base_url) == "https://api.test/api/v1/"
        assert MeshArc("k", tenant="s2")._h._c.headers[TENANT_HEADER] == "s2"
    finally:
        child.close()
        arc.close()


def test_a_tenant_the_key_may_not_act_on_is_a_404():
    arc = scripted((404, {"error": "sub-account not found", "code": "not_found"}))
    with pytest.raises(MeshArcError) as exc:
        arc.me()
    assert exc.value.status == 404


# -- connections and logins ----------------------------------------------------------------------------

def test_a_data_provider_is_connected_with_its_key_and_tested():
    arc = scripted({"id": "c1", "kind": "data provider"}, {"status": "failing", "error": "refused the key (401)"},
                   {"connections": [{"id": "c1"}], "secretsEncrypted": True}, (204, None))
    arc.connections.create("pdl", name="Company data", secrets={"api_key": "k"})
    assert body(arc.calls[0]) == {"provider": "pdl", "name": "Company data", "secrets": {"api_key": "k"}}
    assert arc.connections.test("c1")["status"] == "failing"
    assert arc.connections.list() == [{"id": "c1"}]
    assert arc.connections.delete("c1") is None


def test_saved_logins_are_listed_renamed_expired_tested_and_deleted():
    arc = scripted({"data": [], "enabled": False, "why": "logins are off"}, {"id": "l1", "status": "expired"},
                   {"status": 200, "loginPage": False}, (204, None))
    assert arc.logins.list()["enabled"] is False
    arc.logins.update("l1", expired=True)
    assert body(arc.calls[1]) == {"expired": True}
    assert arc.logins.test("l1")["loginPage"] is False
    assert arc.logins.delete("l1") is None


def test_the_user_agent_still_names_the_package():
    arc = scripted({"id": "ws"})
    arc.me()
    assert arc.calls[0].headers["User-Agent"] == f"mesharc-python/{__version__}"
