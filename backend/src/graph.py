from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph

from src.graph_nodes.appointment_agent import build_appointment_agent, route_appointment_tools
from src.graph_nodes.database_query_node import query_database
from src.graph_nodes.lead_scorer import lead_scorer
from src.graph_nodes.main_agent import build_main_agent, route_main_agent
from src.graph_nodes.search_criteria_agent import build_search_criteria_agent
from src.util.appointment_tools import safe_tools, sensitive_tools
from src.util.create_node import Assistant, back_to_main, create_tool_node, enter_appointment_agent
from src.util.state import State


def create_graph(llm=None, checkpointer=None):
    """main_agent -> (search_criteria_agent -> query_database -> lead_scorer -> main_agent)
                  -> (enter_appointment_agent -> appointment_agent <-> tools), with broker approval
                     before any booking, reschedule or cancellation."""
    if llm is None:
        from src.util.llm import get_llm
        llm = get_llm()

    builder = StateGraph(State)

    builder.add_node("main_agent", Assistant(build_main_agent(llm)))
    builder.add_node("leave_specialized_agent", back_to_main)
    builder.add_edge(START, "main_agent")
    builder.add_conditional_edges("main_agent", route_main_agent)
    builder.add_edge("leave_specialized_agent", "main_agent")

    builder.add_node("search_criteria_agent", build_search_criteria_agent(llm))
    builder.add_node("query_database", query_database)
    builder.add_node("lead_scorer", lead_scorer)
    builder.add_edge("search_criteria_agent", "query_database")
    builder.add_edge("query_database", "lead_scorer")
    builder.add_edge("lead_scorer", "main_agent")

    builder.add_node("enter_appointment_agent", enter_appointment_agent)
    builder.add_node("appointment_agent", Assistant(build_appointment_agent(llm)))
    builder.add_node("safe_appointment_tools", create_tool_node(safe_tools))
    builder.add_node("sensitive_appointment_tools", create_tool_node(sensitive_tools))
    builder.add_edge("enter_appointment_agent", "appointment_agent")
    builder.add_conditional_edges("appointment_agent", route_appointment_tools)
    builder.add_edge("safe_appointment_tools", "appointment_agent")
    builder.add_edge("sensitive_appointment_tools", "appointment_agent")

    return builder.compile(
        checkpointer=checkpointer or InMemorySaver(),
        interrupt_before=["sensitive_appointment_tools"],
    )
