"""Tests run without any API key: a scripted chat model stands in for the LLM,
so the real graph, SQL search, lead scoring, booking and broker approval all execute."""
from datetime import timedelta

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from src.graph import create_graph
from src.graph_nodes.search_criteria_agent import Extraction
from src.runner import resume, run_turn
from src.util import db, notify
from src.util.india import format_inr, now_ist, parse_inr
from src.util.lead_scoring import score_lead


class ScriptedModel(BaseChatModel):
    """Returns queued AIMessages for agent calls and queued Extractions for structured output."""

    replies: list = []
    extractions: list = []
    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        if not self.replies:
            raise AssertionError("Scripted model ran out of replies")
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return RunnableLambda(lambda _: self.extractions.pop(0))


def call(name, args, i=1):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}_{i}"}])


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("ROUTINES_ENABLED", "0")
    monkeypatch.delenv("BROKER_WHATSAPP", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    notify.sent_log.clear()
    db.load_listings()


def next_saturday_iso(hour=11):
    d = now_ist() + timedelta(days=1)
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d"), d.strftime(f"%Y-%m-%dT{hour:02d}:00")


# ---------- helpers ----------
@pytest.mark.parametrize("text,expected", [
    ("80 L", 8_000_000), ("1.2 Cr", 12_000_000), ("₹95,00,000", 9_500_000), ("50 lakh", 5_000_000),
    ("1.5", 15_000_000), ("80", 8_000_000), ("2 crores", 20_000_000), (None, None), ("abc", None),
])
def test_parse_inr(text, expected):
    assert parse_inr(text) == expected


def test_format_inr():
    assert format_inr(16_200_000) == "₹1.62 Cr"
    assert format_inr(9_500_000) == "₹95 L"
    assert format_inr(20_000_000) == "₹2 Cr"
    assert format_inr(24_000) == "₹24,000"


def test_search_widens_and_respects_budget():
    rows, note = db.search_listings({"city": "Bengaluru", "locality": "Whitefield", "bhk": 3,
                                     "min_price": 15_000_000, "max_price": 18_000_000})
    assert rows and all("Whitefield" in r["locality"] and r["price_inr"] <= 18_900_000 for r in rows)
    rows, note = db.search_listings({"city": "Noida", "bhk": 2, "max_price": 4_000_000})
    assert rows and "above" in note


def test_scoring_bands():
    hot = score_lead({"timeline_months": 2, "financing": "ready", "phone": "+91", "name": "A", "wants_visit": True},
                     {"city": "Pune", "locality": "Baner", "bhk": 3, "max_price": 1}, [{}], 4)
    cold = score_lead({"timeline_months": 12}, {"city": "Noida", "bhk": 2, "max_price": 1}, [{}], 3)
    assert hot["band"] == "Hot" and cold["band"] == "Cold"
    assert hot["total"] <= 100


# ---------- full conversation ----------
def test_whitefield_buyer_end_to_end():
    date, slot = next_saturday_iso(11)
    model = ScriptedModel(
        replies=[
            # turn 1: main agent delegates to search, then answers with matches
            call("ToSearchAgent", {"request": "3BHK Whitefield 1.5-1.8 Cr, 2 months, loan sanctioned"}),
            AIMessage(content="Namaste Rohan! Here are 3 ready options in Whitefield. Would you like to visit this Saturday?"),
            # turn 2: hand over to appointment agent, check availability, book
            call("ToAppointmentAgent", {"request": "Saturday 11 AM"}, 2),
            call("check_availability", {"date": date}, 3),
            call("book_site_visit", {"listing_id": "BLR-101", "start_time_ist": slot, "customer_name": "Rohan Mehta"}, 4),
            # after broker approval
            AIMessage(content="Confirmed for Saturday 11 AM at Aurum Greens, Whitefield. Reminder the evening before."),
        ],
        extractions=[Extraction(city="Bangalore", locality="Whitefield", bhk=3, min_budget="1.5 Cr", max_budget="1.8 Cr",
                                timeline="within 2 months", timeline_months=2, financing="ready", ready_to_move=True,
                                language="en")],
    )
    graph = create_graph(llm=model)
    lead = {"name": "Rohan Mehta", "phone": "+919800004521", "source": "Portal enquiry"}

    r1 = run_turn(graph, "t1", "Looking for a 3BHK in Whitefield, 1.5 to 1.8 Cr, shifting in 2 months, loan sanctioned", lead)
    state = graph.get_state({"configurable": {"thread_id": "t1"}}).values
    assert state["search_criteria"]["city"] == "Bengaluru"
    assert state["search_criteria"]["max_price"] == 18_000_000
    assert [m["listing_id"] for m in state["matches"]][0] == "BLR-101"  # ready-to-move filter + closest to budget
    assert r1.lead_score["band"] == "Hot"
    assert "Here are 3" in r1.reply
    # the tool result the main agent saw includes rupee formatting and RERA
    tool_msg = state["messages"][2].content
    assert "₹1.62 Cr" in tool_msg and "RERA" in tool_msg
    # broker was alerted once about the hot lead, and the lead is in the CRM
    assert any(to == "broker" and "Hot lead" in text for to, text in notify.sent_log)
    assert db.list_leads()[0]["band"] == "Hot"

    r2 = run_turn(graph, "t1", "Saturday 11 AM works for us.", lead)
    assert r2.pending and r2.pending[0]["name"] == "book_site_visit"
    assert db.appointments_for(thread_id="t1") == []  # nothing booked before approval

    r3 = resume(graph, "t1", approve=True)
    appts = db.appointments_for(thread_id="t1")
    assert len(appts) == 1 and appts[0]["listing_id"] == "BLR-101" and appts[0]["customer_phone"] == "+919800004521"
    assert "Confirmed" in r3.reply and not r3.pending


def test_broker_declines_booking():
    date, slot = next_saturday_iso(16)
    model = ScriptedModel(
        replies=[
            call("ToAppointmentAgent", {"request": "visit Saturday 4pm"}),
            call("book_site_visit", {"listing_id": "PUN-201", "start_time_ist": slot, "customer_name": "Priya", "mode": "video"}, 2),
            AIMessage(content="Sorry, our advisor isn't free then. Would Sunday 11 AM or 6 PM work?"),
        ],
        extractions=[],
    )
    graph = create_graph(llm=model)
    r = run_turn(graph, "t2", "Can we do a video call Saturday 4pm?", {"phone": "+971500000000"})
    assert r.pending
    r2 = resume(graph, "t2", approve=False, reason="advisor on leave")
    assert db.appointments_for(thread_id="t2") == []
    assert "Sunday" in r2.reply
    # the appointment agent was told why
    last_prompt = model.seen[-1]
    assert any("advisor on leave" in str(m.content) for m in last_prompt)


def test_missing_budget_asks_before_searching():
    model = ScriptedModel(
        replies=[call("ToSearchAgent", {"request": "2bhk Noida"}), AIMessage(content="Budget kitna rakha hai?")],
        extractions=[Extraction(city="Noida", bhk=2, language="hinglish")],
    )
    graph = create_graph(llm=model)
    r = run_turn(graph, "t3", "bhai Noida mein 2bhk chahiye", {"source": "Website chat"})
    state = graph.get_state({"configurable": {"thread_id": "t3"}}).values
    assert state["matches"] == []
    assert "ask the customer for: budget" in state["messages"][2].content
    assert r.reply == "Budget kitna rakha hai?"
    assert state["lead"]["language"] == "hinglish"
