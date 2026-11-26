from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from langgraph.prebuilt import tools_condition

from src.util.appointment_tools import safe_tools, sensitive_tool_names, sensitive_tools
from src.util.general_tools import CompleteOrEscalate
from src.util.prompts import appointment_agent_prompt
from src.util.state import State


def build_appointment_agent(llm):
    prompt = ChatPromptTemplate.from_messages([("system", appointment_agent_prompt), ("placeholder", "{messages}")])
    return prompt | llm.bind_tools(safe_tools + sensitive_tools + [CompleteOrEscalate])


def route_appointment_tools(state: State) -> Literal[
    "__end__", "leave_specialized_agent", "safe_appointment_tools", "sensitive_appointment_tools"
]:
    if tools_condition(state) == "__end__":
        return "__end__"
    names = [tc["name"] for tc in state["messages"][-1].tool_calls]
    if CompleteOrEscalate.__name__ in names:
        return "leave_specialized_agent"
    if any(n in sensitive_tool_names for n in names):
        return "sensitive_appointment_tools"
    return "safe_appointment_tools"
