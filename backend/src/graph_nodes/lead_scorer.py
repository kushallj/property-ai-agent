from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from src.tenancy import from_config
from src.util import db
from src.util.india import format_inr
from src.util.lead_scoring import score_lead
from src.util.notify import notify_broker
from src.util.state import State


def lead_scorer(state: State, config: RunnableConfig) -> dict:
    tenant = from_config(config)
    lead = state.get("lead") or {}
    crit = state.get("search_criteria") or {}
    matches = state.get("matches") or []
    turns = sum(isinstance(m, HumanMessage) for m in state["messages"])
    score = score_lead(lead, crit, matches, turns, state.get("match_note") or "")

    thread_id = config.get("configurable", {}).get("thread_id", "unknown")
    row = db.upsert_lead(thread_id, lead, crit, score, tenant_id=tenant.id)

    if score["band"] == "Hot" and not row.get("broker_alerted"):
        where = ", ".join(x for x in [crit.get("locality"), crit.get("city")] if x)
        notify_broker(
            f"Hot lead ({score['total']}/100): {lead.get('name') or 'Unknown'} {lead.get('phone') or ''}\n"
            f"{crit.get('bhk', '?')}BHK in {where}, {format_inr(crit.get('min_price'))}–{format_inr(crit.get('max_price'))}\n"
            f"Timeline: {lead.get('timeline') or '?'} · Source: {lead.get('source') or '?'}\n"
            f"{score['next_action']}",
            tenant,
        )
        db.mark_broker_alerted(thread_id)
    return {"lead_score": score}
