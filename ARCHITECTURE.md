# NetStuff-Chat Architecture

## System Overview

NetStuff-Chat is an agentic chat application that connects Claude (via the Anthropic API) to live network infrastructure data via the Model Context Protocol (MCP). Claude acts as an autonomous agent — it decides when to query NetBox or Meraki, formulates the right API call, and summarizes the results for the user.

```
                         NetStuff-Chat Application
 ┌─────────────────────────────────────────────────────────────────┐
 │                                                                 │
 │   ┌──────────┐       ┌───────────────┐       ┌──────────────┐   │
 │   │          │  SSE  │               │ HTTPS │              │   │
 │   │ Browser  │◄─────►│  Flask App    │◄─────►│ Anthropic    │   │
 │   │   UI     │       │  (app.py)     │       │ API (Claude) │   │
 │   │          │       │               │       │              │   │
 │   └──────────┘       └──────┬────────┘       └──────────────┘   │
 │                             │                                   │
 │                    MCP (HTTP) + tool routing                    │
 │                      ┌──────┴──────┐                            │
 │                      │             │                            │
 │              ┌───────▼───────┐ ┌───▼───────────┐                │
 │              │   NetBox MCP  │ │  Meraki MCP   │                │
 │              │    Server     │ │    Server     │                │
 │              │  (port 8000)  │ │  (port 8001)  │                │
 │              └───────┬───────┘ └───────┬───────┘                │
 │                      │                 │                        │
 └──────────────────────┼─────────────────┼────────────────────────┘
                        │ REST API        │ REST API
                        │                 │
               ┌────────▼────────┐ ┌──────▼──────────┐
               │                 │ │                  │
               │  NetBox Cloud   │ │  Meraki Cloud    │
               │   Instance      │ │   Dashboard      │
               │                 │ │                  │
               └─────────────────┘ └──────────────────┘
```

## Component Details

### Browser UI (`templates/index.html`)

The frontend is a single-page chat interface that communicates with Flask via Server-Sent Events (SSE).

- Sends user messages as JSON POST to `/chat`
- Receives streamed `token` events for real-time text display
- Receives `status` events during tool execution (shows a spinner)
- Maintains conversation history client-side

### Flask App (`app.py`)

The orchestration layer that bridges Claude, MCP servers, and the browser. This is where the agentic behavior lives.

**Responsibilities:**
- Serves the web UI and `/chat` SSE endpoint
- Manages MCP client connections to multiple servers for tool discovery and execution
- Builds a `tool_server_map` during discovery to route each tool to its correct server
- Passes full MCP tool schemas and descriptions to Claude (no simplification needed)
- Fixes malformed tool arguments as defense-in-depth before forwarding to MCP (NetBox-specific fixes are guarded to only apply to `netbox_*` tools)
- Streams Claude's responses token-by-token to the browser

### Anthropic API (Claude)

The LLM backend. The app uses Claude Haiku 4.5 via the Anthropic API with native tool-calling support.

- Receives messages + tool definitions from Flask (unified list from all MCP servers)
- Decides autonomously whether to respond directly or invoke a tool
- Returns either text content or structured `tool_use` blocks
- Handles multi-step tool calling (e.g., fetching network IDs before querying clients)

### NetBox MCP Server

An MCP-compliant server that wraps the NetBox REST API. Runs locally on port 8000 using HTTP transport.

**Exposed tools (read-only):**

| Tool | Purpose |
|------|---------|
| `netbox_get_objects` | List/filter objects by type (devices, IPs, VLANs, etc.) |
| `netbox_get_object_by_id` | Get a single object by its ID |
| `netbox_search_objects` | Global search across multiple object types |
| `netbox_get_changelogs` | View audit trail / change history |

### Meraki MCP Server

An MCP-compliant server that wraps the Cisco Meraki Dashboard API. Runs locally on port 8001 using streamable HTTP transport.

**Exposed tools:**

| Tool | Purpose |
|------|---------|
| `call_meraki_api` | Generic Meraki API caller — accepts a section, method, and parameters to query any Meraki Dashboard API endpoint (organizations, networks, devices, SSIDs, clients, etc.) |

### NetBox Cloud

The remote source of truth for network infrastructure data. The MCP server authenticates via API token and queries the REST API.

### Meraki Cloud Dashboard

The remote source of truth for Meraki-managed network infrastructure. The MCP server authenticates via API key and queries the Meraki Dashboard API.

## Multi-Server Tool Routing

With two MCP servers, the app needs to route tool calls to the correct server. This is handled by a `tool_server_map` built during discovery:

```
 discover_tools() loops over MCP_SERVERS:
     NetBox (port 8000) → netbox_get_objects, netbox_get_object_by_id, ...
     Meraki (port 8001) → call_meraki_api

 tool_server_map = {
     "netbox_get_objects":      "http://127.0.0.1:8000/mcp",
     "netbox_get_object_by_id": "http://127.0.0.1:8000/mcp",
     "netbox_search_objects":   "http://127.0.0.1:8000/mcp",
     "netbox_get_changelogs":   "http://127.0.0.1:8000/mcp",
     "call_meraki_api":         "http://127.0.0.1:8001/mcp",
 }
```

When `execute_tool()` is called, it looks up the server URL from this map and connects to the correct MCP server. Since MCP tool names are namespaced by convention (`netbox_*` vs `call_meraki_api`), no name prefixing is needed.

Individual server failures during discovery are handled gracefully — if one server is down, tools from the other server still work.

## Agentic Flow

The key architectural pattern is the **agentic tool-calling loop**. Claude is not hard-coded to call specific tools — it autonomously decides what to do based on the user's question and the available tool definitions.

```
 User: "What devices are in my network?"
  │
  ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ Step 1: Flask sends message + tool definitions to Claude     │
 │                                                             │
 │   system: system_prompt                                     │
 │   messages: [user_message]                                  │
 │   tools:    [netbox_get_objects, netbox_get_object_by_id,   │
 │              netbox_search_objects, netbox_get_changelogs,   │
 │              call_meraki_api]                                │
 └─────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ Step 2: Claude decides to call a tool (autonomous decision)  │
 │                                                             │
 │   content: [ToolUseBlock(                                   │
 │     name: "netbox_get_objects",                             │
 │     input: {object_type: "dcim.device", filters: {}}        │
 │   )]                                                        │
 └─────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ Step 3: Flask routes to correct server, fixes args, executes│
 │                                                             │
 │   tool_server_map["netbox_get_objects"]                     │
 │     → http://127.0.0.1:8000/mcp                            │
 │   fixup_tool_args() ─► MCP call_tool() ─► NetBox REST API  │
 │                                                             │
 │   Result: {"count":1, "results":[{"name":"bubba-sw1",...}]} │
 └─────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ Step 4: Flask feeds tool result back to Claude               │
 │                                                             │
 │   messages: [user, assistant(tool_use), user(tool_result)]  │
 └─────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ Step 5: Claude generates final response (streamed to browser)│
 │                                                             │
 │   "There is 1 device in your network: bubba-sw1,           │
 │    a Cisco cml-iosv access switch at bubba-site1."          │
 └─────────────────────────────────────────────────────────────┘
```

For simple questions like "Hello, how are you?" Claude skips Steps 2-4 entirely and responds directly. The decision is made by the model, not by application code.

## MCP Integration Details

### Why MCP?

The [Model Context Protocol](https://modelcontextprotocol.io/) provides a standardized way to connect LLMs to external data sources. Instead of hard-coding API calls, the app discovers tools dynamically at startup:

```
 Flask App                    MCP Server(s)
     │                            │
     │──── initialize() ─────────►│  (repeated for each server)
     │◄─── capabilities ──────────│
     │                            │
     │──── list_tools() ─────────►│
     │◄─── tool schemas ──────────│  ◄── Dynamic discovery
     │                            │
     │──── call_tool(name, args)─►│
     │◄─── result ────────────────│  ◄── Routed via tool_server_map
     │                            │
```

This means the app automatically adapts if either MCP server adds new tools — no code changes needed.

### Argument Fixup Layer

The `fixup_tool_args()` function provides defense-in-depth by correcting common argument issues before forwarding to MCP. While unlikely to trigger with Claude, the NetBox-specific semantic fixes correct domain knowledge gaps:

| Model Output | Fixed Value | Issue |
|---|---|---|
| `filters: "{}"` | `filters: {}` | Dict serialized as string |
| `fields: "['id', 'name']"` | `fields: ["id", "name"]` | List as Python repr string |
| `limit: "1000"` | `limit: 100` | String instead of int + over max |
| `brief: "true"` | `brief: true` | Bool as string |
| `object_type: "device"` | `object_type: "dcim.device"` | Missing app prefix |

Generic type coercion (strings→dicts, strings→ints, strings→bools) applies to all tools. NetBox-specific semantic fixes (object_type correction, filter key rewrites) are guarded to only run for `netbox_*` tools.

## Protocol & Transport Summary

| Connection | Protocol | Transport | Port |
|---|---|---|---|
| Browser to Flask | HTTP + SSE | TCP | 5001 |
| Flask to Anthropic API | HTTPS (REST) | TCP | 443 |
| Flask to NetBox MCP Server | MCP over HTTP | Streamable HTTP | 8000 |
| Flask to Meraki MCP Server | MCP over HTTP | Streamable HTTP | 8001 |
| NetBox MCP Server to NetBox | HTTPS (REST) | TCP | 443 |
| Meraki MCP Server to Meraki | HTTPS (REST) | TCP | 443 |
