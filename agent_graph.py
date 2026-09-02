import json
import logging
from typing import Any, Literal
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from config import Config
from state import NutanixAgentState
from nodes import get_llm
from subgraphs import (
    build_network_subgraph,
    build_compute_subgraph,
    build_storage_subgraph,
)

# Suppress verbose schema transformation warnings from langchain_google_genai
logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

logger = logging.getLogger("ReflectiveNutanixGraph")


# ============================================================================
# PARENT PLANNER NODE
# ============================================================================

async def planner_node(state: NutanixAgentState) -> dict[str, Any]:
    """Parent Planner Node: Analyzes user request and formulates/refines a modular execution plan."""
    messages = list(state.get("messages", []))
    cluster_context = state.get("cluster_context", {})
    last_user_input = [m.content for m in messages if isinstance(m, HumanMessage)][-1] if messages else "Manage Nutanix Cluster"

    system_prompt = (
        "You are an expert Nutanix Prism Central Orchestrator & Planner.\n"
        "Analyze the user's objective and formulate a sequential plan mapping tasks to specialized subgraphs:\n"
        "- Network Subgraph: VPCs, Subnets, DNS, Routing tables, Static routes.\n"
        "- Compute Subgraph: VMs (e.g. Windows 2022 VM), NICs (e.g. Bastion attachment), Power state.\n"
        "- Storage Subgraph: Storage Containers, Cluster discovery, Inventory.\n\n"
        f"Active Cluster Context: {json.dumps(cluster_context)}\n\n"
        "IMPORTANT RULES:\n"
        "- For simple READ / LIST queries (e.g. 'list VMs', 'show storage containers', 'list subnets'), "
        "produce a SINGLE-STEP plan (e.g. 'List AHV Virtual Machines via Compute Subgraph').\n"
        "- For complex deployments (e.g. VPC + VMs), break down into clear domain steps:\n"
        "  1. Create VPC with DNS server entry and Transit subnets (Network)\n"
        "  2. Attach Linux Bastion VM NIC to Transit-NonERP-01 subnet (Compute)\n"
        "  3. Create Windows 2022 VM on storage container nkp (Compute)\n"
        "  4. Retrieve Route Table and configure default 0.0.0.0/0 static route (Network)\n"
        "  5. Assign Floating IPs from External Subnet to Linux Bastion VM and Windows VM (Network)\n"
        "Respond with a JSON object format:\n"
        '{\n  "plan": ["1. Step one description"]\n}'
    )

    llm = get_llm()
    plan = []
    if llm:
        planner_messages = [SystemMessage(content=system_prompt), HumanMessage(content=last_user_input)]
        try:
            res = await llm.ainvoke(planner_messages)
            content = res.content if hasattr(res, "content") else str(res)
            if "{" in content and "}" in content:
                json_str = content[content.find("{"):content.rfind("}") + 1]
                data = json.loads(json_str)
                plan = data.get("plan", [])
        except Exception as exc:
            logger.warning(f"Planner LLM parsing fallback: {exc}")

    if not plan:
        query_lower = last_user_input.lower()
        if "storage" in query_lower or "container" in query_lower:
            plan = ["1. List Storage Containers via Storage Subgraph"]
        elif "subnet" in query_lower or "network" in query_lower:
            plan = ["1. List Network Subnets via Network Subgraph"]
        elif "vpc" in query_lower and "create" in query_lower:
            plan = [
                "1. Create VPC with DNS server entry configured in commonDhcpOptions and external/ERP attachments",
                "2. Create Transit Subnets (Transit-ERP-01 and Transit-NonERP-01)",
                "3. Attach Linux Bastion VM to Transit-NonERP-01 subnet with static IP 20.20.20.14",
                "4. Create Windows VM (Image: Windows 2022, Container: nkp, 8 vCPUs, 10 GB RAM, 110 GB Boot Disk, IP: 20.20.20.17)",
                "5. Retrieve Route Table for the newly created VPC",
                "6. Create default static route (0.0.0.0/0) with external network next hop",
                "7. Assign Floating IPs from External Subnet to Linux Bastion VM and Windows VM"
            ]
        elif "vm" in query_lower or "virtual" in query_lower:
            plan = ["1. List AHV Virtual Machines via Compute Subgraph"]
        else:
            plan = ["1. Inspect Prism Central Inventory via Discovery Subgraph"]

    plan_summary = "\n".join([f"  • {step}" for step in plan])
    plan_msg = AIMessage(content=f"Formulated Execution Plan:\n{plan_summary}")

    return {
        "plan": plan,
        "current_step": 0,
        "critique": "Plan initialized. Ready for execution step 1.",
        "messages": [plan_msg],
    }


# ============================================================================
# PARENT REVIEWER & RECOVERY NODES
# ============================================================================

async def reviewer_node(state: NutanixAgentState) -> dict[str, Any]:
    """Parent Reviewer Node: Reflects on subgraph output, writes critique, and advances plan step."""
    messages = state.get("messages", [])
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)
    error_trace = state.get("error_trace")

    last_msg = messages[-1] if messages else None
    last_content = str(last_msg.content) if last_msg else ""

    step_failed = bool(error_trace or "error" in last_content.lower() or '"ok": false' in last_content.lower())

    if error_trace:
        critique = f"Step {current_step + 1} encountered error: {error_trace.get('error') or error_trace.get('exception') or 'Execution error'}."
    elif step_failed:
        critique = f"Step {current_step + 1} failed. Reviewing error output."
    else:
        critique = f"Step {current_step + 1} ({plan[current_step] if current_step < len(plan) else 'Step'}) completed successfully by subgraph."

    review_msg = AIMessage(content=f"[ORCHESTRATOR REVIEW]: {critique}")
    next_step = current_step + 1 if not step_failed else current_step

    return {
        "critique": critique,
        "current_step": next_step,
        "messages": [review_msg],
    }


async def recovery_analysis_node(state: NutanixAgentState) -> dict[str, Any]:
    """Parent Recovery Node: Synthesizes failure trace and recommends remediation."""
    error_trace = state.get("error_trace") or {}
    tool_name = error_trace.get("tool_name", "unknown_tool")
    operation = error_trace.get("operation", "unknown_operation")
    details = error_trace.get("raw_output") or error_trace.get("exception") or error_trace.get("error", "No detail")
    current_retries = state.get("retry_count", 0) + 1

    if current_retries >= 2:
        final_msg = AIMessage(
            content=(
                f"Execution stopped after recovery attempts for '{operation}' on tool '{tool_name}'.\n"
                f"Root Cause: {details}\n"
                "Please verify network connectivity to Prism Central (PC_HOST) and confirm credentials."
            )
        )
        return {"messages": [final_msg], "error_trace": None, "retry_count": current_retries}

    diagnosis = (
        f"[RECOVERY ANALYSIS - Attempt {current_retries}] Subgraph operation '{operation}' failed on '{tool_name}'.\n"
        f"Error Details: {details}\n"
        "Recommendation: Re-check operation schema or inspect parameters."
    )

    return {"messages": [SystemMessage(content=diagnosis)], "error_trace": None, "retry_count": current_retries}


# ============================================================================
# DOMAIN ROUTER LOGIC
# ============================================================================

def route_to_domain_subgraph(state: NutanixAgentState) -> Literal["network_subgraph", "compute_subgraph", "storage_subgraph", "__end__"]:
    """Inspects the active plan step and routes to the specialized domain subgraph."""
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)

    if not plan or current_step >= len(plan):
        return END

    active_step = plan[current_step].lower()

    # Networking domain
    if any(k in active_step for k in ("vpc", "subnet", "network", "route", "gateway", "dhcp", "dns", "floating", "fip")):
        return "network_subgraph"

    # Storage & inventory domain
    if any(k in active_step for k in ("storage", "container", "cluster", "disk", "inventory", "schema")):
        return "storage_subgraph"

    # Compute domain (default for VMs, NICs, Bastion)
    return "compute_subgraph"


def route_after_reviewer(state: NutanixAgentState) -> Literal["network_subgraph", "compute_subgraph", "storage_subgraph", "recovery_analysis", "__end__"]:
    """Conditional edge routing after Parent Reviewer evaluation."""
    critique = state.get("critique", "").lower()
    retry_count = state.get("retry_count", 0)

    if retry_count >= 2 or "error" in critique or "failed" in critique or "rejection" in critique:
        if state.get("error_trace"):
            return "recovery_analysis"
        return END

    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)

    if current_step < len(plan):
        return route_to_domain_subgraph(state)

    logger.info("All modular plan steps completed across subgraphs. Ending graph turn.")
    return END


# ============================================================================
# GRAPH BUILDER
# ============================================================================

def build_reflective_nutanix_graph():
    """Constructs the hierarchical Orchestrator graph with Network, Compute, and Storage subgraphs."""
    # Build isolated domain subgraphs
    network_subgraph = build_network_subgraph()
    compute_subgraph = build_compute_subgraph()
    storage_subgraph = build_storage_subgraph()

    # Parent Orchestrator workflow
    workflow = StateGraph(NutanixAgentState)

    # Register high-level nodes & compiled subgraphs
    workflow.add_node("planner", planner_node)
    workflow.add_node("network_subgraph", network_subgraph)
    workflow.add_node("compute_subgraph", compute_subgraph)
    workflow.add_node("storage_subgraph", storage_subgraph)
    workflow.add_node("reviewer", reviewer_node)
    workflow.add_node("recovery_analysis", recovery_analysis_node)

    workflow.set_entry_point("planner")

    # Routing from planner to domain subgraphs
    workflow.add_conditional_edges("planner", route_to_domain_subgraph)

    # All subgraphs flow into parent reviewer
    workflow.add_edge("network_subgraph", "reviewer")
    workflow.add_edge("compute_subgraph", "reviewer")
    workflow.add_edge("storage_subgraph", "reviewer")

    # Routing after reviewer to next subgraph step, recovery, or end
    workflow.add_conditional_edges("reviewer", route_after_reviewer)
    workflow.add_edge("recovery_analysis", "reviewer")

    checkpointer = MemorySaver()
    return workflow.compile(checkpointer=checkpointer)
