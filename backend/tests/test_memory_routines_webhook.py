"""Tests for the three ideas borrowed from OpenMausBot: lead memory, routines, duplicate-safe webhook."""
from datetime import datetime

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app_whatsapp import server
from src.graph import create_graph
from src.graph_nodes.search_criteria_agent import Extraction
from src.routines import run_due
from src.runner import run_turn
from src.util import db, memory, notify
from src.util.india import IST
from tests.test_agent import ScriptedModel, call, temp_db  # noqa: F401  (fixture)

MONDAY_EVENING = datetime(2026, 12, 7, 18, 30, tzinfo=IST)


def sent_to(who):
    return [text for to, text in notify.sent_log if to == who]


# ---------- 1. per-lead memory ----------
def test_memory_file_dedupes_and_formats(tmp_path):
    assert memory.add_notes("wa-91980", ["Wife works in Hinjewadi", "Has a dog"]) == ["Wife works in Hinjewadi", "Has a dog"]
    assert memory.add_notes("wa-91980", ["wife works in hinjewadi!", "Call after 7 pm"], by="broker") == ["Call after 7 pm"]
    notes = memory.read_notes("wa-91980")
    assert [n["text"] for n in notes] == ["Wife works in Hinjewadi", "Has a dog", "Call after 7 pm"]
    assert notes[-1]["by"] == "broker"
    text = (tmp_path / "memory" / "wa-91980.md").read_text(encoding="utf-8")
    assert text.startswith("# Notes about this customer") and "· Has a dog (agent)" in text


def test_agent_saves_and_later_reads_memory():
    model = ScriptedModel(
        replies=[
            call("ToSearchAgent", {"request": "2BHK Hinjewadi under 80L, wife works there, wants east facing"}),
            AIMessage(content="Here are 2 options near Hinjewadi."),
            AIMessage(content="Welcome back! Still looking for east-facing near Hinjewadi?"),
        ],
        extractions=[Extraction(city="Pune", locality="Hinjewadi", bhk=2, max_budget="80 L",
                                remember=["Wife works in Hinjewadi", "Wants east-facing"])],
    )
    graph = create_graph(llm=model)
    run_turn(graph, "wa-919811111111", "2bhk hinjewadi under 80 lakh, my wife works there. east facing please",
             {"phone": "+919811111111"})
    assert [n["text"] for n in memory.read_notes("wa-919811111111")] == ["Wife works in Hinjewadi", "Wants east-facing"]

    run_turn(graph, "wa-919811111111", "Hi again")
    system_prompt = model.seen[-1][0].content  # the main agent's system message on the second visit
    assert "Remember about this customer" in system_prompt and "Wants east-facing" in system_prompt


def test_broker_can_add_notes_over_api():
    client = TestClient(server.app)
    assert client.post("/leads/default:wa-1/notes", json={"note": "Prefers calls after 7 pm"}).json() == {"added": ["Prefers calls after 7 pm"]}
    assert client.get("/leads/default:wa-1/notes").json()[0]["by"] == "broker"


# ---------- 2. routines ----------
def seed_leads():
    hot = {"total": 88, "band": "Hot", "factors": []}
    warm = {"total": 60, "band": "Warm", "factors": []}
    db.upsert_lead("wa-1", {"name": "Rohan Mehta", "phone": "+919800004521"},
                   {"city": "Bengaluru", "locality": "Whitefield", "bhk": 3, "max_price": 18_000_000}, hot)
    db.upsert_lead("wa-2", {"name": "Priya Nair", "phone": "+971500000000"},
                   {"city": "Pune", "bhk": 2, "min_price": 6_000_000, "max_price": 8_000_000}, warm)


def test_quiet_day_sends_nothing():
    results = run_due(datetime(2026, 12, 8, 8, 40, tzinfo=IST))
    assert results == [{"tenant": "default", "routine": "morning_briefing", "period": "2026-12-08", "items": 0, "summary": "nothing to do"}]
    assert notify.sent_log == []


def test_morning_briefing_runs_once_per_day():
    seed_leads()
    morning = datetime(2026, 12, 8, 8, 35, tzinfo=IST)
    first = run_due(morning)
    assert first[0]["routine"] == "morning_briefing" and "briefing sent" in first[0]["summary"]
    assert "Rohan Mehta" in sent_to("broker")[0]
    assert run_due(datetime(2026, 12, 8, 9, 0, tzinfo=IST)) == []  # ledger: already ran today
    assert len(sent_to("broker")) == 1


def test_visit_reminder_sent_once_evening_before():
    db.load_listings()
    appt = db.add_appointment(thread_id="wa-1", customer_name="Rohan Mehta", customer_phone="+919800004521",
                              listing_id="BLR-101", start_ist="2026-12-08T11:00:00+05:30", duration_min=45, mode="site")
    assert run_due(datetime(2026, 12, 7, 17, 0, tzinfo=IST)) == []  # before the 18:00 window
    res = run_due(MONDAY_EVENING)
    assert {"tenant": "default", "routine": "visit_reminders", "period": "2026-12-07 18", "items": 1, "summary": "1 reminder(s) sent"} in res
    msg = sent_to("+919800004521")[0]
    assert "Aurum Greens" in msg and "tomorrow" in msg and "Reply 1 to confirm" in msg
    assert db.get_appointment(appt)["reminded_at"]
    run_due(datetime(2026, 12, 7, 19, 30, tzinfo=IST))  # next hour: checks again, finds it already reminded
    assert len(sent_to("+919800004521")) == 1


def test_weekly_shortlist_only_sends_new_listings():
    seed_leads()
    monday = datetime(2026, 12, 7, 10, 30, tzinfo=IST)
    res = {r["routine"]: r for r in run_due(monday)}
    assert res["weekly_shortlist"]["summary"] == "1 shortlist(s) sent"  # Priya (Warm, quiet); Rohan is Hot
    first = sent_to("+971500000000")[0]
    assert "New this week" in first and "Sahyadri Skyline" in first
    # a week later both in-budget listings were already sent, so nothing new means no message at all
    res2 = {r["routine"]: r for r in run_due(datetime(2026, 12, 14, 10, 30, tzinfo=IST))}
    assert res2["weekly_shortlist"]["summary"] == "nothing to do"
    assert len(sent_to("+971500000000")) == 1
    assert set(db.list_leads()[1]["shortlist_sent"].split(",")) == {"PUN-201", "PUN-203"}


def test_old_database_gets_new_columns(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE leads (thread_id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE appointments (id INTEGER PRIMARY KEY, start_ist TEXT)")
    monkeypatch.setenv("DB_PATH", str(old))
    with db.connect() as conn:
        assert "shortlist_sent" in {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
        assert "reminded_at" in {r[1] for r in conn.execute("PRAGMA table_info(appointments)")}


def test_forced_run_and_history_api():
    seed_leads()
    client = TestClient(server.app)
    out = client.post("/routines/morning_briefing/run").json()
    assert out[0]["routine"] == "morning_briefing" and out[0]["items"] == 1
    assert client.get("/routines").json()[0]["routine"] == "morning_briefing"
    assert client.post("/routines/nope/run").status_code == 404


# ---------- 3. duplicate-safe webhook ----------
def test_repeat_delivery_is_answered_once(monkeypatch):
    model = ScriptedModel(replies=[AIMessage(content="Namaste! Which city are you looking in?")], extractions=[])
    monkeypatch.setattr(server, "_graph", create_graph(llm=model))
    client = TestClient(server.app)
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "919822222222", "profile": {"name": "Asha"}}],
        "messages": [{"id": "wamid.ABC123", "from": "919822222222", "type": "text", "text": {"body": "Hi"}}],
    }}]}]}
    client.post("/webhook", json=payload)
    client.post("/webhook", json=payload)  # Meta retry: same message id
    assert sent_to("919822222222") == ["Namaste! Which city are you looking in?"]
    assert db.claim_message("wamid.NEW") and not db.claim_message("wamid.NEW")
