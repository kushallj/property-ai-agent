from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from langgraph.prebuilt import tools_condition

from src.util.general_tools import ToAppointmentAgent, ToSearchAgent
from src.util.prompts import main_agent_prompt
from src.util.state import State

main_tools = [ToSearchAgent, ToAppointmentAgent]


def build_main_agent(llm):
    prompt = ChatPromptTemplate.from_messages([("system", main_agent_prompt), ("placeholder", "{messages}")])
    return prompt | llm.bind_tools(main_tools)


def route_main_agent(state: State) -> Literal["__end__", "search_criteria_agent", "enter_appointment_agent"]:
    if tools_condition(state) == "__end__":
        return "__end__"
    name = state["messages"][-1].tool_calls[0]["name"]
    if name == ToSearchAgent.__name__:
        return "search_criteria_agent"
    if name == ToAppointmentAgent.__name__:
        return "enter_appointment_agent"
    raise ValueError(f"Unknown tool call from main agent: {name}")
