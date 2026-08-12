import asyncio
import json
import logging
import os
import sys
from typing import Any

# Suppress verbose schema conversion logs in TUI
logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from langchain_core.messages import HumanMessage, ToolMessage

from config import Config
from state import NutanixAgentState
from mcp_client import mcp_client_manager
from agent_graph import build_reflective_nutanix_graph

console = Console()


def show_entity_popup(title: str, items: list[dict], columns: list[tuple[str, str, str]]) -> None:
    """Displays a full-screen popup overlay with the entity table, then waits for Enter.

    Args:
        title:   Popup panel title (e.g. 'Virtual Machines').
        items:   List of entity dicts returned from Prism Central.
        columns: List of (display_name, dict_key, rich_style) tuples defining the table columns.
    """
    console.clear()

    # ── Header banner ────────────────────────────────────────────────────────
    console.print(Rule(f"[bold cyan]  Nutanix Prism Central  ─  {title}  [/bold cyan]", style="cyan"))
    console.print()

    # ── Build the entity table ────────────────────────────────────────────────
    tbl = Table(
        title=f"[bold green]{title}  ({len(items)} total)[/bold green]",
        box=box.DOUBLE_EDGE,
        border_style="cyan",
        header_style="bold white on dark_blue",
        expand=True,
        show_lines=True,
    )
    tbl.add_column("#", style="dim white", width=4, justify="right")
    for (col_name, _key, col_style) in columns:
        tbl.add_column(col_name, style=col_style)

    for idx, item in enumerate(items, 1):
        row_values = [str(idx)]
        for (_col_name, key, _style) in columns:
            # Support dot-path lookup (e.g. "address.value") and fallback keys
            keys = key.split("|")
            val = "N/A"
            for k in keys:
                parts = k.strip().split(".")
                v = item
                for p in parts:
                    if isinstance(v, dict):
                        v = v.get(p)
                    else:
                        v = None
                        break
                if v is not None and str(v).strip():
                    val = str(v)
                    break
            row_values.append(val)
        tbl.add_row(*row_values)

    # ── Wrap table in a full-width panel ─────────────────────────────────────
    popup = Panel(
        Align.center(tbl),
        title=f"[bold white on blue]  ╔══  {title.upper()}  ══╗  [/bold white on blue]",
        subtitle="[dim]Press [bold cyan]Enter[/bold cyan] to return to the dashboard[/dim]",
        border_style="bright_cyan",
        padding=(1, 2),
        expand=True,
    )
    console.print(popup)
    console.print()
    input()


def _build_popup_columns(query: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Returns (popup_title, columns) appropriate for the entity type inferred from the query."""
    q = query.lower()
    if "vm" in q or "virtual" in q or "ahv" in q:
        return (
            "Virtual Machines",
            [
                ("VM Name",     "name|vmName",       "bold white"),
                ("UUID (ExtID)","extId|id",          "bright_cyan"),
                ("Power State", "powerState|status", "bold green"),
            ],
        )
    elif "storage" in q or "container" in q:
        return (
            "Storage Containers",
            [
                ("Container Name",  "name",          "bold white"),
                ("UUID (ExtID)",    "extId|id",      "bright_cyan"),
                ("Container Type",  "containerType", "yellow"),
            ],
        )
    elif "subnet" in q or "network" in q:
        return (
            "Network Subnets",
            [
                ("Subnet Name",  "name",              "bold white"),
                ("UUID (ExtID)", "extId|id",          "bright_cyan"),
                ("VLAN / Type",  "vlanId|subnetType", "yellow"),
            ],
        )
    else:
        return (
            "Nutanix Entities",
            [
                ("Name / ID", "name|extId|id",  "bold white"),
                ("UUID",      "extId|id",        "bright_cyan"),
                ("Details",   "status|type",     "yellow"),
            ],
        )


def create_header() -> Panel:
    """Builds the top header panel showing Prism Central connection & LLM state."""
    table = Table.grid(expand=True)
    table.add_column(justify="left")
    table.add_column(justify="right")

    host_info = f"[bold cyan]Prism Central Host:[/bold cyan] {Config.PC_HOST}:{Config.PC_PORT} | [bold green]User:[/bold green] {Config.PC_USERNAME}"
    model_info = f"[bold yellow]LLM Provider:[/bold yellow] {Config.MODEL_PROVIDER} ({Config.MODEL_NAME}) | [bold magenta]MCP Tools:[/bold magenta] {len(mcp_client_manager.tools)} Mapped"

    table.add_row(
        Text("Nutanix Prism Central Reflective Agent Dashboard", style="bold white on blue"),
        Text(f"{Config.PC_HOST} | {Config.MODEL_NAME}", style="dim white")
    )
    table.add_row(host_info, model_info)

    return Panel(table, style="blue", title="[bold]System Status[/bold]")


def create_menu_panel() -> Panel:
    """Builds the navigation menu panel."""
    menu_table = Table(show_header=False, box=None, padding=(0, 1))
    menu_table.add_column("Key", style="bold cyan", width=5)
    menu_table.add_column("Category Action", style="bold white")

    menu_table.add_row("[1]", "Virtual Machines (VMs)")
    menu_table.add_row("[2]", "Storage Containers")
    menu_table.add_row("[3]", "Network Subnets")
    menu_table.add_row("[4]", "Custom Query")
    menu_table.add_row("[5]", "Exit Application")

    return Panel(menu_table, title="[bold green]Navigation Menu[/bold green]", style="green")


def create_context_panel(cluster_context: dict[str, Any]) -> Panel:
    """Builds the right side panel displaying discovered entity UUIDs."""
    ctx_table = Table(show_header=True, header_style="bold yellow", box=None)
    ctx_table.add_column("Entity Key", style="cyan")
    ctx_table.add_column("UUID / Value", style="white")

    if not cluster_context:
        ctx_table.add_row("[dim]None[/dim]", "[dim]No entity UUIDs stored yet[/dim]")
    else:
        for k, v in cluster_context.items():
            ctx_table.add_row(str(k), str(v)[:24] + "..." if len(str(v)) > 24 else str(v))

    return Panel(ctx_table, title="[bold yellow]Cluster Context (Entity UUIDs)[/bold yellow]", style="yellow")


def extract_json_object(raw_content: Any) -> dict[str, Any] | None:
    """Extracts JSON object dictionary from any raw ToolMessage content structure."""
    if isinstance(raw_content, dict):
        return raw_content

    if isinstance(raw_content, list) and len(raw_content) > 0:
        first_item = raw_content[0]
        if isinstance(first_item, dict) and "text" in first_item:
            try:
                return json.loads(first_item["text"])
            except Exception:
                pass
        return extract_json_object(first_item)

    raw_str = str(raw_content)

    # Direct json load attempt
    try:
        parsed = json.loads(raw_str)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and len(parsed) > 0:
            return extract_json_object(parsed)
    except Exception:
        pass

    # Substring bracket search fallback
    first_brace = raw_str.find("{")
    last_brace = raw_str.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        snippet = raw_str[first_brace:last_brace + 1]
        snippet = snippet.replace("\\n", "\n").replace('\\"', '"')
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return None


def format_entity_table(query_type: str, raw_content: Any) -> Table | None:
    """Parses JSON tool response content and formats entities into a styled Rich Table."""
    try:
        data = extract_json_object(raw_content)
        if not isinstance(data, dict):
            return None

        # Unwrap Nutanix MCP payload wrapper (payload -> data)
        payload = data.get("payload") if "payload" in data and isinstance(data["payload"], dict) else data
        items = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload

        if not isinstance(items, list):
            if isinstance(items, dict):
                items = [items]
            else:
                return None

        if not items:
            table = Table(title="[yellow]Nutanix Prism Central Response[/yellow]", box=box.ROUNDED)
            table.add_column("Status", style="bold cyan")
            table.add_row("0 entities returned for query.")
            return table

        query_lower = query_type.lower()
        if "vm" in query_lower or "virtual" in query_lower or "ahv" in query_lower or "vmm" in query_lower or "esxi" in query_lower:
            table = Table(title=f"[bold green]Discovered Nutanix Virtual Machines ({len(items)} found)[/bold green]", box=box.ROUNDED)
            table.add_column("VM Name", style="bold white")
            table.add_column("ExtID (UUID)", style="cyan")
            table.add_column("Power State / Status", style="yellow")
            for vm in items[:12]:
                name = vm.get("name", vm.get("vmName", "N/A"))
                ext_id = vm.get("extId", vm.get("id", "N/A"))
                power = vm.get("powerState", vm.get("status", "UNKNOWN"))
                table.add_row(str(name), str(ext_id), str(power))
            return table

        elif "storage" in query_lower or "container" in query_lower:
            table = Table(title=f"[bold green]Discovered Storage Containers ({len(items)} found)[/bold green]", box=box.ROUNDED)
            table.add_column("Container Name", style="bold white")
            table.add_column("ExtID (UUID)", style="cyan")
            table.add_column("Container Type", style="yellow")
            for sc in items[:12]:
                name = sc.get("name", "N/A")
                ext_id = sc.get("extId", "N/A")
                c_type = sc.get("containerType", "Standard")
                table.add_row(str(name), str(ext_id), str(c_type))
            return table

        elif "subnet" in query_lower or "network" in query_lower:
            table = Table(title=f"[bold green]Discovered Network Subnets ({len(items)} found)[/bold green]", box=box.ROUNDED)
            table.add_column("Subnet Name", style="bold white")
            table.add_column("ExtID (UUID)", style="cyan")
            table.add_column("Type / VLAN", style="yellow")
            for sub in items[:12]:
                name = sub.get("name", "N/A")
                ext_id = sub.get("extId", "N/A")
                vlan = sub.get("vlanId", sub.get("subnetType", "N/A"))
                table.add_row(str(name), str(ext_id), str(vlan))
            return table

        # Fallback for any other entity array
        table = Table(title=f"[bold green]Discovered Nutanix Entities ({len(items)} found)[/bold green]", box=box.ROUNDED)
        table.add_column("Index", style="dim white")
        table.add_column("Entity Name / ExtID", style="bold white")
        table.add_column("Details", style="cyan")
        for idx, item in enumerate(items[:12]):
            if isinstance(item, dict):
                name = item.get("name", item.get("extId", item.get("id", f"Item #{idx+1}")))
                detail = str({k: v for k, v in item.items() if k not in ("name", "$reserved", "$objectType")})[:60]
                table.add_row(str(idx + 1), str(name), str(detail))
            else:
                table.add_row(str(idx + 1), str(item), "")
        return table

    except Exception:
        pass
    return None


def create_execution_panel(logs: list[str], plan: list[str], current_step: int, critique: str, active_table: Table | None = None) -> Panel:
    """Builds the main center panel showing active plan, critique, live action status, and entity tables."""
    grid = Table.grid(expand=True)
    grid.add_column()

    # Plan section
    if plan:
        plan_text = Text()
        plan_text.append("Active Reflective Plan:\n", style="bold underline magenta")
        for i, step in enumerate(plan):
            style = "bold green" if i == current_step else ("dim white" if i < current_step else "white")
            prefix = "➜ " if i == current_step else "  "
            plan_text.append(f"{prefix}{step}\n", style=style)
        grid.add_row(plan_text)
        grid.add_row(Text("─" * 60, style="dim black"))

    # Critique section
    if critique:
        grid.add_row(Text(f"Reviewer Critique: {critique}\n", style="italic yellow"))
        grid.add_row(Text("─" * 60, style="dim black"))

    # Render Active Entity Table if parsed
    if active_table:
        grid.add_row(active_table)
        grid.add_row(Text("─" * 60, style="dim black"))

    # Execution logs output (adjust log count so table is never clipped by panel boundary)
    log_count = 4 if active_table else 8
    log_text = Text()
    log_text.append("Live Execution & Diagnostic Output:\n", style="bold underline cyan")
    for log_line in logs[-log_count:]:  # Keep recent lines
        log_text.append(f"{log_line}\n")

    grid.add_row(log_text)

    return Panel(grid, title="[bold cyan]Execution & Reflection Workspace[/bold cyan]", style="cyan")


def build_tui_layout(logs: list[str], plan: list[str], current_step: int, critique: str, cluster_context: dict[str, Any], active_table: Table | None = None) -> Layout:
    """Assembles the complete Rich TUI layout grid."""
    layout = Layout()

    layout.split(
        Layout(name="header", size=4),
        Layout(name="main", ratio=1),
    )

    layout["main"].split_row(
        Layout(name="menu", size=30),
        Layout(name="workspace", ratio=2),
        Layout(name="context", size=40),
    )

    layout["header"].update(create_header())
    layout["menu"].update(create_menu_panel())
    layout["workspace"].update(create_execution_panel(logs, plan, current_step, critique, active_table))
    layout["context"].update(create_context_panel(cluster_context))

    return layout


async def run_tui_app():
    """Main execution loop for Rich Text-Based User Interface (TUI)."""
    console.clear()
    console.print("[bold cyan]========================================================================[/bold cyan]")
    console.print("[bold blue]  Nutanix Prism Central Reflective Agent Dashboard (MCP Stdio Client)[/bold blue]")
    console.print("[bold cyan]========================================================================[/bold cyan]\n")

    console.print("[bold white][1/5][/bold white] [cyan]Loading configuration & environment variables...[/cyan]")
    console.print(f"     • PC_HOST:     [bold green]{Config.PC_HOST}:{Config.PC_PORT}[/bold green]")
    console.print(f"     • PC_USERNAME: [bold green]{Config.PC_USERNAME}[/bold green]")
    console.print(f"     • PC_INSECURE: [bold green]{Config.PC_INSECURE}[/bold green]\n")

    console.print("[bold white][2/5][/bold white] [cyan]Spawning local Nutanix MCP server process over stdio...[/cyan]")
    cmd_tuple = Config.get_server_command()
    console.print(f"     • Command: [dim white]{cmd_tuple[0]} {' '.join(cmd_tuple[1])}[/dim white]\n")

    console.print("[bold white][3/5][/bold white] [cyan]Performing stdio handshake & loading V4 API YAML specifications...[/cyan]")
    start_t = asyncio.get_event_loop().time()
    try:
        tools = await mcp_client_manager.initialize_tools()
        duration = asyncio.get_event_loop().time() - start_t
        console.print(f"     • [bold green]Success:[/bold green] Dynamically mapped [bold yellow]{len(tools)}[/bold yellow] tools across 19 namespaces ({duration:.2f}s).\n")
    except Exception as exc:
        console.print(f"     • [bold red]Error initializing MCP tools:[/bold red] {exc}")
        return

    console.print("[bold white][4/5][/bold white] [cyan]Compiling Reflective StateGraph (Planner -> Executor -> Reviewer)...[/cyan]")
    graph = build_reflective_nutanix_graph()
    console.print("     • [bold green]Success:[/bold green] Reflective State Graph compiled with MemorySaver checkpointer.\n")

    console.print("[bold white][5/5][/bold white] [bold green]System initialization complete! Opening interactive dashboard...[/bold green]\n")
    await asyncio.sleep(1.0)

    thread_config = {"configurable": {"thread_id": "nutanix-tui-thread"}}

    logs = ["System initialized. Select an option [1-5] or type a query."]
    plan = []
    current_step = 0
    critique = ""
    cluster_context = {}
    active_table = None

    while True:
        console.clear()
        layout = build_tui_layout(logs, plan, current_step, critique, cluster_context, active_table)
        console.print(layout)

        console.print("\n[bold green]Nutanix TUI Menu Options:[/bold green] [1] VMs  [2] Storage Containers  [3] Subnets  [4] Custom Query  [5] Exit")
        user_choice = Prompt.ask("Select option [1-5] or type input", default="1").strip()

        if user_choice == "5" or user_choice.lower() in ("exit", "quit"):
            console.print("[bold yellow]Exiting Nutanix TUI Dashboard. Good day![/bold yellow]")
            break

        query = ""
        if user_choice == "1":
            query = "List all Nutanix Virtual Machines using vmm_execute ahv_listVms"
        elif user_choice == "2":
            query = "List all Nutanix Storage Containers using storage_execute listStorageContainers"
        elif user_choice == "3":
            query = "List all Nutanix Network Subnets using networking_execute listSubnets"
        elif user_choice == "4":
            query = Prompt.ask("Enter custom natural language query").strip()
        else:
            query = user_choice

        if not query:
            continue

        # Instant progress log output to workspace panel
        logs.append(f"\n[User Query]: '{query}'")
        logs.append(f"[bold yellow]⌛ [1/3] Initiating query to Prism Central at {Config.PC_HOST}:{Config.PC_PORT}...[/bold yellow]")

        # Immediately refresh screen so user sees live connectivity status!
        console.clear()
        console.print(build_tui_layout(logs, plan, current_step, critique, cluster_context, active_table))

        initial_state: NutanixAgentState = {
            "messages": [HumanMessage(content=query)],
            "plan": [],
            "current_step": 0,
            "critique": "",
            "cluster_context": cluster_context,
            "pending_tool_call": None,
            "approval_granted": None,
            "error_trace": None,
            "retry_count": 0,
        }

        try:
            async for event in graph.astream(initial_state, thread_config):
                for node_name, state_update in event.items():
                    if node_name == "planner":
                        logs.append("[cyan]➜ [2/3] Planner node formulated multi-step reflective execution plan...[/cyan]")
                    elif node_name == "executor":
                        logs.append(f"[cyan]➜ [3/3] Executor node preparing Nutanix MCP tool invocation...[/cyan]")
                    elif node_name == "tools":
                        logs.append(f"[green]✔ MCP Tool call executed against Prism Central ({Config.PC_HOST}:{Config.PC_PORT}).[/green]")
                    else:
                        logs.append(f"-> Executed node: [bold magenta]{node_name}[/bold magenta]")

                    if "plan" in state_update and state_update["plan"]:
                        plan = state_update["plan"]
                        current_step = state_update.get("current_step", 0)

                    if "critique" in state_update and state_update["critique"]:
                        critique = state_update["critique"]

                    if "cluster_context" in state_update and state_update["cluster_context"]:
                        cluster_context.update(state_update["cluster_context"])

                    if node_name == "human_approval":
                        pending = state_update.get("pending_tool_call") or {}
                        logs.append("[bold red]HUMAN-IN-THE-LOOP AUTHORIZATION REQUIRED[/bold red]")
                        logs.append(f"Operation: {pending.get('name')} -> {pending.get('args', {}).get('operation')}")

                        # Redraw layout before asking prompt
                        console.clear()
                        console.print(build_tui_layout(logs, plan, current_step, critique, cluster_context, active_table))

                        choice = Prompt.ask("Authorize state-mutating operation? (y/n)", choices=["y", "n"], default="n")
                        approved = choice == "y"

                        resume_state = {"approval_granted": approved}
                        async for sub_event in graph.astream(resume_state, thread_config):
                            for sub_node, sub_update in sub_event.items():
                                logs.append(f"  -> Resume Node '{sub_node}'")

                    if "messages" in state_update and state_update["messages"]:
                        last_m = state_update["messages"][-1]
                        raw_content = str(last_m.content)

                        # For ToolMessage results: show popup overlay instead of embedding in workspace panel
                        if isinstance(last_m, ToolMessage):
                            from main import _parse_tool_message_entities
                            entities = _parse_tool_message_entities(last_m.content)
                            if entities:
                                popup_title, popup_columns = _build_popup_columns(query)
                                show_entity_popup(popup_title, entities, popup_columns)
                                active_table = None  # Clear embedded table; popup handled display
                                logs.append(f"[bold green]✔ Displayed {len(entities)} {popup_title} in popup view.[/bold green]")
                            elif '"ok": false' in raw_content.lower() or "execution_error" in raw_content:
                                logs.append(f"[bold red]Prism Central API Error:[/bold red] Operation failed or host {Config.PC_HOST} unreachable.")
                            else:
                                logs.append(f"Output: {raw_content[:120]}...")
                        else:
                            # Non-tool messages: render as before (plan text, critique text, etc.)
                            parsed_table = format_entity_table(query, last_m.content)
                            if parsed_table:
                                active_table = parsed_table
                                logs.append("[bold green]✔ Parsed returned Nutanix entities into structured view below.[/bold green]")
                            elif '"ok": false' in raw_content.lower() or "execution_error" in raw_content:
                                logs.append(f"[bold red]Prism Central API Error:[/bold red] Operation failed or host {Config.PC_HOST} unreachable.")
                            else:
                                clean_str = raw_content.replace("\n", " ").strip()
                                logs.append(f"Output: {clean_str[:120]}...")

                    # Real-time screen redraw after each node event!
                    console.clear()
                    console.print(build_tui_layout(logs, plan, current_step, critique, cluster_context, active_table))

        except Exception as exc:
            logs.append(f"[red]Error during graph execution: {exc}[/red]")


def main():
    asyncio.run(run_tui_app())


if __name__ == "__main__":
    main()
