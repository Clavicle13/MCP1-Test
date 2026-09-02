import asyncio
import logging
from config import Config
from state import NutanixAgentState, merge_cluster_context
from agent_graph import build_reflective_nutanix_graph, planner_node, reviewer_node, route_to_domain_subgraph
from subgraphs import build_network_subgraph, build_compute_subgraph, build_storage_subgraph
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


def test_subgraphs_compilation():
    print("\nTesting Individual Subgraphs Compilation...")
    net_sg = build_network_subgraph()
    assert net_sg is not None
    print(" -> Network Subgraph compiled successfully!")

    comp_sg = build_compute_subgraph()
    assert comp_sg is not None
    print(" -> Compute Subgraph compiled successfully!")

    stor_sg = build_storage_subgraph()
    assert stor_sg is not None
    print(" -> Storage Subgraph compiled successfully!")


def test_graph_compilation():
    print("\nTesting Parent Reflective Agent Graph Compilation...")
    graph = build_reflective_nutanix_graph()
    assert graph is not None
    print(" -> Parent State Graph (Planner -> Subgraphs -> Reviewer) compiled cleanly!")


def test_subgraph_routing():
    print("\nTesting Domain Subgraph Routing Logic...")
    net_state: NutanixAgentState = {"messages": [], "plan": ["1. Create VPC and Subnets"], "current_step": 0, "critique": "", "cluster_context": {}, "pending_tool_call": None, "approval_granted": None, "error_trace": None, "retry_count": 0}
    assert route_to_domain_subgraph(net_state) == "network_subgraph"
    print(" -> Routed 'Create VPC' -> network_subgraph")

    comp_state: NutanixAgentState = {"messages": [], "plan": ["1. Create Windows VM"], "current_step": 0, "critique": "", "cluster_context": {}, "pending_tool_call": None, "approval_granted": None, "error_trace": None, "retry_count": 0}
    assert route_to_domain_subgraph(comp_state) == "compute_subgraph"
    print(" -> Routed 'Create Windows VM' -> compute_subgraph")

    stor_state: NutanixAgentState = {"messages": [], "plan": ["1. List Storage Containers"], "current_step": 0, "critique": "", "cluster_context": {}, "pending_tool_call": None, "approval_granted": None, "error_trace": None, "retry_count": 0}
    assert route_to_domain_subgraph(stor_state) == "storage_subgraph"
    print(" -> Routed 'List Storage Containers' -> storage_subgraph")

    fip_state: NutanixAgentState = {"messages": [], "plan": ["1. Assign Floating IPs from External Subnet to Linux Bastion VM and Windows VM"], "current_step": 0, "critique": "", "cluster_context": {}, "pending_tool_call": None, "approval_granted": None, "error_trace": None, "retry_count": 0}
    assert route_to_domain_subgraph(fip_state) == "network_subgraph"
    print(" -> Routed 'Assign Floating IPs' -> network_subgraph")


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
    test_subgraphs_compilation()
    test_graph_compilation()
    test_subgraph_routing()
    await test_planner_and_reviewer_nodes()
    test_tui_layout_generation()
    print("\n" + "=" * 60)
    print(" ALL REFLECTIVE WORKFLOW, SUBGRAPH & TUI TESTS PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
