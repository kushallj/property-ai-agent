"""Turns the conversation into structured search criteria and a lead profile."""
import json
import logging
from typing import Literal, Optional

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from src.util import memory
from src.util.create_node import message_text
from src.util.india import parse_inr
from src.util.prompts import search_extraction_prompt
from src.util.state import State

logger = logging.getLogger(__name__)

CITY_ALIASES = {
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru", "blr": "Bengaluru",
    "gurgaon": "Gurugram", "gurugram": "Gurugram", "bombay": "Mumbai", "navi mumbai": "Mumbai", "thane": "Mumbai",
    "greater noida": "Noida", "madras": "Chennai", "new delhi": "Delhi",
}


class Extraction(BaseModel):
    city: Optional[str] = None
    locality: Optional[str] = None
    bhk: Optional[int] = None
    min_budget: Optional[str] = Field(None, description='Lower budget, e.g. "80 L" or "1.2 Cr"')
    max_budget: Optional[str] = Field(None, description='Upper budget, e.g. "1 Cr"')
    ready_to_move: Optional[bool] = None
    name: Optional[str] = None
    timeline: Optional[str] = Field(None, description="Timeline in the customer's words")
    timeline_months: Optional[int] = None
    financing: Optional[Literal["ready", "needs_loan"]] = None
    purpose: Optional[Literal["self_use", "investment"]] = None
    nri: Optional[bool] = None
    language: Optional[Literal["en", "hinglish"]] = None
    wants_visit: Optional[bool] = None
    visit_mode: Optional[Literal["site", "video"]] = None
    remember: list[str] = Field(default_factory=list, description=(
        "New lasting personal details worth remembering, as short phrases: family, workplace, "
        "facing, floor, parking, pets, schools, likes and dislikes, best time to call. "
        "Not city, BHK, budget or timeline."))


def normalise(ex: Extraction) -> tuple[dict, dict]:
    criteria, lead = {}, {}
    if ex.city:
        criteria["city"] = CITY_ALIASES.get(ex.city.strip().lower(), ex.city.strip().title())
    if ex.locality:
        criteria["locality"] = ex.locality.strip()
    if ex.bhk:
        criteria["bhk"] = int(ex.bhk)
    lo, hi = parse_inr(ex.min_budget), parse_inr(ex.max_budget)
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    if hi:
        criteria["max_price"] = hi
        criteria["min_price"] = lo or int(hi * 0.7)
    elif lo:
        criteria["min_price"], criteria["max_price"] = lo, int(lo * 1.2)
    if ex.ready_to_move is not None:
        criteria["ready_to_move"] = ex.ready_to_move
    for key in ["name", "timeline", "timeline_months", "financing", "purpose", "nri", "language", "wants_visit", "visit_mode"]:
        value = getattr(ex, key)
        if value is not None:
            lead[key] = value
    return criteria, lead


def build_search_criteria_agent(llm):
    extractor = llm.with_structured_output(Extraction)

    def search_criteria_agent(state: State, config: RunnableConfig) -> dict:
        request = state["messages"][-1].tool_calls[0]["args"].get("request", "")
        customer_lines = [message_text(m) for m in state["messages"] if isinstance(m, HumanMessage)][-6:]
        current = {"criteria": state.get("search_criteria") or {}, "lead": state.get("lead") or {}}
        messages = [
            ("system", search_extraction_prompt.format(current=json.dumps(current, ensure_ascii=False))),
            ("user", "Customer messages so far:\n" + "\n".join(f"- {line}" for line in customer_lines)
             + f"\n\nAssistant's summary of the latest request: {request}"),
        ]
        try:
            extraction = extractor.invoke(messages)
            criteria, lead = normalise(extraction)
        except Exception as exc:  # keep the conversation going if extraction fails
            logger.warning("Search criteria extraction failed: %s", exc)
            return {}
        if extraction.remember:
            memory.add_notes(config.get("configurable", {}).get("thread_id", "unknown"), extraction.remember, by="agent")
        return {"search_criteria": criteria, "lead": lead}

    return search_criteria_agent
