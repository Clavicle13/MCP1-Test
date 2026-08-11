import logging
from typing import Literal
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from state import NutanixAgentState
from mcp_client import is_state_mutating_tool_call
from nodes import agent_node, human_approval_node, recovery_analysis_node, tool_execution_node

logger = logging.getLogger("NutanixGraph")


def route_after_agent(state: NutanixAgentState) -> Literal["human_approval", "tools", "__end__"]:
    """Conditional edge routing after agent LLM node execution."""
    if state.get("retry_count", 0) >= 2:
        logger.info("Maximum error recovery retry limit reached. Ending graph execution.")
        return END

    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])

    if not tool_calls:
        return END

    tool_call = tool_calls[0]
    tool_name = tool_call.get("name", "")
    tool_args = tool_call.get("args", {})

    # Evaluate whether tool call is state-mutating
    if is_state_mutating_tool_call(tool_name, tool_args):
        logger.info(f"State-mutating operation detected ({tool_name}: {tool_args.get('operation')}). Routing to human_approval node.")
        return "human_approval"
    else:
        logger.info(f"Read-only operation detected ({tool_name}: {tool_args.get('operation')}). Routing directly to tool_execution node.")
        return "tools"


def route_after_approval(state: NutanixAgentState) -> Literal["tools", "agent"]:
    """Conditional edge routing after HITL approval decision."""
    if state.get("approval_granted"):
        return "tools"
    return "agent"


def route_after_tool(state: NutanixAgentState) -> Literal["recovery_analysis", "agent"]:
    """Conditional fallback edge routing after tool execution."""
    if state.get("error_trace"):
        logger.warning("Tool execution error detected. Routing to recovery_analysis fallback node.")
        return "recovery_analysis"
    return "agent"


def build_nutanix_graph():
    """Constructs and compiles the reactive Nutanix Prism Central agent graph with MemorySaver checkpointer."""
    workflow = StateGraph(NutanixAgentState)

    # Register nodes
    workflow.add_node("agent", agent_node)
    workflow.add_node("human_approval", human_approval_node)
    workflow.add_node("tools", tool_execution_node)
    workflow.add_node("recovery_analysis", recovery_analysis_node)

    # Set entry point
    workflow.set_entry_point("agent")

    # Add conditional edges
    workflow.add_conditional_edges("agent", route_after_agent)
    workflow.add_conditional_edges("human_approval", route_after_approval)
    workflow.add_conditional_edges("tools", route_after_tool)
    workflow.add_edge("recovery_analysis", "agent")

    # Memory checkpointer for persistent state tracking across thread IDs
    checkpointer = MemorySaver()

    # Compile graph
    compiled_graph = workflow.compile(checkpointer=checkpointer)
    return compiled_graph
