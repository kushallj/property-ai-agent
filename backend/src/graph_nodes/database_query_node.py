from src.util.create_node import ack_tool_calls
from src.util.db import search_listings
from src.util.india import emi, format_inr
from src.util.state import State


def describe(m: dict) -> str:
    status = "Ready to move" if m["ready_to_move"] else f"Possession {m['possession']}"
    per_sqft = round(m["price_inr"] / m["carpet_sqft"])
    return (f"{m['listing_id']} | {m['project']} by {m['builder']}, {m['locality']}, {m['city']} | {m['bhk']}BHK, "
            f"{m['carpet_sqft']} sq ft carpet | {format_inr(m['price_inr'])} (₹{per_sqft:,}/sq ft, EMI about "
            f"{format_inr(emi(m['price_inr']))}/month on 80% loan) | {status} | RERA {m['rera_id']} | "
            f"{m['amenities']} | {m['highlight']}")


def query_database(state: State) -> dict:
    crit = state.get("search_criteria") or {}
    lead = state.get("lead") or {}
    last = state["messages"][-1]

    missing = [k for k, ok in [("location", crit.get("city")), ("BHK", crit.get("bhk")), ("budget", crit.get("max_price"))] if not ok]
    if missing:
        return {
            "matches": state.get("matches") or [],
            "messages": ack_tool_calls(last, f"Details saved. Before searching, ask the customer for: {missing[0]} (one question only)."),
        }

    matches, note = search_listings(crit)
    if not matches:
        result = "No listings match yet. Tell the customer we will message them as soon as something fits, and ask if they are flexible on locality or budget."
    else:
        result = "Matching listings" + (f" ({note})" if note else "") + ":\n" + "\n".join(describe(m) for m in matches)
        if not (lead.get("timeline_months") or lead.get("timeline")):
            result += "\nAfter sharing these, ask when they plan to buy or move in."
        else:
            result += "\nShare these briefly, then offer a site visit (video walkthrough for NRI customers)."
    return {"matches": matches, "match_note": note, "messages": ack_tool_calls(last, result)}
