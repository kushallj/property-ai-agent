from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app_whatsapp import server
from src import campaigns
from src.graph import create_graph
from src.util import db, memory, notify
from src.util.india import IST
from tests.test_agent import ScriptedModel, temp_db  # noqa: F401  (fixture)

NOON = datetime(2026, 12, 8, 12, 0, tzinfo=IST)
CSV = """Name,Mobile Number,Locality,Budget
Rohan Mehta,98000 04521,Whitefield,1.6 Cr
Priya Nair,+971 50 123 4567,Baner,80 L
Rohan M,+91-9800004521,Whitefield,1.6 Cr
Bad Number,12345,Thane,
Amit Shah,09811122233,,90 L
Sneha Patil,9822233344,Kharadi,1 Cr
"""


def make(params=("{first_name}", "{locality|your area}"), csv=CSV):
    return campaigns.create_campaign("Dec launch", "new_project_launch", "en", list(params), csv)


class FakeSender:
    def __init__(self, results=None):
        self.calls, self.results = [], list(results or [])

    def __call__(self, to, name, lang, params, image):
        self.calls.append((to, params))
        return self.results.pop(0) if self.results else (True, f"wamid.{len(self.calls)}", None, "")


def no_sleep(_):
    pass


# ---------- import ----------
@pytest.mark.parametrize("raw,expected", [
    ("98000 04521", "919800004521"), ("+91-9800004521", "919800004521"), ("09800004521", "919800004521"),
    ("919800004521", "919800004521"), ("+971 50 123 4567", "971501234567"), ("0044 7700 900123", "447700900123"),
    ("12345", None), ("", None), ("5800004521", None),
])
def test_normalize_phone(raw, expected):
    assert campaigns.normalize_phone(raw) == expected


def test_import_cleans_list_and_fills_variables():
    campaigns.opt_out("919822233344")
    res = make()
    c = res["counts"]
    assert c == {"rows": 6, "queued": 3, "invalid_phone": 1, "duplicates": 1, "opted_out": 1, "missing_values": 0}
    assert res["columns"]["phone_column"] == "Mobile Number"
    assert res["samples"][0] == {"phone": "+919800004521", "name": "Rohan Mehta", "variables": ["Rohan", "Whitefield"]}
    amit = [r for r in campaigns.recipients(res["id"]) if r["name"] == "Amit Shah"][0]
    assert amit["params_json"] == '["Amit", "your area"]'  # fallback used for the empty locality
    assert campaigns.get_campaign(res["id"])["status"] == "draft"


def test_missing_variable_without_fallback_is_skipped():
    res = make(params=("{first_name}", "{locality}"))
    assert res["counts"]["missing_values"] == 1 and any("Amit" in p for p in res["problems"])


def test_csv_without_phone_column_is_rejected():
    with pytest.raises(ValueError, match="No phone column"):
        make(csv="Name,City\nA,Pune\n")


# ---------- sending ----------
def test_draft_is_not_sent_until_started():
    cid = make()["id"]
    sender = FakeSender()
    assert campaigns.tick(NOON, sender, no_sleep) == 0
    campaigns.set_status(cid, "running")
    assert campaigns.tick(NOON, sender, no_sleep) == 4
    assert {to for to, _ in sender.calls} == {"919800004521", "971501234567", "919811122233", "919822233344"}
    assert campaigns.tick(NOON, sender, no_sleep) == 0  # nothing left: campaign completes
    assert campaigns.get_campaign(cid)["status"] == "completed"
    assert campaigns.stats(cid)["sent"] == 4


def test_respects_sending_hours_and_daily_limit(monkeypatch):
    cid = make()["id"]
    campaigns.set_status(cid, "running")
    sender = FakeSender()
    assert campaigns.tick(datetime(2026, 12, 8, 22, 0, tzinfo=IST), sender, no_sleep) == 0
    assert "sending hours" in campaigns.get_campaign(cid)["note"]
    monkeypatch.setenv("WHATSAPP_DAILY_LIMIT", "2")
    assert campaigns.tick(NOON, sender, no_sleep) == 2
    assert campaigns.tick(NOON, sender, no_sleep) == 0
    assert "Daily limit of 2 reached" in campaigns.get_campaign(cid)["note"]
    assert campaigns.tick(datetime(2026, 12, 9, 12, 0, tzinfo=IST), sender, no_sleep) == 2  # next day continues


def test_template_error_pauses_campaign():
    cid = make()["id"]
    campaigns.set_status(cid, "running")
    sender = FakeSender([(False, None, 132001, "Template name does not exist in the translation")])
    assert campaigns.tick(NOON, sender, no_sleep) == 0
    c = campaigns.get_campaign(cid)
    assert c["status"] == "paused" and "132001" in c["note"]
    assert campaigns.stats(cid)["queued"] == 4  # nobody lost; resume after fixing the template


def test_rate_limit_is_retried_later_and_bad_number_fails():
    cid = make()["id"]
    campaigns.set_status(cid, "running")
    sender = FakeSender([(False, None, 131026, "Message undeliverable"), (False, None, 130429, "Rate limit hit")])
    campaigns.tick(NOON, sender, no_sleep)
    rows = {r["phone"]: r for r in campaigns.recipients(cid)}
    assert rows["919800004521"]["status"] == "failed"
    assert rows["971501234567"]["status"] == "queued" and rows["971501234567"]["next_attempt_at"]  # backing off
    assert campaigns.tick(NOON, sender, no_sleep) == 2  # everyone else still goes out
    later = datetime(2026, 12, 8, 13, 0, tzinfo=IST)
    assert campaigns.tick(later, sender, no_sleep) == 1  # retry after backoff succeeds


# ---------- webhooks ----------
def test_status_webhooks_build_the_funnel():
    cid = make()["id"]
    campaigns.set_status(cid, "running")
    campaigns.tick(NOON, FakeSender(), no_sleep)
    client = TestClient(server.app)
    statuses = [{"id": "wamid.1", "status": "delivered"}, {"id": "wamid.1", "status": "read"},
                {"id": "wamid.2", "status": "delivered"}, {"id": "wamid.1", "status": "delivered"},  # late, out of order
                {"id": "wamid.3", "status": "failed", "errors": [{"code": 131049, "title": "Frequency cap"}]}]
    client.post("/webhook", json={"entry": [{"changes": [{"value": {"statuses": statuses}}]}]})
    s = campaigns.stats(cid)
    # 4 sent; wamid.3 failed after sending, wamid.4 has no receipt yet
    assert (s["sent"], s["delivered"], s["read"], s["failed"]) == (3, 2, 1, 1)


def wa_message(phone, text, mid):
    return {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": phone, "profile": {"name": "Rohan"}}],
                                              "messages": [{"id": mid, "from": phone, "type": "text", "text": {"body": text}}]}}]}]}


def test_stop_opts_out_and_blocks_future_campaigns():
    client = TestClient(server.app)
    client.post("/webhook", json=wa_message("919800004521", "STOP", "m1"))
    assert campaigns.is_opted_out("919800004521")
    assert "won't get offers" in notify.sent_log[-1][1]
    res = make()
    assert res["counts"]["opted_out"] == 1
    client.post("/webhook", json=wa_message("919800004521", "start", "m2"))
    assert not campaigns.is_opted_out("919800004521")


def test_reply_to_campaign_reaches_agent_with_campaign_as_source(monkeypatch):
    cid = make()["id"]
    campaigns.set_status(cid, "running")
    campaigns.tick(NOON, FakeSender(), no_sleep)
    model = ScriptedModel(replies=[AIMessage(content="Great to hear, Rohan! Shall I share 3 options in Whitefield?")], extractions=[])
    monkeypatch.setattr(server, "_graph", create_graph(llm=model))
    TestClient(server.app).post("/webhook", json=wa_message("919800004521", "Interested", "m3"))
    assert campaigns.stats(cid)["replied"] == 1
    assert ("919800004521", "Great to hear, Rohan! Shall I share 3 options in Whitefield?") in notify.sent_log
    state = server._graph.get_state({"configurable": {"thread_id": "wa-919800004521"}}).values
    assert state["lead"]["source"] == "Campaign: Dec launch"
    assert "Replied to the 'Dec launch' WhatsApp campaign" in [n["text"] for n in memory.read_notes("wa-919800004521")]


# ---------- REST API ----------
def test_campaign_api_end_to_end(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "k")
    client = TestClient(server.app)
    h = {"X-API-Key": "k"}
    assert client.post("/campaigns", json={"name": "x", "template_name": "t", "csv": CSV}).status_code == 401
    res = client.post("/campaigns", headers=h, json={"name": "Dec launch", "template_name": "new_project_launch",
                                                       "params": ["{first_name}"], "csv": CSV}).json()
    cid = res["id"]
    test = client.post(f"/campaigns/{cid}/test", headers=h, json={"phone": "98111 00000"}).json()
    assert test["ok"] and test["message_id"].startswith("sim-") and test["variables"] == ["Rohan"]
    assert client.post(f"/campaigns/{cid}/start", headers=h).json()["status"] == "running"
    assert client.post(f"/campaigns/{cid}/start", headers=h).status_code == 409
    assert client.post(f"/campaigns/{cid}/pause", headers=h).json()["status"] == "paused"
    listing = client.get("/campaigns", headers=h).json()
    assert listing["daily_limit"] == 250 and listing["campaigns"][0]["stats"]["queued"] == 4
    assert client.get(f"/campaigns/{cid}/export.csv", headers=h).text.startswith("phone,name,status")
    assert client.post("/opt-outs", headers=h, json={"phone": "9822233344"}).json() == {"opted_out": "+919822233344"}
    assert "WhatsApp Campaigns" in client.get("/admin").text
