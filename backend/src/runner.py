"""One conversation turn, and broker approve/decline, shared by the CLI and the WhatsApp server."""
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.util import db
from src.util.create_node import message_text
from src.util.india import fmt_ist, format_inr, parse_ist


@dataclass
class TurnResult:
    reply: str
    pending: list[dict] = field(default_factory=list)  # tool calls waiting for broker approval
    lead_score: dict = field(default_factory=dict)


def config_for(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _result(graph, cfg: dict, start: int) -> TurnResult:
    snap = graph.get_state(cfg)
    msgs = snap.values.get("messages", [])
    reply = ""
    for m in msgs[start:]:
        if isinstance(m, AIMessage) and message_text(m).strip() and not m.tool_calls:
            reply = message_text(m).strip()
    pending = list(msgs[-1].tool_calls) if snap.next and msgs and isinstance(msgs[-1], AIMessage) else []
    return TurnResult(reply=reply, pending=pending, lead_score=snap.values.get("lead_score") or {})


def run_turn(graph, thread_id: str, text: str, lead: Optional[dict] = None) -> TurnResult:
    cfg = config_for(thread_id)
    snap = graph.get_state(cfg)
    if snap.next:  # a booking is still waiting for the broker; don't start a new run over it
        return TurnResult(reply="", pending=list(snap.values["messages"][-1].tool_calls))
    start = len(snap.values.get("messages", []))
    inputs: dict = {"messages": [HumanMessage(content=text)]}
    if lead:
        inputs["lead"] = lead
    graph.invoke(inputs, cfg)
    return _result(graph, cfg, start)


def resume(graph, thread_id: str, approve: bool, reason: str = "") -> TurnResult:
    cfg = config_for(thread_id)
    snap = graph.get_state(cfg)
    if not snap.next:
        return TurnResult(reply="")
    start = len(snap.values["messages"])
    if not approve:
        calls = snap.values["messages"][-1].tool_calls
        graph.update_state(cfg, {"messages": [
            ToolMessage(tool_call_id=tc["id"], content=(
                f"The broker declined this. Reason: {reason or 'not given'}. Do not retry the same slot; "
                "apologise briefly and offer two other options."))
            for tc in calls
        ]}, as_node="sensitive_appointment_tools")
    graph.invoke(None, cfg)
    return _result(graph, cfg, start)


def describe_pending(tool_calls: list[dict], state_values: Optional[dict] = None) -> str:
    """Readable summary of what the broker is being asked to approve."""
    lead = (state_values or {}).get("lead") or {}
    score = (state_values or {}).get("lead_score") or {}
    lines = []
    for tc in tool_calls:
        a = tc["args"]
        if tc["name"] == "book_site_visit":
            listing = db.get_listing(str(a.get("listing_id", "")).upper()) or {}
            when = a.get("start_time_ist", "?")
            try:
                when = fmt_ist(parse_ist(when))
            except ValueError:
                pass
            lines.append(
                f"Book {'video walkthrough' if a.get('mode') == 'video' else 'site visit'}: {a.get('customer_name')} "
                f"{lead.get('phone') or ''}\n{when}\n{listing.get('project', a.get('listing_id'))}, {listing.get('locality', '')}"
                f" ({format_inr(listing.get('price_inr'))})"
                + (f"\nLead score {score.get('total')} ({score.get('band')})" if score else "")
            )
        elif tc["name"] == "reschedule_appointment":
            lines.append(f"Reschedule visit #{a.get('appointment_id')} to {a.get('new_start_time_ist')}")
        elif tc["name"] == "cancel_appointment":
            lines.append(f"Cancel visit #{a.get('appointment_id')} ({a.get('reason') or 'no reason given'})")
        else:
            lines.append(f"{tc['name']}: {a}")
    return "\n\n".join(lines)
