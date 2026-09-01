import json
import logging
from typing import Any, Literal
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from config import Config
from state import NutanixAgentState
from mcp_client import is_state_mutating_tool_call, mcp_client_manager
from nodes import get_llm

# Suppress verbose schema transformation warnings from langchain_google_genai
logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

logger = logging.getLogger("ReflectiveNutanixGraph")


# ============================================================================
# NODES
# ============================================================================

async def planner_node(state: NutanixAgentState) -> dict[str, Any]:
    """Planner Node: Analyzes user request and formulates/refines a sequential execution plan."""
    messages = list(state.get("messages", []))
    cluster_context = state.get("cluster_context", {})
    last_user_input = [m.content for m in messages if isinstance(m, HumanMessage)][-1] if messages else "Manage Nutanix Cluster"

    system_prompt = (
        "You are an expert Nutanix Prism Central Planner.\n"
        "Analyze the user's objective and formulate a sequential execution plan using available Nutanix V4 API tools.\n"
        f"Active Cluster Context: {json.dumps(cluster_context)}\n\n"
        "IMPORTANT RULES:\n"
        "- For simple READ / LIST queries (e.g. 'list VMs', 'show storage containers', 'list subnets'), "
        "produce a SINGLE-STEP plan that directly invokes the MCP tool (e.g. 'Use vmm_execute ahv_listVms to list all AHV Virtual Machines').\n"
        "- Do NOT add display, extraction, or summary steps — the calling code handles presentation.\n"
        "- Only use multiple steps for complex workflows requiring chained API calls (e.g. create VM then power on).\n"
        "- VPC CREATION & ROUTING DIRECTIVES:\n"
        "  1. DNS Configuration: During VPC creation (`networking_execute` `createVpc`), ALWAYS include the captured DNS server IP in `commonDhcpOptions.domainNameServers` for both Transit and Spoke VPCs.\n"
        "  2. Default Static Route (0.0.0.0/0): After creating a VPC, plan steps to retrieve the VPC's Route Table (`listRouteTables` with filter `vpcReference eq '<vpc_ext_id>'`) and add a default static route (`createRouteForRouteTable`):\n"
        "     - For Transit VPC: Next hop must target the external network attachment.\n"
        "     - For Spoke VPC: Next hop must target the Transit VPC ERP subnet.\n"
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
            # Parse JSON plan or fallback to line splitting
            if "{" in content and "}" in content:
                json_str = content[content.find("{"):content.rfind("}") + 1]
                data = json.loads(json_str)
                plan = data.get("plan", [])
        except Exception as exc:
            logger.warning(f"Planner LLM parsing fallback: {exc}")

    if not plan:
        # Default single-step plan generation based on query keyword
        query_lower = last_user_input.lower()
        if "storage" in query_lower or "container" in query_lower:
            plan = ["1. Use storage_execute listStorageContainers to list all Nutanix Storage Containers"]
        elif "subnet" in query_lower or "network" in query_lower:
            plan = ["1. Use networking_execute listSubnets to list all Nutanix Network Subnets"]
        elif "vpc" in query_lower and "create" in query_lower:
            plan = [
                "1. Create VPC with DNS server entry configured in commonDhcpOptions and external/ERP attachments",
                "2. Retrieve Route Table for the newly created VPC",
                "3. Create default static route (0.0.0.0/0) with appropriate next hop (external network attachment for Transit VPC, Transit VPC ERP subnet for Spoke VPC)"
            ]
        elif "create" in query_lower or "delete" in query_lower or "update" in query_lower:
            plan = [
                "1. Inspect operation schema for requested mutation",
                "2. Submit state-mutating operation with parameters",
                "3. Verify task completion and entity status"
            ]
        else:
            plan = ["1. Use vmm_execute ahv_listVms to list all AHV Virtual Machines"]

    plan_summary = "\n".join([f"  • {step}" for step in plan])
    plan_msg = AIMessage(content=f"Formulated Execution Plan:\n{plan_summary}")

    return {
        "plan": plan,
        "current_step": 0,
        "critique": "Plan initialized. Ready for execution step 1.",
        "messages": [plan_msg],
    }


async def executor_node(state: NutanixAgentState) -> dict[str, Any]:
    """Executor Node: Executes the tool call required by the active plan step."""
    tools = mcp_client_manager.tools
    messages = list(state.get("messages", []))
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)
    cluster_context = state.get("cluster_context", {})
    critique = state.get("critique", "")

    active_step_desc = plan[current_step] if current_step < len(plan) else "Execute requested Nutanix operation"

    system_prompt = (
        "You are the Nutanix MCP Executor Agent managing Nutanix Prism Central resources.\n"
        f"CURRENT PLAN STEP [{current_step + 1}/{len(plan)}]: {active_step_desc}\n"
        f"REVIEWER CRITIQUE: {critique}\n"
        f"CLUSTER CONTEXT: {json.dumps(cluster_context, indent=2)}\n\n"
        "STRICT EXECUTION DIRECTIVES:\n"
        "1. To retrieve or list entity records (VMs, Storage Containers, Subnets, Clusters), directly invoke the primary namespace execution tool:\n"
        "   - For Virtual Machines: ALWAYS use tool 'vmm_execute' with operation 'ahv_listVms' (do NOT use 'esxi_listVms' or 'listVms').\n"
        "   - For Storage Containers: Use tool 'storage_execute' or 'clustermgmt_execute' with operation 'listStorageContainers'.\n"
        "   - For Network Subnets: Use tool 'networking_execute' with operation 'listSubnets'.\n"
        "2. VPC & ROUTING EXECUTION DIRECTIVES:\n"
        "   - When creating VPCs (Transit or Spoke), ALWAYS populate `commonDhcpOptions.domainNameServers` with the captured DNS server IP.\n"
        "   - When configuring default routes (0.0.0.0/0), use `createRouteForRouteTable` on the VPC's route table:\n"
        "     * Transit VPC: Set next hop to the external network attachment.\n"
        "     * Spoke VPC: Set next hop to the Transit VPC ERP subnet.\n"
        "3. Do NOT call schema discovery tools ('getOperationSchema', 'listOperations') when asked to list or inspect actual entities.\n"
        "4. Always select and invoke the exact tool call required to fetch live Prism Central entity data."
    )

    # Gemini requires message lists to end with user role (HumanMessage)
    user_step_request = HumanMessage(
        content=f"Execute active plan step [{current_step + 1}/{len(plan)}]: '{active_step_desc}'. Select the exact tool call required."
    )
    exec_messages = [SystemMessage(content=system_prompt), user_step_request]

    llm = get_llm()
    response = None

    if llm and tools:
        try:
            llm_with_tools = llm.bind_tools(tools)
            response = await llm_with_tools.ainvoke(exec_messages)
        except Exception as exc:
            logger.warning(f"Executor LLM API error (falling back to rule-based execution): {exc}")
            response = None

    if not response:
        # Fallback simulation response if offline / invalid or leaked LLM API key
        step_lower = active_step_desc.lower()
        if "storage" in step_lower:
            response = AIMessage(
                content=f"Executing step {current_step + 1}: {active_step_desc} (Rule-based Fallback)",
                tool_calls=[{"name": "storage_execute", "args": {"operation": "listStorageContainers"}, "id": f"call_exec_{current_step}", "type": "tool_call"}]
            )
        elif "subnet" in step_lower or "network" in step_lower:
            response = AIMessage(
                content=f"Executing step {current_step + 1}: {active_step_desc} (Rule-based Fallback)",
                tool_calls=[{"name": "networking_execute", "args": {"operation": "listSubnets"}, "id": f"call_exec_{current_step}", "type": "tool_call"}]
            )
        else:
            response = AIMessage(
                content=f"Executing step {current_step + 1}: {active_step_desc} (Rule-based Fallback)",
                tool_calls=[{"name": "vmm_execute", "args": {"operation": "listVms", "_limit": 5}, "id": f"call_exec_{current_step}", "type": "tool_call"}]
            )

    return {"messages": [response]}


async def human_approval_node(state: NutanixAgentState) -> dict[str, Any]:
    """Human-in-the-Loop approval node for state-mutating actions."""
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])

    if not tool_calls:
        return {"approval_granted": True, "pending_tool_call": None}

    tool_call = tool_calls[0]
    tool_name = tool_call.get("name", "")
    tool_args = tool_call.get("args", {})

    approval_request = {
        "action": "AUTHORIZATION_REQUIRED",
        "tool_name": tool_name,
        "operation": tool_args.get("operation", "N/A"),
        "arguments": tool_args,
        "cluster_context": state.get("cluster_context", {}),
        "warning": "CRITICAL: State-mutating operation (POST/PUT/DELETE) detected. Explicit human authorization required.",
    }

    human_response = interrupt(approval_request)

    authorized = False
    if isinstance(human_response, dict):
        authorized = human_response.get("approved", False)
    elif isinstance(human_response, str):
        authorized = human_response.strip().lower() in ("yes", "y", "true", "approve", "authorized")
    else:
        authorized = bool(human_response)

    if not authorized:
        rejection_msg = ToolMessage(
            content=json.dumps({"ok": False, "error": "Operation rejected by user during HITL authorization."}),
            tool_call_id=tool_call.get("id", "call_cancelled")
        )
        return {
            "messages": [rejection_msg],
            "approval_granted": False,
            "pending_tool_call": None,
        }

    return {"approval_granted": True, "pending_tool_call": tool_call}


async def tool_execution_node(state: NutanixAgentState) -> dict[str, Any]:
    """Tool execution node: Invokes MCP tools and updates cluster_context."""
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])

    if not tool_calls:
        return {}

    tools_by_name = {t.name: t for t in mcp_client_manager.tools}
    new_messages = []
    updated_context = dict(state.get("cluster_context", {}))
    error_trace = None

    for call in tool_calls:
        tool_name = call.get("name")
        tool_args = call.get("args", {})
        tool_id = call.get("id", "call_id")

        call_tool_result: Any = None
        tool_obj = tools_by_name.get(tool_name)

        if not tool_obj:
            err_msg = f"Tool '{tool_name}' not registered in Nutanix MCP server."
            new_messages.append(ToolMessage(content=json.dumps({"ok": False, "error": err_msg}), tool_call_id=tool_id))
            error_trace = {"tool_name": tool_name, "error": err_msg}
            continue

        try:
            call_tool_result = await tool_obj.ainvoke(tool_args)
            result_str = str(call_tool_result)

            if "execution_error" in result_str or '"ok": false' in result_str.lower():
                error_trace = {
                    "tool_name": tool_name,
                    "operation": tool_args.get("operation"),
                    "raw_output": result_str,
                }

            # Parse entity UUIDs into cluster_context
            try:
                data = json.loads(result_str) if isinstance(call_tool_result, str) else call_tool_result
                if isinstance(data, dict):
                    for key, val in data.items():
                        if isinstance(val, str) and ("extid" in key.lower() or "uuid" in key.lower()):
                            updated_context[key] = val
                    if "data" in data and isinstance(data["data"], dict):
                        for key, val in data["data"].items():
                            if isinstance(val, str) and ("extid" in key.lower() or "uuid" in key.lower()):
                                updated_context[key] = val
            except Exception:
                pass

            new_messages.append(ToolMessage(content=result_str, tool_call_id=tool_id))

        except Exception as exc:
            logger.error(f"Tool execution exception for {tool_name}: {exc}")
            err_payload = {"ok": False, "error": f"Tool execution exception: {str(exc)}"}
            new_messages.append(ToolMessage(content=json.dumps(err_payload), tool_call_id=tool_id))
            error_trace = {"tool_name": tool_name, "operation": tool_args.get("operation"), "exception": str(exc)}

    return {
        "messages": new_messages,
        "cluster_context": updated_context,
        "error_trace": error_trace,
    }


async def reviewer_node(state: NutanixAgentState) -> dict[str, Any]:
    """Reviewer Node: Reflects on tool output, writes critique, and determines plan step progress."""
    messages = state.get("messages", [])
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)
    error_trace = state.get("error_trace")

    last_msg = messages[-1] if messages else None
    last_content = str(last_msg.content) if last_msg else ""

    step_failed = bool(error_trace or '"ok": false' in last_content.lower())

    if error_trace:
        critique = f"Step {current_step + 1} encountered tool error: {error_trace.get('error') or error_trace.get('exception') or 'Execution error'}. Routing to recovery."
    elif '"ok": false' in last_content.lower():
        critique = f"Step {current_step + 1} tool call returned error payload. Plan revision needed."
    else:
        critique = f"Step {current_step + 1} ({plan[current_step] if current_step < len(plan) else 'Step'}) completed successfully."

    review_msg = AIMessage(content=f"[REVIEWER CRITIQUE]: {critique}")

    # Explicitly increment current_step in state update dictionary if step succeeded
    next_step = current_step + 1 if not step_failed else current_step

    return {
        "critique": critique,
        "current_step": next_step,
        "messages": [review_msg],
    }


async def recovery_analysis_node(state: NutanixAgentState) -> dict[str, Any]:
    """Recovery Node: Synthesizes failure trace and suggests remediation."""
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
        f"[RECOVERY ANALYSIS - Attempt {current_retries}] Tool '{tool_name}' failed during operation '{operation}'.\n"
        f"Error Details: {details}\n"
        "Recommendation: Re-check operation schema or inspect parameters."
    )

    return {"messages": [SystemMessage(content=diagnosis)], "error_trace": None, "retry_count": current_retries}


# ============================================================================
# CONDITIONAL ROUTING EDGES
# ============================================================================

def route_after_planner(state: NutanixAgentState) -> Literal["executor", "__end__"]:
    plan = state.get("plan", [])
    if not plan:
        return END
    return "executor"


def route_after_executor(state: NutanixAgentState) -> Literal["human_approval", "tools", "reviewer"]:
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])

    if not tool_calls:
        return "reviewer"

    tool_call = tool_calls[0]
    tool_name = tool_call.get("name", "")
    tool_args = tool_call.get("args", {})

    if is_state_mutating_tool_call(tool_name, tool_args):
        return "human_approval"
    return "tools"


def route_after_approval(state: NutanixAgentState) -> Literal["tools", "reviewer"]:
    if state.get("approval_granted"):
        return "tools"
    return "reviewer"


def route_after_tools(state: NutanixAgentState) -> Literal["recovery_analysis", "reviewer"]:
    if state.get("error_trace"):
        return "recovery_analysis"
    return "reviewer"


def route_after_reviewer(state: NutanixAgentState) -> Literal["executor", "planner", "recovery_analysis", "__end__"]:
    critique = state.get("critique", "").lower()
    retry_count = state.get("retry_count", 0)

    # Stop graph turn if tool execution failed or error recovery was triggered
    if retry_count >= 1 or "error" in critique or "failed" in critique or "rejection" in critique:
        logger.info("Tool execution failed or error recovery triggered. Ending graph turn.")
        return END

    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)

    # If there are remaining steps in the plan, proceed to executor
    if current_step < len(plan):
        return "executor"

    # All plan steps completed! End graph turn cleanly.
    logger.info("All plan steps completed. Ending graph turn.")
    return END


# ============================================================================
# GRAPH BUILDER
# ============================================================================

def build_reflective_nutanix_graph():
    """Constructs cyclical Plan-Execute-Review reflective LangGraph workflow."""
    workflow = StateGraph(NutanixAgentState)

    workflow.add_node("planner", planner_node)
    workflow.add_node("executor", executor_node)
    workflow.add_node("human_approval", human_approval_node)
    workflow.add_node("tools", tool_execution_node)
    workflow.add_node("reviewer", reviewer_node)
    workflow.add_node("recovery_analysis", recovery_analysis_node)

    workflow.set_entry_point("planner")

    workflow.add_conditional_edges("planner", route_after_planner)
    workflow.add_conditional_edges("executor", route_after_executor)
    workflow.add_conditional_edges("human_approval", route_after_approval)
    workflow.add_conditional_edges("tools", route_after_tools)
    workflow.add_conditional_edges("reviewer", route_after_reviewer)
    workflow.add_edge("recovery_analysis", "reviewer")

    checkpointer = MemorySaver()
    return workflow.compile(checkpointer=checkpointer)
