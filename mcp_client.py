import os
import sys
from pathlib import Path
from typing import Any
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from config import Config


# Discovery tools provided by nutanix-mcp server (always read-only)
DISCOVERY_TOOLS = {
    "listOperations",
    "getOperationSchema",
    "getCodeSample",
    "getOperationPermissions",
}

# Operation prefixes / keywords that indicate read-only calls
READ_ONLY_PREFIXES = (
    "list",
    "get",
    "fetch",
    "search",
    "read",
    "describe",
    "show",
    "export",
    "download",
    "check",
    "inspect",
)

# Operation keywords that explicitly indicate state-mutating calls
MUTATION_KEYWORDS = (
    "create",
    "update",
    "delete",
    "post",
    "put",
    "patch",
    "remove",
    "add",
    "attach",
    "detach",
    "power",
    "reboot",
    "shutdown",
    "migrate",
    "clone",
    "restore",
    "mount",
    "unmount",
    "configure",
    "trigger",
    "enable",
    "disable",
)


def is_state_mutating_tool_call(tool_name: str, tool_args: dict[str, Any]) -> bool:
    """Determines whether a requested MCP tool call represents a state-mutating API operation.
    
    Args:
        tool_name: Name of the tool (e.g. 'vmm_execute', 'prism_execute', 'listOperations').
        tool_args: Dictionary of arguments passed to the tool call.
        
    Returns:
        bool: True if the operation modifies state (POST/PUT/PATCH/DELETE), False if read-only (GET).
    """
    # Standard discovery tools are always read-only
    if tool_name in DISCOVERY_TOOLS:
        return False

    # For namespace executor tools (<namespace>_execute)
    raw_operation = tool_args.get("operation", "").strip().lower()
    request_body = tool_args.get("request_body")

    # If a request payload is supplied, it is a write operation (POST/PUT/PATCH)
    if request_body and isinstance(request_body, dict) and len(request_body) > 0:
        return True

    # Strip common Nutanix hypervisor/version prefixes (ahv_, esxi_, v4_)
    op_clean = raw_operation
    for prefix in ("ahv_", "esxi_", "v4_", "api_"):
        if op_clean.startswith(prefix):
            op_clean = op_clean[len(prefix):]

    # Explicit mutation keyword check
    if any(keyword in op_clean for keyword in MUTATION_KEYWORDS):
        return True

    # Read-only keyword check
    if any(keyword in op_clean for keyword in READ_ONLY_PREFIXES) or any(op_clean.startswith(prefix) for prefix in READ_ONLY_PREFIXES):
        return False

    # Default to treating unknown/unrecognized operations as state-mutating for safety
    return True


class NutanixMCPClientManager:
    """Manager for standard Stdio MultiServerMCPClient connected to local Nutanix MCP server process."""

    def __init__(self) -> None:
        self.client: MultiServerMCPClient | None = None
        self._tools: list[BaseTool] = []

    def get_server_connection_config(self) -> dict[str, Any]:
        """Builds Stdio connection configuration for MultiServerMCPClient."""
        venv_bin = Path(sys.executable).parent / "nutanix-mcp.exe"
        if venv_bin.exists():
            cmd = str(venv_bin)
            args = ["serve-stdio"]
        else:
            cmd = sys.executable
            args = ["-m", "ntnx_mcp", "serve_stdio"]

        env = Config.get_server_env()

        return {
            "nutanix": {
                "command": cmd,
                "args": args,
                "env": env,
                "transport": "stdio",
            }
        }

    async def initialize_tools(self) -> list[BaseTool]:
        """Instantiates MultiServerMCPClient and dynamically fetches LangGraph-compatible tools."""
        connection_config = self.get_server_connection_config()
        self.client = MultiServerMCPClient(connections=connection_config)
        self._tools = await self.client.get_tools()
        return self._tools

    @property
    def tools(self) -> list[BaseTool]:
        return self._tools


# Global singleton instance for easy import across nodes
mcp_client_manager = NutanixMCPClientManager()
