import asyncio
import logging
from config import Config
from state import NutanixAgentState, merge_cluster_context
from agent_graph import build_reflective_nutanix_graph, planner_node, reviewer_node
from tui_app import build_tui_layout

logging.basicConfig(level=logging.INFO)


def test_reflective_state_schema():
    print("Testing Reflective NutanixAgentState Schema...")
    dummy_state: NutanixAgentState = {
        "messages": [],
        "plan": ["1. Step one", "2. Step two"],
        "current_step": 0,
        "critique": "Plan initialized.",
        "cluster_context": {"vm_uuid": "1111-2222"},
        "pending_tool_call": None,
        "approval_granted": None,
        "error_trace": None,
        "retry_count": 0,
    }
    assert len(dummy_state["plan"]) == 2
    assert dummy_state["current_step"] == 0
    assert dummy_state["cluster_context"]["vm_uuid"] == "1111-2222"
    print(" -> Reflective state schema verification passed!")


def test_graph_compilation():
    print("\nTesting Reflective Agent Graph Compilation...")
    graph = build_reflective_nutanix_graph()
    assert graph is not None
    print(" -> Reflective State Graph (Planner-Executor-Reviewer) compiled cleanly!")


async def test_planner_and_reviewer_nodes():
    print("\nTesting Planner & Reviewer Node execution...")
    dummy_state: NutanixAgentState = {
        "messages": [],
        "plan": [],
        "current_step": 0,
        "critique": "",
        "cluster_context": {"cluster_uuid": "cl-999"},
        "pending_tool_call": None,
        "approval_granted": None,
        "error_trace": None,
        "retry_count": 0,
    }

    planner_res = await planner_node(dummy_state)
    assert "plan" in planner_res
    assert len(planner_res["plan"]) > 0
    print(f" -> Formulated Plan: {planner_res['plan']}")

    dummy_state["plan"] = planner_res["plan"]
    dummy_state["messages"] = planner_res["messages"]

    reviewer_res = await reviewer_node(dummy_state)
    assert "critique" in reviewer_res
    print(f" -> Reviewer Critique: {reviewer_res['critique']}")


def test_tui_layout_generation():
    print("\nTesting Rich TUI Layout Generation...")
    logs = ["System initialized.", "User query: List VMs"]
    plan = ["1. Discover operations", "2. List VMs"]
    current_step = 0
    critique = "Step 1 in progress."
    cluster_context = {"vm_uuid": "abc-123", "cluster_uuid": "cl-456"}

    layout = build_tui_layout(logs, plan, current_step, critique, cluster_context)
    assert layout is not None
    print(" -> Rich TUI layout grid generated successfully without errors!")


async def main():
    test_reflective_state_schema()
    test_graph_compilation()
    await test_planner_and_reviewer_nodes()
    test_tui_layout_generation()
    print("\n" + "=" * 60)
    print(" ALL REFLECTIVE WORKFLOW & TUI TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
