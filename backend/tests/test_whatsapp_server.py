import re

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app_whatsapp import server
from src.graph import create_graph
from src.util import db, notify
from tests.test_agent import ScriptedModel, call, next_saturday_iso, temp_db  # noqa: F401  (fixture)


def wa_payload(sender: str, text: str, name: str = "Rohan") -> dict:
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": sender, "profile": {"name": name}}],
        "messages": [{"from": sender, "type": "text", "text": {"body": text}}],
    }}]}]}


def test_webhook_booking_with_broker_whatsapp_approval(monkeypatch):
    monkeypatch.setenv("BROKER_WHATSAPP", "+919811100000")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "secret-token")
    date, slot = next_saturday_iso(12)
    model = ScriptedModel(replies=[
        call("ToAppointmentAgent", {"request": "Saturday noon"}),
        call("book_site_visit", {"listing_id": "BLR-103", "start_time_ist": slot, "customer_name": "Rohan"}, 2),
        AIMessage(content="Confirmed: Saturday 12 PM at Palm Court."),
    ], extractions=[])
    monkeypatch.setattr(server, "_graph", create_graph(llm=model))
    client = TestClient(server.app)

    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "secret-token",
                                          "hub.challenge": "42"}).text == "42"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong"}).status_code == 403

    # customer asks for a visit (background task runs before TestClient returns)
    assert client.post("/webhook", json=wa_payload("919800004521", "Saturday 12 baje visit kar sakte hain?")).status_code == 200
    broker_msg = next(text for to, text in notify.sent_log if to == "+919811100000")
    assert "Approval needed" in broker_msg and "Palm Court" in broker_msg
    assert any(to == "919800004521" and "confirm this slot" in text for to, text in notify.sent_log)
    code = re.search(r"YES ([0-9A-F]{4})", broker_msg).group(1)

    # broker approves from WhatsApp
    client.post("/webhook", json=wa_payload("919811100000", f"yes {code}", name="Broker"))
    appts = client.get("/appointments").json()
    assert len(appts) == 1 and appts[0]["listing_id"] == "BLR-103" and appts[0]["customer_phone"] == "+919800004521"
    assert any(to == "919800004521" and "Confirmed" in text for to, text in notify.sent_log)
    assert db.get_approval(code)["status"] == "approved"
