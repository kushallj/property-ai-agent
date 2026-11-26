from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langgraph.prebuilt import ToolNode

from src.util import memory
from src.util.india import format_inr, now_ist
from src.util.llm import assistant_name, firm_name
from src.util.state import State


def message_text(msg) -> str:
    """Text of a message whether content is a string or a list of content blocks."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def lead_context(state: State, thread_id: str | None = None) -> str:
    lead = state.get("lead") or {}
    crit = state.get("search_criteria") or {}
    lines = []
    who = [lead.get("name") or "name unknown", lead.get("phone") or "phone unknown"]
    if lead.get("source"):
        who.append(f"came via {lead['source']}")
    if lead.get("nri"):
        who.append("NRI")
    lines.append("Customer: " + ", ".join(who))

    req = []
    if crit.get("bhk"):
        req.append(f"{crit['bhk']}BHK")
    if crit.get("locality") or crit.get("city"):
        req.append("in " + ", ".join(x for x in [crit.get("locality"), crit.get("city")] if x))
    if crit.get("max_price"):
        req.append(f"budget {format_inr(crit.get('min_price'))} to {format_inr(crit['max_price'])}")
    if crit.get("ready_to_move") is not None:
        req.append("ready to move only" if crit["ready_to_move"] else "under construction OK")
    lines.append("Requirement: " + (" ".join(req) if req else "not shared yet"))

    profile = []
    if lead.get("timeline") or lead.get("timeline_months"):
        profile.append(f"timeline {lead.get('timeline') or str(lead['timeline_months']) + ' months'}")
    if lead.get("financing"):
        profile.append({"ready": "loan sanctioned / own funds", "needs_loan": "needs home loan"}.get(lead["financing"], lead["financing"]))
    if lead.get("purpose"):
        profile.append(lead["purpose"].replace("_", " "))
    if lead.get("language"):
        profile.append(f"writes in {lead['language']}")
    if profile:
        lines.append("Profile: " + ", ".join(profile))

    missing = [k for k, ok in [("location", crit.get("city")), ("BHK", crit.get("bhk")),
                               ("budget", crit.get("max_price")), ("timeline", lead.get("timeline_months") or lead.get("timeline"))] if not ok]
    lines.append("Still missing: " + (", ".join(missing) if missing else "nothing"))

    matches = state.get("matches") or []
    if matches:
        lines.append("Matched listings shared:")
        for m in matches:
            lines.append(f"  - {m['listing_id']}: {m['project']}, {m['locality']}, {m['bhk']}BHK, {format_inr(m['price_inr'])}")
    score = state.get("lead_score") or {}
    if score:
        lines.append(f"Lead score: {score.get('total')} ({score.get('band')})")
    notes = memory.as_context(thread_id) if thread_id else ""
    if notes:
        lines.append("Remember about this customer (from earlier chats and the broker):\n" + notes)
    return "\n".join(lines)


class Assistant:
    """Runs an LLM agent with the per-turn prompt variables filled in."""

    def __init__(self, runnable: Runnable):
        self.runnable = runnable

    def __call__(self, state: State, config: RunnableConfig):
        inputs = {
            **state,
            "time": now_ist().strftime("%A %d %B %Y, %I:%M %p IST"),
            "lead_context": lead_context(state, config.get("configurable", {}).get("thread_id")),
            "firm_name": firm_name(),
            "assistant_name": assistant_name(),
        }
        result = None
        for _ in range(3):
            result = self.runnable.invoke(inputs, config)
            if result.tool_calls or message_text(result).strip():
                break
            inputs["messages"] = list(inputs["messages"]) + [("user", "Respond with a real output.")]
        return {"messages": result}


def ack_tool_calls(ai_message: AIMessage, first: str, rest: str = "Skipped: handle one request at a time.") -> list[ToolMessage]:
    """Every tool call needs a matching ToolMessage, or the next LLM call fails."""
    return [
        ToolMessage(content=first if i == 0 else rest, tool_call_id=tc["id"])
        for i, tc in enumerate(ai_message.tool_calls)
    ]


def enter_appointment_agent(state: State) -> dict:
    return {"messages": ack_tool_calls(state["messages"][-1],
                                       "You are now the appointment specialist. Help the customer with their visit request.")}


def back_to_main(state: State) -> dict:
    return {"messages": ack_tool_calls(state["messages"][-1], "Back to the main assistant. Continue helping the customer.")}


def handle_tool_error(state) -> dict:
    error = state.get("error")
    return {
        "messages": [
            ToolMessage(content=f"Error: {error!r}. Fix the input and try again.", tool_call_id=tc["id"])
            for tc in state["messages"][-1].tool_calls
        ]
    }


def create_tool_node(tools: list):
    return ToolNode(tools).with_fallbacks([RunnableLambda(handle_tool_error)], exception_key="error")
