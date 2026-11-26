"""Transparent lead score out of 100. Each factor has a reason the broker can read.

Same rubric as the pitch demo:
  Timeline 25 · Budget fit 20 · Financing 15 · Buying intent 20 · Contact info 10 · Engagement 10
  Hot >= 75, Warm 50-74, Cold < 50
"""
from __future__ import annotations

from src.util.india import format_inr


def _timeline_points(lead: dict) -> tuple[int, str]:
    months = lead.get("timeline_months")
    text = lead.get("timeline")
    if months is None and not text:
        return 0, "Not asked yet"
    label = text or f"{months} months"
    if months is None:
        return 8, label
    if months <= 2:
        return 25, label
    if months <= 4:
        return 18, label
    if months <= 6:
        return 10, label
    if months <= 12:
        return 5, label
    return 3, label


def score_lead(lead: dict, criteria: dict, matches: list[dict], human_turns: int, match_note: str = "") -> dict:
    factors = []

    pts, note = _timeline_points(lead)
    factors.append({"factor": "Timeline", "points": pts, "max": 25, "reason": note})

    max_price = criteria.get("max_price")
    if not max_price:
        bf, bn = 0, "Budget not shared"
    elif matches and "above" not in match_note:
        bf, bn = 20, f"{len(matches)} listings within {format_inr(max_price)}"
    elif matches:
        bf, bn = 10, "Closest options are above budget"
    else:
        bf, bn = 6, f"Budget up to {format_inr(max_price)}, no stock yet"
    factors.append({"factor": "Budget fit", "points": bf, "max": 20, "reason": bn})

    fin = lead.get("financing")
    fp = 15 if fin == "ready" else 9 if fin == "needs_loan" else 3
    factors.append({"factor": "Financing", "points": fp, "max": 15,
                    "reason": {"ready": "Loan sanctioned / own funds", "needs_loan": "Needs a home loan"}.get(fin, "Unknown")})

    intent, why = 0, []
    if lead.get("wants_visit"):
        intent += 8
        why.append("asked for a video tour" if lead.get("visit_mode") == "video" else "asked for a site visit")
    if criteria.get("locality"):
        intent += 4
        why.append("specific locality")
    elif criteria.get("city"):
        intent += 2
        why.append("city only")
    if criteria.get("bhk"):
        intent += 3
    if lead.get("purpose") or criteria.get("ready_to_move") is not None:
        intent += 3
    factors.append({"factor": "Buying intent", "points": min(intent, 20), "max": 20, "reason": ", ".join(why) or "No signals yet"})

    contact = (5 if lead.get("phone") else 1) + (3 if lead.get("name") else 0)
    factors.append({"factor": "Contact info", "points": contact, "max": 10,
                    "reason": ("Number known" if lead.get("phone") else "No number yet") + (" + name" if lead.get("name") else "")})

    eng = min(10, human_turns * 2)
    factors.append({"factor": "Engagement", "points": eng, "max": 10, "reason": f"{human_turns} messages"})

    total = sum(f["points"] for f in factors)
    band = "Hot" if total >= 75 else "Warm" if total >= 50 else "Cold"
    next_action = {
        "Hot": "Call within the hour and lock a site visit.",
        "Warm": "Keep sharing matches and offer visit slots.",
        "Cold": "Add to the weekly shortlist message.",
    }[band]
    return {"total": total, "band": band, "factors": factors, "next_action": next_action}
