from typing import Annotated, Any, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


def merge_cluster_context(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Reducer function to merge cluster context dict updates while preserving existing keys."""
    res = dict(left) if left else {}
    if right:
        res.update(right)
    return res


class NutanixAgentState(TypedDict):
    """Reflective TypedDict state schema for Nutanix Prism Central LangGraph agent.
    
    Attributes:
        messages: Conversation history, annotated with `add_messages` for automatic appending.
        plan: Sequential execution steps formulated by the planner node (list of strings).
        current_step: Integer tracking active step index in the plan (0-indexed).
        critique: Reviewer node's reflection and evaluation of the previous tool execution.
        cluster_context: Dedicated dictionary to retain transient entity UUIDs and Prism Central state
                         across iterative tool calls (e.g. vm_uuid, cluster_uuid, storage_container_uuid).
        pending_tool_call: Stores details of state-mutating tool calls awaiting human authorization.
        approval_granted: Boolean flag set during HITL resume/authorization.
        error_trace: Formatted error payload populated when tool execution fails, for recovery routing.
        retry_count: Counter tracking consecutive tool recovery retries to prevent endless loops.
    """
    messages: Annotated[list[BaseMessage], add_messages]
    plan: list[str]
    current_step: int
    critique: str
    cluster_context: Annotated[dict[str, Any], merge_cluster_context]
    pending_tool_call: dict[str, Any] | None
    approval_granted: bool | None
    error_trace: dict[str, Any] | None
    retry_count: int
