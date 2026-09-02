import json
import logging
from typing import Any, Literal
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from config import Config
from state import NutanixAgentState
from mcp_client import is_state_mutating_tool_call, mcp_client_manager
from nodes import get_llm

logger = logging.getLogger("NutanixSubgraphs")


# ============================================================================
# HELPER: SHARED TOOL EXECUTOR FOR SUBGRAPHS
# ============================================================================

async def _execute_mcp_tool_calls(
    tool_calls: list[dict[str, Any]],
    current_context: dict[str, Any]
) -> tuple[list[ToolMessage], dict[str, Any], dict[str, Any] | None]:
    """Helper to execute tool calls against Nutanix MCP server and extract UUID context."""
    tools_by_name = {t.name: t for t in mcp_client_manager.tools}
    new_messages: list[ToolMessage] = []
    updated_context = dict(current_context or {})
    error_trace = None

    for call in tool_calls:
        tool_name = call.get("name")
        tool_args = call.get("args", {})
        tool_id = call.get("id", "call_id")

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

            # Parse entity UUIDs automatically into cluster_context
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
                    elif "payload" in data and isinstance(data["payload"], dict):
                        for key, val in data["payload"].items():
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

    return new_messages, updated_context, error_trace


# ============================================================================
# 1. NETWORK SUBGRAPH (VPC, Subnets, Routing)
# ============================================================================

async def network_executor_node(state: NutanixAgentState) -> dict[str, Any]:
    """Network Subgraph: Plans and invokes networking-specific MCP tools."""
    tools = mcp_client_manager.tools
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)
    cluster_context = state.get("cluster_context", {})
    critique = state.get("critique", "")
    active_step_desc = plan[current_step] if current_step < len(plan) else "Manage Nutanix Networking"

    system_prompt = (
        "You are the Nutanix Prism Central Network Specialist Subgraph.\n"
        f"CURRENT STEP [{current_step + 1}/{len(plan)}]: {active_step_desc}\n"
        f"PREVIOUS CRITIQUE: {critique}\n"
        f"CLUSTER CONTEXT: {json.dumps(cluster_context, indent=2)}\n\n"
        "NETWORKING DIRECTIVES:\n"
        "1. To list network subnets: Use 'networking_execute' with operation 'listSubnets'.\n"
        "2. To create VPC: Use 'networking_execute' with operation 'createVpc'. ALWAYS populate 'commonDhcpOptions.domainNameServers' with DNS IP.\n"
        "3. To create Subnet: Use 'networking_execute' with operation 'createSubnet' under the respective VPC.\n"
        "4. To list Route Tables: Use 'networking_execute' with operation 'listRouteTables' filtering by vpcReference.\n"
        "5. To create Static Route: Use 'networking_execute' with operation 'createRouteForRouteTable' on VPC route table (0.0.0.0/0 default route).\n"
        "6. To assign Floating IP: Use 'networking_execute' with operation 'createFloatingIp' targeting the external subnet and VM NIC.\n"
        "7. Output the exact tool call required."
    )

    user_request = HumanMessage(content=f"Execute network action for step: '{active_step_desc}'.")
    llm = get_llm()
    response = None

    if llm and tools:
        try:
            llm_with_tools = llm.bind_tools(tools)
            response = await llm_with_tools.ainvoke([SystemMessage(content=system_prompt), user_request])
        except Exception as exc:
            logger.warning(f"Network LLM error (using rule-based fallback): {exc}")
            response = None

    if not response:
        step_lower = active_step_desc.lower()
        if "floating" in step_lower or "fip" in step_lower:
            response = AIMessage(
                content=f"[Network Subgraph] Creating Floating IP for step: {active_step_desc}",
                tool_calls=[{"name": "networking_execute", "args": {"operation": "createFloatingIp", "request_body": {"name": "FIP-LinuxTools"}}, "id": f"call_net_{current_step}", "type": "tool_call"}]
            )
        elif "route" in step_lower:
            response = AIMessage(
                content=f"[Network Subgraph] Configuring Route Table for step: {active_step_desc}",
                tool_calls=[{"name": "networking_execute", "args": {"operation": "listRouteTables"}, "id": f"call_net_{current_step}", "type": "tool_call"}]
            )
        elif "vpc" in step_lower and "create" in step_lower:
            response = AIMessage(
                content=f"[Network Subgraph] Creating VPC with DNS for step: {active_step_desc}",
                tool_calls=[{"name": "networking_execute", "args": {"operation": "createVpc", "request_body": {"name": "Transit-VPC", "commonDhcpOptions": {"domainNameServers": [{"ipv4": {"value": Config.DNS_SERVER_IP}}]}}}, "id": f"call_net_{current_step}", "type": "tool_call"}]
            )
        else:
            response = AIMessage(
                content=f"[Network Subgraph] Listing subnets for step: {active_step_desc}",
                tool_calls=[{"name": "networking_execute", "args": {"operation": "listSubnets"}, "id": f"call_net_{current_step}", "type": "tool_call"}]
            )

    return {"messages": [response]}


async def network_human_approval_node(state: NutanixAgentState) -> dict[str, Any]:
    """HITL Approval for state-mutating networking operations."""
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return {"approval_granted": True, "pending_tool_call": None}

    tool_call = tool_calls[0]
    approval_request = {
        "action": "AUTHORIZATION_REQUIRED",
        "domain": "NETWORKING",
        "tool_name": tool_call.get("name"),
        "operation": tool_call.get("args", {}).get("operation", "N/A"),
        "arguments": tool_call.get("args", {}),
        "warning": "Mutating network topology (VPC/Subnet/Route). Human authorization required."
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
            content=json.dumps({"ok": False, "error": "Network mutation rejected by user during HITL."}),
            tool_call_id=tool_call.get("id", "call_cancelled")
        )
        return {"messages": [rejection_msg], "approval_granted": False, "pending_tool_call": None}

    return {"approval_granted": True, "pending_tool_call": tool_call}


async def network_tool_execution_node(state: NutanixAgentState) -> dict[str, Any]:
    """Executes network MCP tool call and captures entity UUIDs."""
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return {}

    new_messages, updated_context, error_trace = await _execute_mcp_tool_calls(tool_calls, state.get("cluster_context", {}))
    return {"messages": new_messages, "cluster_context": updated_context, "error_trace": error_trace}


async def network_validator_node(state: NutanixAgentState) -> dict[str, Any]:
    """Validates network execution and outputs clean summary message."""
    messages = state.get("messages", [])
    error_trace = state.get("error_trace")
    last_msg = messages[-1] if messages else None
    last_content = str(last_msg.content) if last_msg else ""

    if error_trace:
        critique = f"Network operation failed: {error_trace.get('error') or error_trace.get('exception') or 'Error'}"
        summary_msg = AIMessage(content=f"[Network Subgraph Error]: {critique}")
    elif '"ok": false' in last_content.lower():
        critique = "Network tool returned failure response."
        summary_msg = AIMessage(content=f"[Network Subgraph Error]: {critique}")
    else:
        critique = "Network operation executed successfully."
        summary_msg = AIMessage(content=f"[Network Subgraph Success]: Configured network state and updated cluster context.")

    return {
        "critique": critique,
        "messages": [summary_msg]
    }


def route_after_network_executor(state: NutanixAgentState) -> Literal["network_approval", "network_tools", "network_validator"]:
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return "network_validator"

    tool_call = tool_calls[0]
    if is_state_mutating_tool_call(tool_call.get("name", ""), tool_call.get("args", {})):
        return "network_approval"
    return "network_tools"


def route_after_network_approval(state: NutanixAgentState) -> Literal["network_tools", "network_validator"]:
    if state.get("approval_granted"):
        return "network_tools"
    return "network_validator"


def build_network_subgraph():
    """Builds and compiles the isolated Network Subgraph."""
    builder = StateGraph(NutanixAgentState)
    builder.add_node("network_executor", network_executor_node)
    builder.add_node("network_approval", network_human_approval_node)
    builder.add_node("network_tools", network_tool_execution_node)
    builder.add_node("network_validator", network_validator_node)

    builder.set_entry_point("network_executor")
    builder.add_conditional_edges("network_executor", route_after_network_executor)
    builder.add_conditional_edges("network_approval", route_after_network_approval)
    builder.add_edge("network_tools", "network_validator")
    builder.add_edge("network_validator", END)

    return builder.compile()


# ============================================================================
# 2. COMPUTE SUBGRAPH (VMs, NICs, Disks, Power)
# ============================================================================

async def compute_executor_node(state: NutanixAgentState) -> dict[str, Any]:
    """Compute Subgraph: Plans and invokes VMM / VM-specific MCP tools."""
    tools = mcp_client_manager.tools
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)
    cluster_context = state.get("cluster_context", {})
    critique = state.get("critique", "")
    active_step_desc = plan[current_step] if current_step < len(plan) else "Manage Nutanix Compute VMs"

    system_prompt = (
        "You are the Nutanix Prism Central Compute & VM Specialist Subgraph.\n"
        f"CURRENT STEP [{current_step + 1}/{len(plan)}]: {active_step_desc}\n"
        f"PREVIOUS CRITIQUE: {critique}\n"
        f"CLUSTER CONTEXT: {json.dumps(cluster_context, indent=2)}\n\n"
        "COMPUTE DIRECTIVES:\n"
        "1. To list Virtual Machines: ALWAYS use 'vmm_execute' with operation 'ahv_listVms' (do not use esxi_listVms).\n"
        "2. To attach Bastion VM NIC: Use 'vmm_execute' with operation 'ahv_createNic' targeting 'Transit-NonERP-01' with IP 20.20.20.14.\n"
        "3. To create Windows VM: Use 'vmm_execute' with operation 'ahv_createVm' (Name: Windows2022-VM, Image: Windows 2022, Container: nkp, 8 vCPUs, 10 GB RAM, 110 GB Boot Disk, Subnet: Transit-NonERP-01, IP: 20.20.20.17, Gateway: 20.20.20.1, Project: default).\n"
        "4. Output the exact tool call required."
    )

    user_request = HumanMessage(content=f"Execute compute action for step: '{active_step_desc}'.")
    llm = get_llm()
    response = None

    if llm and tools:
        try:
            llm_with_tools = llm.bind_tools(tools)
            response = await llm_with_tools.ainvoke([SystemMessage(content=system_prompt), user_request])
        except Exception as exc:
            logger.warning(f"Compute LLM error (using rule-based fallback): {exc}")
            response = None

    if not response:
        step_lower = active_step_desc.lower()
        if "bastion" in step_lower or "nic" in step_lower:
            response = AIMessage(
                content=f"[Compute Subgraph] Attaching NIC to Bastion VM for step: {active_step_desc}",
                tool_calls=[{"name": "vmm_execute", "args": {"operation": "ahv_createNic", "request_body": {"ipAddress": "20.20.20.14"}}, "id": f"call_comp_{current_step}", "type": "tool_call"}]
            )
        elif "windows" in step_lower or ("create" in step_lower and "vm" in step_lower):
            response = AIMessage(
                content=f"[Compute Subgraph] Creating Windows VM for step: {active_step_desc}",
                tool_calls=[{
                    "name": "vmm_execute",
                    "args": {
                        "operation": "ahv_createVm",
                        "request_body": {
                            "name": Config.WINDOWS_VM_NAME,
                            "numSockets": 1,
                            "numCoresPerSocket": Config.WINDOWS_VM_VCPU,
                            "memorySizeBytes": Config.WINDOWS_VM_MEMORY_GB * 1024 * 1024 * 1024,
                            "description": "Windows Server 2022 VM created by Nutanix Compute Subgraph",
                        }
                    },
                    "id": f"call_comp_{current_step}",
                    "type": "tool_call"
                }]
            )
        else:
            response = AIMessage(
                content=f"[Compute Subgraph] Listing AHV VMs for step: {active_step_desc}",
                tool_calls=[{"name": "vmm_execute", "args": {"operation": "ahv_listVms", "_limit": 20}, "id": f"call_comp_{current_step}", "type": "tool_call"}]
            )

    return {"messages": [response]}


async def compute_human_approval_node(state: NutanixAgentState) -> dict[str, Any]:
    """HITL Approval for state-mutating compute operations."""
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return {"approval_granted": True, "pending_tool_call": None}

    tool_call = tool_calls[0]
    approval_request = {
        "action": "AUTHORIZATION_REQUIRED",
        "domain": "COMPUTE",
        "tool_name": tool_call.get("name"),
        "operation": tool_call.get("args", {}).get("operation", "N/A"),
        "arguments": tool_call.get("args", {}),
        "warning": "Mutating compute infrastructure (VM/NIC/Disk). Human authorization required."
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
            content=json.dumps({"ok": False, "error": "Compute mutation rejected by user during HITL."}),
            tool_call_id=tool_call.get("id", "call_cancelled")
        )
        return {"messages": [rejection_msg], "approval_granted": False, "pending_tool_call": None}

    return {"approval_granted": True, "pending_tool_call": tool_call}


async def compute_tool_execution_node(state: NutanixAgentState) -> dict[str, Any]:
    """Executes compute MCP tool call and captures VM UUIDs."""
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return {}

    new_messages, updated_context, error_trace = await _execute_mcp_tool_calls(tool_calls, state.get("cluster_context", {}))
    return {"messages": new_messages, "cluster_context": updated_context, "error_trace": error_trace}


async def compute_validator_node(state: NutanixAgentState) -> dict[str, Any]:
    """Validates compute execution and outputs clean summary message."""
    messages = state.get("messages", [])
    error_trace = state.get("error_trace")
    last_msg = messages[-1] if messages else None
    last_content = str(last_msg.content) if last_msg else ""

    if error_trace:
        critique = f"Compute operation failed: {error_trace.get('error') or error_trace.get('exception') or 'Error'}"
        summary_msg = AIMessage(content=f"[Compute Subgraph Error]: {critique}")
    elif '"ok": false' in last_content.lower():
        critique = "Compute tool returned failure response."
        summary_msg = AIMessage(content=f"[Compute Subgraph Error]: {critique}")
    else:
        critique = "Compute operation executed successfully."
        summary_msg = AIMessage(content=f"[Compute Subgraph Success]: VM operation completed and context updated.")

    return {
        "critique": critique,
        "messages": [summary_msg]
    }


def route_after_compute_executor(state: NutanixAgentState) -> Literal["compute_approval", "compute_tools", "compute_validator"]:
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return "compute_validator"

    tool_call = tool_calls[0]
    if is_state_mutating_tool_call(tool_call.get("name", ""), tool_call.get("args", {})):
        return "compute_approval"
    return "compute_tools"


def route_after_compute_approval(state: NutanixAgentState) -> Literal["compute_tools", "compute_validator"]:
    if state.get("approval_granted"):
        return "compute_tools"
    return "compute_validator"


def build_compute_subgraph():
    """Builds and compiles the isolated Compute Subgraph."""
    builder = StateGraph(NutanixAgentState)
    builder.add_node("compute_executor", compute_executor_node)
    builder.add_node("compute_approval", compute_human_approval_node)
    builder.add_node("compute_tools", compute_tool_execution_node)
    builder.add_node("compute_validator", compute_validator_node)

    builder.set_entry_point("compute_executor")
    builder.add_conditional_edges("compute_executor", route_after_compute_executor)
    builder.add_conditional_edges("compute_approval", route_after_compute_approval)
    builder.add_edge("compute_tools", "compute_validator")
    builder.add_edge("compute_validator", END)

    return builder.compile()


# ============================================================================
# 3. STORAGE & DISCOVERY SUBGRAPH (Storage, Clusters, Schemas)
# ============================================================================

async def storage_executor_node(state: NutanixAgentState) -> dict[str, Any]:
    """Storage Subgraph: Plans and invokes storage / cluster discovery MCP tools."""
    tools = mcp_client_manager.tools
    plan = state.get("plan", [])
    current_step = state.get("current_step", 0)
    cluster_context = state.get("cluster_context", {})
    active_step_desc = plan[current_step] if current_step < len(plan) else "Manage Nutanix Storage & Inventory"

    system_prompt = (
        "You are the Nutanix Prism Central Storage & Discovery Specialist Subgraph.\n"
        f"CURRENT STEP [{current_step + 1}/{len(plan)}]: {active_step_desc}\n"
        f"CLUSTER CONTEXT: {json.dumps(cluster_context, indent=2)}\n\n"
        "STORAGE & INVENTORY DIRECTIVES:\n"
        "1. To list Storage Containers: Use 'storage_execute' or 'clustermgmt_execute' with operation 'listStorageContainers'.\n"
        "2. To list Clusters: Use 'clustermgmt_execute' or 'prism_execute' with operation 'listClusters'.\n"
        "3. Output the exact tool call required."
    )

    user_request = HumanMessage(content=f"Execute storage/discovery action for step: '{active_step_desc}'.")
    llm = get_llm()
    response = None

    if llm and tools:
        try:
            llm_with_tools = llm.bind_tools(tools)
            response = await llm_with_tools.ainvoke([SystemMessage(content=system_prompt), user_request])
        except Exception as exc:
            logger.warning(f"Storage LLM error (using rule-based fallback): {exc}")
            response = None

    if not response:
        response = AIMessage(
            content=f"[Storage Subgraph] Listing Storage Containers for step: {active_step_desc}",
            tool_calls=[{"name": "storage_execute", "args": {"operation": "listStorageContainers"}, "id": f"call_stor_{current_step}", "type": "tool_call"}]
        )

    return {"messages": [response]}


async def storage_tool_execution_node(state: NutanixAgentState) -> dict[str, Any]:
    """Executes storage MCP tool call and updates context."""
    last_msg = state["messages"][-1]
    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return {}

    new_messages, updated_context, error_trace = await _execute_mcp_tool_calls(tool_calls, state.get("cluster_context", {}))
    return {"messages": new_messages, "cluster_context": updated_context, "error_trace": error_trace}


async def storage_validator_node(state: NutanixAgentState) -> dict[str, Any]:
    """Validates storage execution and formats summary."""
    messages = state.get("messages", [])
    error_trace = state.get("error_trace")
    last_msg = messages[-1] if messages else None
    last_content = str(last_msg.content) if last_msg else ""

    if error_trace or '"ok": false' in last_content.lower():
        critique = "Storage/Discovery tool returned an error."
        summary_msg = AIMessage(content=f"[Storage Subgraph Error]: {critique}")
    else:
        critique = "Storage containers and inventory discovered successfully."
        summary_msg = AIMessage(content=f"[Storage Subgraph Success]: Discovery completed.")

    return {
        "critique": critique,
        "messages": [summary_msg]
    }


def build_storage_subgraph():
    """Builds and compiles the isolated Storage Subgraph."""
    builder = StateGraph(NutanixAgentState)
    builder.add_node("storage_executor", storage_executor_node)
    builder.add_node("storage_tools", storage_tool_execution_node)
    builder.add_node("storage_validator", storage_validator_node)

    builder.set_entry_point("storage_executor")
    builder.add_edge("storage_executor", "storage_tools")
    builder.add_edge("storage_tools", "storage_validator")
    builder.add_edge("storage_validator", END)

    return builder.compile()
