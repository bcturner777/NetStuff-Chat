import asyncio
import base64
import json
import os

import anthropic
import httpx
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv()

app = Flask(__name__)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4096

# Build CML Basic auth header from env vars (required in HTTP transport mode)
_cml_user = os.getenv("CML_USERNAME", "")
_cml_pass = os.getenv("CML_PASSWORD", "")
_cml_auth = base64.b64encode(f"{_cml_user}:{_cml_pass}".encode()).decode()

MCP_SERVERS = [
    {"name": "NetBox", "url": os.getenv("MCP_URL_NETBOX", "http://127.0.0.1:8000/mcp")},
    {"name": "Meraki", "url": os.getenv("MCP_URL_MERAKI", "http://127.0.0.1:8001/mcp")},
    {
        "name": "CML",
        "url": os.getenv("MCP_URL_CML", "http://127.0.0.1:8002/mcp"),
        "headers": {"X-Authorization": f"Basic {_cml_auth}"},
    },
]

client = anthropic.Anthropic()

SYSTEM_PROMPT = (
    "You are a helpful network assistant. You have tools that query live NetBox, "
    "Cisco Meraki, and Cisco Modeling Labs (CML) systems. When the user asks about "
    "network infrastructure, call the appropriate tool, wait for the result, and then "
    "present the returned data in a clear summary. "
    "Never explain how to use the API. Never write code. Never make up data. "
    "If a tool returns an error, tell the user what went wrong — do NOT guess or fabricate results. "
    "\n\n"
    "NetBox tools (netbox_*): "
    "object_type must use dotted format like 'dcim.device', 'ipam.ipaddress', 'dcim.site', 'extras.tag'. "
    "filters must be a JSON object like {} or {\"status\": \"active\"}. "
    "To find resources by tag, use the filter {\"tag\": \"tag-slug\"} (e.g. {\"tag\": \"production\"}). "
    "Do NOT use 'tagged_objects__id' or other multi-hop filters. "
    "\n\n"
    "Meraki tools: "
    "Use the dedicated Meraki tools to query Cisco Meraki. Common tools include: "
    "getOrganizations, getOrganizationNetworks, getOrganizationDevices, "
    "getNetworkClients (requires networkId), getNetworkDevices (requires networkId), "
    "getNetworkWirelessSsids (requires networkId), getDeviceSwitchPorts (requires serial). "
    "For any endpoint not covered by a dedicated tool, use call_meraki_api with a section, method, and parameters. "
    "When a tool requires a networkId, first call getOrganizationNetworks to get the list of networks. "
    "\n\n"
    "CML tools (cml_* or get_cml_* etc.): "
    "Use these tools to interact with Cisco Modeling Labs. Common operations include: "
    "get_cml_labs (list all labs), get_cml_information and get_cml_status (server info), "
    "get_nodes_for_cml_lab (requires lab_id), get_all_links_for_lab (requires lab_id), "
    "start_cml_lab / stop_cml_lab (requires lab_id), send_cli_command (requires lab_id and node_id). "
    "Always call get_cml_labs first when you need a lab_id."
)

# Global store for MCP tools in Anthropic format
anthropic_tools = []
mcp_tool_names = {}
tool_server_map = {}  # tool_name -> server_url for routing execution
tools_discovered = False


def ensure_tools():
    """Discover MCP tools if not already done. Safe to call repeatedly."""
    global anthropic_tools, mcp_tool_names, tool_server_map, tools_discovered
    if tools_discovered:
        return
    try:
        anthropic_tools, mcp_tool_names, tool_server_map = asyncio.run(discover_tools())
        tools_discovered = True
        print(f"Discovered {len(anthropic_tools)} MCP tools: {list(mcp_tool_names.keys())}")
    except Exception as e:
        print(f"Warning: Could not connect to MCP servers: {e}")


async def discover_tools():
    """Connect to all MCP servers and discover available tools."""
    tools = []
    names = {}
    server_map = {}
    for server in MCP_SERVERS:
        try:
            http_client = httpx.AsyncClient(headers=server["headers"]) if server.get("headers") else None
            async with streamable_http_client(server["url"], http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    for tool in result.tools:
                        tools.append({
                            "name": tool.name,
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema,
                        })
                        names[tool.name] = tool.description or tool.name
                        server_map[tool.name] = server["url"]
                    print(f"  {server['name']}: discovered {len(result.tools)} tools")
        except Exception as e:
            print(f"  Warning: Could not connect to {server['name']} MCP server at {server['url']}: {e}")
    return tools, names, server_map


def fixup_tool_args(name, args):
    """Fix common argument mistakes from models.

    Coerces dicts/lists passed as strings, fixes wrong types for
    ints/bools, corrects dotted prefixes on object_type, and clamps
    limits. Defense-in-depth — unlikely to trigger with Claude but
    the NetBox-specific semantic fixes correct domain knowledge gaps.
    """
    args = dict(args)

    # --- Type coercion ---

    # Dicts passed as JSON strings
    for key in ("filters",):
        if key in args and isinstance(args[key], str):
            try:
                args[key] = json.loads(args[key])
            except (json.JSONDecodeError, TypeError):
                args[key] = {}

    # Lists passed as strings (JSON or Python repr)
    for key in ("fields", "object_types", "ordering"):
        val = args.get(key)
        if isinstance(val, str):
            # Try JSON first, then Python-style single quotes
            for attempt in (val, val.replace("'", '"')):
                try:
                    parsed = json.loads(attempt)
                    if isinstance(parsed, list):
                        args[key] = parsed
                        break
                except (json.JSONDecodeError, TypeError):
                    continue

    # Ints passed as strings
    for key in ("limit", "offset", "object_id"):
        if key in args and isinstance(args[key], str):
            try:
                args[key] = int(args[key])
            except ValueError:
                pass

    # Bools passed as strings
    for key in ("brief",):
        val = args.get(key)
        if isinstance(val, str):
            args[key] = val.lower() in ("true", "1", "yes")

    # Clamp limit to 1–100
    if "limit" in args and isinstance(args["limit"], int):
        args["limit"] = max(1, min(args["limit"], 100))

    # --- NetBox-specific semantic fixes ---
    # Only apply these when the tool belongs to NetBox
    if name.startswith("netbox_"):
        # Fix object_type — correct wrong or missing app prefix
        ot = args.get("object_type", "")
        if ot:
            # Map short names to correct dotted type
            short_map = {
                "device": "dcim.device", "site": "dcim.site", "rack": "dcim.rack",
                "interface": "dcim.interface", "cable": "dcim.cable",
                "manufacturer": "dcim.manufacturer", "platform": "dcim.platform",
                "ipaddress": "ipam.ipaddress", "prefix": "ipam.prefix",
                "vlan": "ipam.vlan", "vrf": "ipam.vrf",
                "virtualmachine": "virtualization.virtualmachine",
                "cluster": "virtualization.cluster",
                "circuit": "circuits.circuit", "provider": "circuits.provider",
                "tenant": "tenancy.tenant",
                "tag": "extras.tag", "webhook": "extras.webhook",
                "customfield": "extras.customfield",
                "journalentry": "extras.journalentry",
            }
            # Map wrongly-prefixed types to correct ones (model often puts
            # extras types under dcim/ipam)
            correction_map = {
                "dcim.tag": "extras.tag", "ipam.tag": "extras.tag",
                "dcim.webhook": "extras.webhook", "ipam.webhook": "extras.webhook",
                "dcim.customfield": "extras.customfield",
                "dcim.journalentry": "extras.journalentry",
                "dcim.virtualmachine": "virtualization.virtualmachine",
                "ipam.device": "dcim.device", "ipam.site": "dcim.site",
                "ipam.interface": "dcim.interface", "ipam.rack": "dcim.rack",
                "dcim.ipaddress": "ipam.ipaddress", "dcim.vlan": "ipam.vlan",
                "dcim.prefix": "ipam.prefix", "dcim.vrf": "ipam.vrf",
            }
            ot_lower = ot.lower()
            if ot_lower in correction_map:
                args["object_type"] = correction_map[ot_lower]
            elif "." not in ot:
                args["object_type"] = short_map.get(ot_lower, f"dcim.{ot_lower}")

        # Fix common filter key mistakes from the model
        if "filters" in args and isinstance(args["filters"], dict):
            filters = args["filters"]
            # Model uses multi-hop filters that NetBox MCP rejects;
            # rewrite to the correct single-key filter
            filter_rewrites = {
                "tagged_objects__id": "tag",
                "tagged_objects__name": "tag",
                "tags__name": "tag",
                "tags__id": "tag",
                "tags__slug": "tag",
            }
            rewritten = {}
            for k, v in filters.items():
                new_key = filter_rewrites.get(k, k)
                rewritten[new_key] = v
            args["filters"] = rewritten

    # Strip None-valued keys (model sometimes sends ordering=None)
    args = {k: v for k, v in args.items() if v is not None}

    return args


async def execute_tool(name, arguments):
    """Execute a tool via MCP and return the text result."""
    arguments = fixup_tool_args(name, arguments)
    server_url = tool_server_map.get(name)
    if not server_url:
        return f"Error: unknown tool '{name}' — not found on any MCP server"
    server_headers = next((s.get("headers") for s in MCP_SERVERS if s["url"] == server_url), None)
    http_client = httpx.AsyncClient(headers=server_headers) if server_headers else None
    async with streamable_http_client(server_url, http_client=http_client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            texts = []
            for content in result.content:
                if hasattr(content, "text"):
                    texts.append(content.text)
            return "\n".join(texts) if texts else str(result)


def parse_text_tool_call(text):
    """Detect and parse a raw JSON tool call emitted as text.

    Safety net — unlikely to trigger with Claude but harmless to keep.
    If the text looks like {"name": ..., "parameters": ...},
    parse it and return (tool_name, tool_args) so we can execute it anyway.
    Returns None if the text isn't a tool call.
    """
    text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        # Normalize Python-style bools/None for JSON parsing
        normalized = text.replace("False", "false").replace("True", "true").replace("None", "null")
        obj = json.loads(normalized)
        if "name" in obj and "parameters" in obj:
            return obj["name"], obj["parameters"]
    except (json.JSONDecodeError, TypeError):
        pass
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    # Lazy tool discovery — retry if startup failed
    ensure_tools()

    data = request.get_json()
    user_message = data.get("message", "")
    history = data.get("history", [])

    history.append({"role": "user", "content": user_message})

    messages = list(history)

    def generate():
        try:
            MAX_TOOL_ROUNDS = 10  # Safety limit for agentic loops
            total_input_tokens = 0
            total_output_tokens = 0
            tool_rounds = 0

            for _ in range(MAX_TOOL_ROUNDS):
                with client.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    tools=anthropic_tools or [],
                ) as stream:
                    for text in stream.text_stream:
                        yield f"data: {json.dumps({'type': 'token', 'token': text})}\n\n"
                    response = stream.get_final_message()

                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens

                tool_use_blocks = [
                    block for block in response.content if block.type == "tool_use"
                ]

                # Fallback: model emitted a raw JSON tool call as text
                if not tool_use_blocks:
                    full_text = "".join(
                        block.text for block in response.content if block.type == "text"
                    )
                    parsed = parse_text_tool_call(full_text)
                    if parsed:
                        tool_name, tool_args = parsed
                        yield f"data: {json.dumps({'type': 'clear'})}\n\n"
                        yield f"data: {json.dumps({'type': 'status', 'content': f'Calling {tool_name}...'})}\n\n"
                        try:
                            result = asyncio.run(execute_tool(tool_name, tool_args))
                            if result.startswith("Error"):
                                result = f"TOOL ERROR — do NOT make up data. Report this error to the user: {result}"
                        except Exception as e:
                            result = f"TOOL ERROR — do NOT make up data. Report this error to the user: {e}"
                        messages.append({"role": "assistant", "content": full_text})
                        messages.append({"role": "user", "content": result})
                        tool_rounds += 1
                        continue  # Loop back for next Claude response

                    break  # No tool calls — we're done

                # Add assistant's full response (text + tool_use blocks)
                messages.append({"role": "assistant", "content": response.content})

                # Execute each tool and collect results
                tool_results = []
                for block in tool_use_blocks:
                    yield f"data: {json.dumps({'type': 'status', 'content': f'Calling {block.name}...'})}\n\n"
                    try:
                        result = asyncio.run(execute_tool(block.name, block.input))
                        if result.startswith("Error"):
                            result = f"TOOL ERROR — do NOT make up data. Report this error to the user: {result}"
                    except Exception as e:
                        result = f"TOOL ERROR — do NOT make up data. Report this error to the user: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                messages.append({"role": "user", "content": tool_results})
                tool_rounds += 1
                # Loop back — Claude may need to make more tool calls

            # Log and emit token usage summary
            total_tokens = total_input_tokens + total_output_tokens
            print(
                f"Token usage — input: {total_input_tokens}, "
                f"output: {total_output_tokens}, "
                f"total: {total_tokens}, "
                f"tool rounds: {tool_rounds}"
            )
            yield f"data: {json.dumps({'type': 'usage', 'input': total_input_tokens, 'output': total_output_tokens, 'total': total_tokens, 'rounds': tool_rounds})}\n\n"
            yield "data: [DONE]\n\n"
        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# Try to discover tools at startup (will retry on first request if this fails)
ensure_tools()


if __name__ == "__main__":
    app.run(debug=True, port=5001)
