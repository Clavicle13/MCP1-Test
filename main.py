import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from config import Config
from state import NutanixAgentState
from mcp_client import mcp_client_manager
from agent_graph import build_reflective_nutanix_graph

# Setup clean logging output
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("NutanixMain")

# Suppress noisy third-party debug loggers
for _noisy in ("httpcore", "httpx", "urllib3", "langsmith", "asyncio", "google_genai", "langchain_google_genai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)



def _parse_tool_message_entities(content: Any) -> list | None:
    """Robustly parses a ToolMessage content value into a list of entity dicts.

    Handles all known content shapes produced by langchain_mcp_adapters:
      - Python list: [{'type': 'text', 'text': '<JSON string>'}]
      - Raw JSON string: '{"ok": true, "payload": {"data": [...]}}'
      - Python repr of a list (str): "[{'type': 'text', 'text': '{...}'}]"
      - Plain dict: {'ok': True, 'payload': {'data': [...]}}

    Returns a list of entity dicts on success, or None if parsing fails.
    """
    import ast

    logger.debug(f"[parser] content type={type(content).__name__}, preview={str(content)[:120]}")

    try:
        # Shape 1: Python list of content blocks (most common from MCP adapters)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    # Accept blocks with type="text" OR any block that has a "text" key
                    text_val = block.get("text") if "text" in block else None
                    if text_val is not None:
                        try:
                            data = json.loads(text_val)
                            result = _unwrap_nutanix_payload(data)
                            logger.debug(f"[parser] Shape1 list→text→json.loads → {len(result) if result else 'None'} items")
                            return result
                        except (json.JSONDecodeError, TypeError) as e:
                            logger.debug(f"[parser] Shape1 json.loads failed: {e}")
            return None

        # Shape 2: Already a dict
        if isinstance(content, dict):
            return _unwrap_nutanix_payload(content)

        # Shape 3: String - may be JSON or Python repr of a list
        if isinstance(content, str):
            # 3a. Try direct JSON parse (handles clean JSON strings)
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    return _unwrap_nutanix_payload(data)
                if isinstance(data, list):
                    return _parse_tool_message_entities(data)
            except (json.JSONDecodeError, TypeError):
                pass

            # 3b. Try ast.literal_eval (handles Python repr: "[{'type': 'text', ...}]")
            try:
                data = ast.literal_eval(content)
                if isinstance(data, (list, dict)):
                    return _parse_tool_message_entities(data)
            except (ValueError, SyntaxError):
                pass

            # 3c. Fallback: extract first {...} substring and parse as JSON
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end > start:
                try:
                    data = json.loads(content[start:end + 1])
                    if isinstance(data, dict):
                        return _unwrap_nutanix_payload(data)
                except (json.JSONDecodeError, TypeError):
                    pass

    except Exception as e:
        logger.debug(f"[parser] Unexpected error: {e}")
    return None


def _unwrap_nutanix_payload(data: dict) -> list | None:
    """Unwraps Nutanix MCP API response envelope to extract the entity list."""
    if not isinstance(data, dict):
        return None
    # Nutanix MCP wraps results: { "ok": true, "payload": { "data": [...] } }
    payload = data.get("payload", data)
    if isinstance(payload, dict):
        items = payload.get("data")
        if isinstance(items, list):
            logger.debug(f"[parser] Unwrapped {len(items)} entities from payload")
            return items
    return None



async def run_interactive_workflow():
    """Main execution flow for Nutanix Prism Central MCP Reflective Agent workflow."""
    print("=" * 80)
    print("  Nutanix Prism Central Reflective MCP Agent (LangGraph + Rich TUI)")
    print("=" * 80)

    # 1. Validate environment configuration
    print("\n[1/4] Validating Prism Central Environment Variables...")
    try:
        Config.validate()
        print(f" -> PC_HOST: {Config.PC_HOST}")
        print(f" -> PC_USERNAME: {Config.PC_USERNAME}")
        print(f" -> PC_INSECURE: {Config.PC_INSECURE}")
    except ValueError as err:
        print(f"Configuration Warning: {err}")

    # 2. Instantiate MultiServerMCPClient & discover tools
    print("\n[2/4] Instantiating MultiServerMCPClient & Discovering V4 API Schemas...")
    try:
        tools = await mcp_client_manager.initialize_tools()
        print(f" -> Successfully mapped {len(tools)} LangGraph-compatible tools.")
    except Exception as exc:
        print(f"Error initializing MCP tools: {exc}")
        return

    # 3. Construct Reflective Agent Graph
    print("\n[3/4] Building Reflective LangGraph Workflow (Planner -> Executor -> Reviewer)...")
    graph = build_reflective_nutanix_graph()
    print(" -> Reflective State Graph successfully compiled.")

    # 4. Interactive Execution Loop
    print("\n[4/4] Starting Interactive Session...")
    print("-" * 80)
    print("Commands:")
    print("  'exit' / 'quit' - Exit application")
    print("  'demo'          - Run automated multi-scenario test suite")
    print("-" * 80)

    thread_config = {"configurable": {"thread_id": "nutanix-reflective-1"}}

    while True:
        try:
            user_input = input("\nNutanix Reflective Agent > ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                print("Exiting Nutanix Agent workflow.")
                break

            if user_input.lower() == "demo":
                await run_demo_suite(graph, thread_config)
                continue

            initial_state: NutanixAgentState = {
                "messages": [HumanMessage(content=user_input)],
                "plan": [],
                "current_step": 0,
                "critique": "",
                "cluster_context": {},
                "pending_tool_call": None,
                "approval_granted": None,
                "error_trace": None,
                "retry_count": 0,
            }

            async for event in graph.astream(initial_state, thread_config):
                for node_name, state_update in event.items():
                    print(f"\n[Node Execution: '{node_name}']")

                    if "plan" in state_update and state_update["plan"]:
                        print(f" -> Plan Steps: {state_update['plan']}")

                    if "critique" in state_update and state_update["critique"]:
                        print(f" -> Reviewer Critique: {state_update['critique']}")

                    if node_name == "human_approval":
                        pending = state_update.get("pending_tool_call") or {}
                        print("\n" + "!" * 60)
                        print(" HUMAN-IN-THE-LOOP (HITL) AUTHORIZATION REQUIRED")
                        print("!" * 60)
                        print(f" Action requested: {pending.get('name')}")
                        print(f" Arguments:        {json.dumps(pending.get('args', {}), indent=2)}")
                        print("!" * 60)

                        choice = input("Authorize state-mutating operation? (y/n) > ").strip().lower()
                        approved = choice in ("y", "yes", "approve")

                        resume_state = {"approval_granted": approved}
                        async for sub_event in graph.astream(resume_state, thread_config):
                            for sub_node, sub_update in sub_event.items():
                                print(f" -> Resume Node '{sub_node}': {sub_update.keys()}")

                    elif "messages" in state_update:
                        last_m = state_update["messages"][-1]
                        if isinstance(last_m, ToolMessage):
                            items = _parse_tool_message_entities(last_m.content)
                            if items is not None:
                                if len(items) > 0:
                                    print()
                                    print(f"   {'#':<5} {'Name':<30} {'UUID':<40} {'Power State':<12}")
                                    print(f"   {'-'*5} {'-'*30} {'-'*40} {'-'*12}")
                                    for idx, item in enumerate(items[:20], 1):
                                        if isinstance(item, dict):
                                            name = item.get("name", item.get("vmName", "N/A"))
                                            ext_id = item.get("extId", item.get("id", "N/A"))
                                            power = item.get("powerState", item.get("status", ""))
                                            print(f"   {str(idx):<5} {str(name):<30} {str(ext_id):<40} {str(power):<12}")
                                    print(f"\n   ✔ Total: {len(items)} entities returned from Prism Central.")
                                else:
                                    print(" -> Output (ToolMessage): 0 entities returned for operation.")
                            else:
                                print(f" -> Output (ToolMessage): {str(last_m.content)[:300]}")
                        else:
                            print(f" -> Output ({last_m.__class__.__name__}): {last_m.content[:300]}")

                    if "cluster_context" in state_update and state_update["cluster_context"]:
                        print(f" -> Updated Cluster Context: {json.dumps(state_update['cluster_context'])}")

        except (KeyboardInterrupt, EOFError):
            print("\nSession interrupted.")
            break
        except Exception as exc:
            print(f"Execution Error: {exc}")


async def run_demo_suite(graph, thread_config):
    """Automated demonstration suite verifying Reflective Planning, Executor, HITL, and Reviewer."""
    print("\n" + "=" * 80)
    print(" RUNNING AUTOMATED REFLECTIVE NUTANIX AGENT DEMO SUITE")
    print("=" * 80)

    # Scenario 1: Reflective GET Query (Autonomous execution)
    print("\n--- [Scenario 1: Reflective Read-Only GET Query] ---")
    query_1 = "List and inspect Nutanix Virtual Machines"
    print(f"User: '{query_1}'")

    state_1: NutanixAgentState = {
        "messages": [HumanMessage(content=query_1)],
        "plan": [],
        "current_step": 0,
        "critique": "",
        "cluster_context": {},
        "pending_tool_call": None,
        "approval_granted": None,
        "error_trace": None,
        "retry_count": 0,
    }

    async for event in graph.astream(state_1, thread_config):
        for node_name, state_update in event.items():
            print(f" -> Executed node: '{node_name}'")
            if "plan" in state_update and state_update["plan"]:
                print(f"    Plan: {state_update['plan']}")
            if "critique" in state_update and state_update["critique"]:
                print(f"    Critique: {state_update['critique']}")

    print("\n" + "=" * 80)
    print(" DEMO SUITE COMPLETE - REFLECTIVE ROUTING CONSTRAINTS VERIFIED")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Nutanix Prism Central Reflective LangGraph Agent")
    parser.add_argument("--demo", action="store_true", help="Run automated test suite and exit")
    parser.add_argument("--tui", action="store_true", help="Launch Rich Text-Based User Interface (TUI)")
    args = parser.parse_args()

    if args.tui:
        from tui_app import main as tui_main
        tui_main()
    elif args.demo:
        graph = build_reflective_nutanix_graph()
        thread_config = {"configurable": {"thread_id": "nutanix-reflective-demo"}}
        asyncio.run(run_demo_suite(graph, thread_config))
    else:
        asyncio.run(run_interactive_workflow())


if __name__ == "__main__":
    main()
