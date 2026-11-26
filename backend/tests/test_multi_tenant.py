"""Many brokers on one server: each broker's key sees only their own data."""
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app_whatsapp import server
from src import campaigns, tenancy
from src.graph import create_graph
from src.graph_nodes.search_criteria_agent import Extraction
from src.routines import run_due
from src.util import db, notify
from src.util.india import IST
from tests.test_agent import ScriptedModel, call, temp_db  # noqa: F401  (fixture)

PLATFORM = {"X-API-Key": "platform-secret"}
INVENTORY_A = """listing_id,project,builder,locality,city,state,bhk,carpet_sqft,price_inr,ready_to_move,possession,rera_id,amenities,highlight
A-1,Sharma Heights,Sharma Builders,Baner,Pune,Maharashtra,2,800,7500000,1,Ready to move,MH/RERA/A1,Gym,Near Balewadi
A-2,Sharma Grande,Sharma Builders,Baner,Pune,Maharashtra,3,1200,12500000,1,Ready to move,MH/RERA/A2,Pool,Hill view
"""
CSV = "name,phone\nRohan,9800004521\nPriya,9822233344\n"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "platform-secret")
    return TestClient(server.app)


def add_broker(client, name, **extra):
    r = client.post("/platform/tenants", headers=PLATFORM, json={"name": name, **extra})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], {"X-API-Key": body["api_key"]}


def wa_message(phone_number_id, sender, text, mid):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "contacts": [{"wa_id": sender, "profile": {"name": "Rohan"}}],
        "messages": [{"id": mid, "from": sender, "type": "text", "text": {"body": text}}]}}]}]}


def test_platform_endpoints_need_the_platform_key(client):
    a_id, a_key = add_broker(client, "Sharma Properties")
    assert client.get("/platform/tenants").status_code == 401
    assert client.get("/platform/tenants", headers=a_key).status_code == 401  # a broker can't manage brokers
    ids = [t["id"] for t in client.get("/platform/tenants", headers=PLATFORM).json()]
    assert ids == ["default", "sharma-properties"]
    assert "api_key" not in client.get(f"/platform/tenants/{a_id}", headers=PLATFORM).json()
    assert "whatsapp_token" not in client.get(f"/platform/tenants/{a_id}", headers=PLATFORM).json()


def test_each_broker_sees_only_their_own_data(client, monkeypatch):
    a_id, a_key = add_broker(client, "Sharma Properties")
    b_id, b_key = add_broker(client, "Gupta Realty", sample_listings=True)

    # inventory
    assert client.post("/listings", headers=a_key, json={"csv": INVENTORY_A}).json() == {"loaded": 2}
    assert {l["listing_id"] for l in client.get("/listings", headers=a_key).json()} == {"A-1", "A-2"}
    assert len(client.get("/listings", headers=b_key).json()) == 34  # sample set, none of A's
    assert all(l["tenant_id"] == b_id for l in client.get("/listings", headers=b_key).json())

    # a conversation with A uses A's inventory and A's name, and creates a lead only A can see
    model = ScriptedModel(
        replies=[call("ToSearchAgent", {"request": "2BHK Baner"}), AIMessage(content="Sharma Heights fits.")],
        extractions=[Extraction(city="Pune", locality="Baner", bhk=2, max_budget="80 L", timeline_months=2)])
    monkeypatch.setattr(server, "_graph", create_graph(llm=model))
    res = client.post("/chat", headers=a_key, json={"thread_id": "web-1", "message": "2BHK Baner under 80L", "name": "Rohan"}).json()
    assert [m["listing_id"] for m in res["matches"]] == ["A-1"]
    assert "Sharma Properties" in model.seen[0][0].content
    a_leads = client.get("/leads", headers=a_key).json()
    assert [l["thread_id"] for l in a_leads] == [f"{a_id}:web-1"]
    assert client.get("/leads", headers=b_key).json() == []
    assert client.get(f"/leads/{a_id}:web-1/notes", headers=b_key).status_code == 404

    # campaigns and opt-outs
    cid = client.post("/campaigns", headers=a_key, json={"name": "A launch", "template_name": "t", "csv": CSV}).json()["id"]
    assert client.get(f"/campaigns/{cid}", headers=b_key).status_code == 404
    assert client.post(f"/campaigns/{cid}/start", headers=b_key).status_code == 404
    assert client.get("/campaigns", headers=b_key).json()["campaigns"] == []
    assert client.get("/campaigns", headers=a_key).json()["firm"] == "Sharma Properties"
    client.post("/opt-outs", headers=a_key, json={"phone": "9800004521"})
    assert client.get("/opt-outs", headers=b_key).json() == []
    assert campaigns.create_campaign("B", "t", "en", [], CSV, tenant_id=b_id)["counts"]["opted_out"] == 0

    # the platform key acts as the default broker unless it names one
    assert client.get("/leads", headers=PLATFORM).json() == []
    assert len(client.get("/leads", headers={**PLATFORM, "X-Tenant": a_id}).json()) == 1


def test_whatsapp_messages_route_by_business_number(client, monkeypatch):
    a_id, _ = add_broker(client, "Sharma Properties", whatsapp_phone_number_id="111", broker_whatsapp="+919811100001")
    b_id, b_key = add_broker(client, "Gupta Realty", whatsapp_phone_number_id="222", broker_whatsapp="+919811100002")
    model = ScriptedModel(replies=[AIMessage(content="Namaste from Gupta Realty!")], extractions=[])
    monkeypatch.setattr(server, "_graph", create_graph(llm=model))

    client.post("/webhook", json=wa_message("222", "919800004521", "Hi", "m1"))
    assert "Gupta Realty" in model.seen[0][0].content
    state = server._graph.get_state({"configurable": {"thread_id": f"{b_id}:wa-919800004521"}}).values
    assert state["lead"]["phone"] == "+919800004521"
    assert db.get_usage(b_id)["ai_turns"] == 1 and db.get_usage(a_id)["ai_turns"] == 0

    # STOP to A's number opts out from A only
    client.post("/webhook", json=wa_message("111", "919800004521", "STOP", "m2"))
    assert campaigns.is_opted_out("919800004521", a_id) and not campaigns.is_opted_out("919800004521", b_id)
    assert "Sharma Properties" in notify.sent_log[-1][1]

    # a number no broker owns is ignored
    client.post("/webhook", json=wa_message("999", "919800004521", "Hi", "m3"))
    assert len(model.seen) == 1


def test_ai_monthly_limit_protects_margin(client, monkeypatch):
    a_id, a_key = add_broker(client, "Sharma Properties", ai_monthly_limit=1, broker_whatsapp="+919811100001")
    model = ScriptedModel(replies=[AIMessage(content="First reply")], extractions=[])
    monkeypatch.setattr(server, "_graph", create_graph(llm=model))
    assert client.post("/chat", headers=a_key, json={"thread_id": "w1", "message": "hi"}).json()["reply"] == "First reply"
    second = client.post("/chat", headers=a_key, json={"thread_id": "w2", "message": "hello"}).json()
    assert second["limit_reached"] and second["reply"] == server.LIMIT_REPLY
    assert len(model.seen) == 1  # no AI call over the limit
    assert any(to == "+919811100001" and "limit" in text for to, text in notify.sent_log)
    me = client.get("/me", headers=a_key).json()
    assert me["usage_this_month"]["ai_turns"] == 1 and me["ai_limit_reached"]


def test_pause_and_rotate_key(client, monkeypatch):
    a_id, a_key = add_broker(client, "Sharma Properties", whatsapp_phone_number_id="111")
    assert client.post(f"/platform/tenants/{a_id}/pause", headers=PLATFORM).json()["status"] == "paused"
    assert client.get("/leads", headers=a_key).status_code == 403
    model = ScriptedModel(replies=[], extractions=[])
    monkeypatch.setattr(server, "_graph", create_graph(llm=model))
    client.post("/webhook", json=wa_message("111", "919800004521", "Hi", "m1"))
    assert model.seen == []  # paused broker's number gets no AI replies
    client.post(f"/platform/tenants/{a_id}/activate", headers=PLATFORM)
    new_key = client.post(f"/platform/tenants/{a_id}/rotate-key", headers=PLATFORM).json()["api_key"]
    assert client.get("/leads", headers=a_key).status_code == 401
    assert client.get("/leads", headers={"X-API-Key": new_key}).status_code == 200
    assert client.post("/platform/tenants/default/pause", headers=PLATFORM).status_code == 400


def test_whatsapp_number_cannot_belong_to_two_brokers(client):
    add_broker(client, "Sharma Properties", whatsapp_phone_number_id="111")
    r = client.post("/platform/tenants", headers=PLATFORM, json={"name": "Copycat", "whatsapp_phone_number_id": "111"})
    assert r.status_code == 400 and "already connected" in r.json()["detail"]


def test_routines_run_per_broker_with_their_own_name(client):
    a_id, _ = add_broker(client, "Sharma Properties", broker_whatsapp="+919811100001")
    db.upsert_lead(f"{a_id}:wa-1", {"name": "Rohan", "phone": "+919800004521"},
                   {"city": "Pune", "bhk": 2, "max_price": 8_000_000}, {"total": 90, "band": "Hot", "factors": []}, tenant_id=a_id)
    res = run_due(datetime(2026, 12, 8, 8, 40, tzinfo=IST))
    by = {(r["tenant"], r["routine"]): r for r in res}
    assert by[(a_id, "morning_briefing")]["items"] == 1 and by[("default", "morning_briefing")]["items"] == 0
    briefing = next(text for to, text in notify.sent_log if to == "+919811100001")
    assert "Sharma Properties" in briefing and "Rohan" in briefing
    assert db.routine_ran("morning_briefing", f"{a_id}|2026-12-08")


def test_campaign_daily_limit_is_per_broker(client):
    a_id, _ = add_broker(client, "Sharma Properties", daily_limit=1)
    b_id, _ = add_broker(client, "Gupta Realty", daily_limit=5)
    for t in (a_id, b_id):
        cid = campaigns.create_campaign("c", "t", "en", [], CSV, tenant_id=t)["id"]
        campaigns.set_status(cid, "running", tenant_id=t)
    sent = campaigns.tick(datetime(2026, 12, 8, 12, 0, tzinfo=IST), lambda *a: (True, "wamid.x", None, ""), lambda _: None)
    assert sent == 3  # A stops at its limit of 1, B sends both
    assert db.get_usage(a_id)["template_messages"] == 1 and db.get_usage(b_id)["template_messages"] == 2


def test_old_single_broker_database_is_upgraded(tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE opt_outs (phone TEXT PRIMARY KEY, reason TEXT, created_at TEXT)")
        conn.execute("INSERT INTO opt_outs VALUES ('919800004521', 'replied STOP', '2026-11-01')")
        conn.execute(f"CREATE TABLE listings ({', '.join(db.LISTING_COLUMNS)})")
        conn.execute(f"INSERT INTO listings VALUES ({', '.join('?' * len(db.LISTING_COLUMNS))})",
                     ("OLD-1", "Old Tower", "B", "Wakad", "Pune", "MH", 2, 700, 7000000, 1, "Ready", "R", "", ""))
        conn.execute("CREATE TABLE leads (thread_id TEXT PRIMARY KEY, name TEXT)")
    monkeypatch.setenv("DB_PATH", str(old))
    assert campaigns.is_opted_out("919800004521", "default")
    assert db.get_listing("OLD-1")["project"] == "Old Tower"
    assert tenancy.get("default").name
