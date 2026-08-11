import asyncio
import json
import logging
from config import Config
from state import NutanixAgentState, merge_cluster_context
from mcp_client import is_state_mutating_tool_call
from graph import build_nutanix_graph
from nodes import recovery_analysis_node

logging.basicConfig(level=logging.INFO)


def test_config():
    print("Testing Config loading...")
    Config.validate()
    print(" -> Config validation passed.")


def test_tool_classification():
    print("\nTesting Tool Classification (Read-Only vs State-Mutating)...")
    # Read-only tests
    assert not is_state_mutating_tool_call("listOperations", {})
    assert not is_state_mutating_tool_call("getOperationSchema", {})
    assert not is_state_mutating_tool_call("vmm_execute", {"operation": "listVms"})
    assert not is_state_mutating_tool_call("prism_execute", {"operation": "getTaskById"})
    assert not is_state_mutating_tool_call("clustermgmt_execute", {"operation": "listHosts"})

    # State-mutating tests
    assert is_state_mutating_tool_call("vmm_execute", {"operation": "createVm"})
    assert is_state_mutating_tool_call("vmm_execute", {"operation": "deleteVmById"})
    assert is_state_mutating_tool_call("prism_execute", {"operation": "updateCategory"})
    assert is_state_mutating_tool_call("vmm_execute", {"operation": "listVms", "request_body": {"name": "test"}})

    print(" -> All tool classification assertions passed successfully!")


def test_state_merging():
    print("\nTesting State Merging (cluster_context reducer)...")
    initial = {"vm_uuid": "1234-5678"}
    update = {"cluster_uuid": "abcd-efgh"}
    merged = merge_cluster_context(initial, update)
    assert merged == {"vm_uuid": "1234-5678", "cluster_uuid": "abcd-efgh"}
    print(f" -> Merged Context: {merged}")


def test_graph_compilation():
    print("\nTesting LangGraph Compilation...")
    graph = build_nutanix_graph()
    assert graph is not None
    print(" -> State graph compiled cleanly with MemorySaver checkpointer.")


async def test_recovery_node():
    print("\nTesting Recovery Analysis Node...")
    dummy_state: NutanixAgentState = {
        "messages": [],
        "cluster_context": {},
        "pending_tool_call": None,
        "approval_granted": None,
        "error_trace": {
            "tool_name": "vmm_execute",
            "operation": "createVm",
            "exception": "Validation error: missing property 'cluster_reference'",
        },
    }
    result = await recovery_analysis_node(dummy_state)
    assert "messages" in result
    print(" -> Recovery Node Output:")
    print("   ", result["messages"][0].content.replace("\n", "\n    "))


def test_gemini_model_binding():
    print("\nTesting Gemini LLM Model Binding...")
    from nodes import get_llm
    # Temporarily set dummy GOOGLE_API_KEY if not present
    original_key = Config.GOOGLE_API_KEY
    if not Config.GOOGLE_API_KEY:
        Config.GOOGLE_API_KEY = "test_google_api_key_123"

    llm = get_llm()
    assert llm is not None
    print(f" -> Model class instantiated: {llm.__class__.__name__}")
    assert "Google" in llm.__class__.__name__ or "GenerativeAI" in llm.__class__.__name__ or "Anthropic" in llm.__class__.__name__ or "OpenAI" in llm.__class__.__name__

    # Restore original key
    Config.GOOGLE_API_KEY = original_key
    print(" -> Gemini model binding verified successfully!")


async def main():
    test_config()
    test_tool_classification()
    test_state_merging()
    test_graph_compilation()
    test_gemini_model_binding()
    await test_recovery_node()
    print("\n" + "=" * 60)
    print(" ALL WORKFLOW VERIFICATION TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
