import json
import logging
from typing import Any
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from config import Config
from state import NutanixAgentState
from mcp_client import is_state_mutating_tool_call, mcp_client_manager

logger = logging.getLogger("NutanixGraphNodes")


def get_llm():
    """Helper to instantiate configured LLM model (Google Gemini / Anthropic / OpenAI / Fallback)."""
    provider = Config.MODEL_PROVIDER
    model_name = Config.MODEL_NAME

    if (provider in ("google", "gemini") or Config.GOOGLE_API_KEY) and Config.GOOGLE_API_KEY:
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model_name, google_api_key=Config.GOOGLE_API_KEY, temperature=0)
    elif provider == "anthropic" and Config.ANTHROPIC_API_KEY:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model_name=model_name, api_key=Config.ANTHROPIC_API_KEY, temperature=0)
    elif provider == "openai" and Config.OPENAI_API_KEY:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model_name=model_name, api_key=Config.OPENAI_API_KEY, temperature=0)
    else:
        # If no explicit provider key set, attempt generic init_chat_model
        try:
            from langchain.chat_models import init_chat_model
            return init_chat_model(model_name, model_provider=provider, temperature=0)
        except Exception:
            logger.warning("No LLM API key detected for Google/Anthropic/OpenAI. Using fallback agent model binding.")
            return None


async def agent_node(state: NutanixAgentState) -> dict[str, Any]:
    """Agent node: Formulates prompt with cluster_context awareness and invokes LLM with mapped tools."""
    tools = mcp_client_manager.tools
    messages = list(state.get("messages", []))
    cluster_context = state.get("cluster_context", {})

    # Construct dynamic system message incorporating active cluster context
    system_prompt = (
        "You are an AI Infrastructure Engineer assistant managing Nutanix Prism Central via V4 APIs.\n"
        "You have access to Nutanix MCP tools to inspect schemas, list resources, and execute operations.\n\n"
        f"CURRENT CLUSTER CONTEXT (Entity UUIDs & State):\n{json.dumps(cluster_context, indent=2)}\n\n"
        "STRICT GUIDELINES:\n"
        "1. Always reuse entity UUIDs stored in CLUSTER CONTEXT when referencing VMs, clusters, or tasks.\n"
        "2. For read-only queries (listing VMs, checking specs, listing clusters), use GET operations directly.\n"
        "3. When creating, modifying, or deleting resources, format tool parameters accurately.\n"
        "4. VPC, Routing & VM Provisioning Directives:\n"
        "   - Always configure DNS server entry in `commonDhcpOptions.domainNameServers` during VPC creation.\n"
        "   - Always configure default static route (0.0.0.0/0) via `createRouteForRouteTable` targeting external network attachment (for Transit VPC) or Transit ERP subnet (for Spoke VPC).\n"
        "   - Always attach the Linux Bastion VM (`LinuxTools`) to the `Transit-NonERP-01` subnet with static IP `20.20.20.14` via `vmm_execute` `ahv_createNic`.\n"
        "   - After Linux VM attachment/creation, create a Windows VM with the following parameters via `vmm_execute` `ahv_createVm` (or `createVm`):\n"
        "     * Name: `Windows2022-VM`\n"
        "     * Image: `Windows 2022`\n"
        "     * Storage Container: `nkp`\n"
        "     * vCPU: 8\n"
        "     * Memory: 10 GByte (10737418240 bytes)\n"
        "     * Boot Disk: 110 GByte (118111600640 bytes) cloned from Image `Windows 2022` on container `nkp`\n"
        "     * Network: Connected to Transit VPC NON ERP subnet (`Transit-NonERP-01`)\n"
        "     * IP Address: 20.20.20.17 (Gateway: 20.20.20.1)\n"
        "     * Project: `default`\n"
        "5. If a tool call fails, analyze the error output carefully and fix missing or invalid parameters."
    )

    # Ensure system prompt is present at head of message list
    if not messages or not isinstance(messages[0], SystemMessage):
        messages.insert(0, SystemMessage(content=system_prompt))
    else:
        messages[0] = SystemMessage(content=system_prompt)

    llm = get_llm()
    response = None
    if llm and tools:
        try:
            llm_with_tools = llm.bind_tools(tools)
            response = await llm_with_tools.ainvoke(messages)
        except Exception as exc:
            logger.warning(f"Agent LLM API error (falling back to simulation mode): {exc}")
            response = None

    if not response:
        # Fallback simulation response if no LLM API key present or API key error
        last_user_msg = [m.content for m in messages if isinstance(m, HumanMessage)][-1] if messages else ""
        if "vm" in last_user_msg.lower() or "list" in last_user_msg.lower():
            response = AIMessage(
                content="Listing VMs from Prism Central.",
                tool_calls=[{
                    "name": "vmm_execute",
                    "args": {"operation": "listVms", "_limit": 5},
                    "id": "call_mock_1",
                    "type": "tool_call"
                }]
            )
        else:
            response = AIMessage(content="Connected to Nutanix Prism Central MCP Server.")

    return {"messages": [response]}


async def human_approval_node(state: NutanixAgentState) -> dict[str, Any]:
    """Human-in-the-Loop (HITL) authorization node for state-mutating tool calls.
    
    Pauses graph execution using `interrupt` and waits for explicit user confirmation before proceeding.
    """
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

    # Trigger LangGraph interrupt for human-in-the-loop pause
    human_response = interrupt(approval_request)

    # Evaluate approval choice
    if isinstance(human_response, dict):
        authorized = human_response.get("approved", False)
    elif isinstance(human_response, str):
        authorized = human_response.strip().lower() in ("yes", "y", "true", "approve", "authorized")
    else:
        authorized = bool(human_response)

    if not authorized:
        rejection_msg = ToolMessage(
            content=json.dumps({
                "ok": False,
                "error": "Operation rejected by user during Human-in-the-Loop authorization."
            }),
            tool_call_id=tool_call.get("id", "call_cancelled")
        )
        return {
            "messages": [rejection_msg],
            "approval_granted": False,
            "pending_tool_call": None,
        }

    return {"approval_granted": True, "pending_tool_call": tool_call}


async def tool_execution_node(state: NutanixAgentState) -> dict[str, Any]:
    """Tool execution node: Runs MCP tool calls and updates cluster_context with discovered entity UUIDs."""
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

        # Explicitly initialize call_tool_result variable in loop scope
        call_tool_result: Any = None

        tool_obj = tools_by_name.get(tool_name)
        if not tool_obj:
            err_msg = f"Tool '{tool_name}' not registered in Nutanix MCP server."
            new_messages.append(ToolMessage(content=json.dumps({"ok": False, "error": err_msg}), tool_call_id=tool_id))
            error_trace = {"tool_name": tool_name, "error": err_msg}
            continue

        try:
            # Execute tool
            call_tool_result = await tool_obj.ainvoke(tool_args)
            result_str = str(call_tool_result)

            # Check for MCP server returned execution error payload
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
                    # Extract common Prism Central UUID fields
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
            logger.error(f"Exception executing tool {tool_name}: {exc}")
            err_payload = {"ok": False, "error": f"Tool execution exception: {str(exc)}"}
            new_messages.append(ToolMessage(content=json.dumps(err_payload), tool_call_id=tool_id))
            error_trace = {
                "tool_name": tool_name,
                "operation": tool_args.get("operation"),
                "exception": str(exc),
            }

    return {
        "messages": new_messages,
        "cluster_context": updated_context,
        "error_trace": error_trace,
    }


async def recovery_analysis_node(state: NutanixAgentState) -> dict[str, Any]:
    """Recovery analysis node: Synthesizes failure trace (e.g. invalid schema payload, timeout, auth error).
    
    Generates diagnostic suggestions and updates graph state for recovery or graceful reporting.
    """
    error_trace = state.get("error_trace") or {}
    tool_name = error_trace.get("tool_name", "unknown_tool")
    operation = error_trace.get("operation", "unknown_operation")
    details = error_trace.get("raw_output") or error_trace.get("exception") or error_trace.get("error", "No detail")
    current_retries = state.get("retry_count", 0) + 1

    if current_retries >= 2:
        final_msg = AIMessage(
            content=(
                f"Execution failed after recovery attempts for operation '{operation}' on tool '{tool_name}'.\n"
                f"Root Cause: {details}\n"
                "Please verify network connectivity to Prism Central (PC_HOST) and confirm authentication credentials."
            )
        )
        return {
            "messages": [final_msg],
            "error_trace": None,
            "retry_count": current_retries,
        }

    diagnosis = (
        f"[RECOVERY ANALYSIS - Attempt {current_retries}] Tool '{tool_name}' failed during operation '{operation}'.\n"
        f"Error Details: {details}\n"
        "Diagnostic Recommendation: Inspect operation schema via getOperationSchema tool or adjust OData filter syntax."
    )

    recovery_msg = SystemMessage(content=diagnosis)

    return {
        "messages": [recovery_msg],
        "error_trace": None,  # Reset error trace after recovery analysis
        "retry_count": current_retries,
    }
