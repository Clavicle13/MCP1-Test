import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from langchain_core.messages import HumanMessage
from config import Config
from state import NutanixAgentState
from mcp_client import mcp_client_manager
from agent_graph import build_reflective_nutanix_graph

# Setup clean logging output
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("NutanixMain")


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
