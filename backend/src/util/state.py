from typing import Annotated, Optional

from typing_extensions import TypedDict
from langgraph.graph.message import AnyMessage, add_messages


class SearchCriteria(TypedDict, total=False):
    city: str
    locality: str
    bhk: int
    min_price: int  # rupees
    max_price: int  # rupees
    ready_to_move: bool


class LeadProfile(TypedDict, total=False):
    name: str
    phone: str
    source: str  # WhatsApp, Portal enquiry, Meta lead form, Website chat, Missed call...
    timeline: str  # free text, e.g. "within 2 months"
    timeline_months: int
    financing: str  # "ready" (loan sanctioned / own funds) or "needs_loan"
    purpose: str  # "self_use" or "investment"
    nri: bool
    language: str  # "en", "hinglish", "hi"
    wants_visit: bool
    visit_mode: str  # "site" or "video"


def merge_criteria(current: Optional[SearchCriteria], new: Optional[SearchCriteria]) -> SearchCriteria:
    """Merge new search criteria into the old ones.

    A new city without a locality clears the old locality, so "Whitefield" doesn't
    stick when the buyer switches to Pune.
    """
    result = dict(current or {})
    new = {k: v for k, v in (new or {}).items() if v is not None}
    if "city" in new and new["city"] != result.get("city") and "locality" not in new:
        result.pop("locality", None)
    result.update(new)
    return result  # type: ignore[return-value]


def merge_lead(current: Optional[LeadProfile], new: Optional[LeadProfile]) -> LeadProfile:
    result = dict(current or {})
    result.update({k: v for k, v in (new or {}).items() if v is not None})
    return result  # type: ignore[return-value]


def replace(_current, new):
    return new


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    search_criteria: Annotated[SearchCriteria, merge_criteria]
    lead: Annotated[LeadProfile, merge_lead]
    matches: Annotated[list[dict], replace]
    match_note: Annotated[str, replace]
    lead_score: Annotated[dict, replace]
