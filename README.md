# NetStuff-Chat

A local AI chat app powered by [Claude Haiku 4.5](https://www.anthropic.com/) (Anthropic API), with [NetBox](https://netboxlabs.com/) and [Cisco Meraki](https://meraki.cisco.com/) integration via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). Ask questions about your network infrastructure directly in the chat and get live answers from your NetBox inventory and Meraki dashboard.

## Prerequisites

- **Python 3.10+**
- **An [Anthropic API key](https://console.anthropic.com/)** for Claude
- **[uv](https://docs.astral.sh/uv/)** (for the MCP servers):
  ```bash
  brew install uv
  ```
- **A NetBox instance** with an API token
- **A Cisco Meraki dashboard** with an API key and organization ID

## Setup

```bash
# Clone the NetBox MCP server
git clone https://github.com/netboxlabs/netbox-mcp-server.git
cd netbox-mcp-server && uv sync && cd ..

# Clone the Meraki MCP server
git clone https://github.com/CiscoDevNet/meraki-magic-mcp-community.git meraki-mcp-server
cd meraki-mcp-server && uv sync && cd ..

# Create Python venv and install dependencies
python3.10 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your_anthropic_api_key_here
NETBOX_URL=https://your-instance.cloud.netboxapp.com
NETBOX_TOKEN=your_api_token_here
MERAKI_API_KEY=your_meraki_api_key_here
MERAKI_ORG_ID=your_meraki_org_id_here
```

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude | *(required)* |
| `NETBOX_URL` | Your NetBox instance URL | *(required)* |
| `NETBOX_TOKEN` | NetBox API token (read-only is fine) | *(required)* |
| `MERAKI_API_KEY` | Cisco Meraki dashboard API key | *(required)* |
| `MERAKI_ORG_ID` | Meraki organization ID | *(required)* |
| `MCP_URL_NETBOX` | NetBox MCP server endpoint | `http://127.0.0.1:8000/mcp` |
| `MCP_URL_MERAKI` | Meraki MCP server endpoint | `http://127.0.0.1:8001/mcp` |

## Usage

### Quick Start

```bash
./start.sh
```

This starts both the NetBox MCP server (port 8000), the Meraki MCP server (port 8001), and the Flask chat app (port 5001). Open [http://localhost:5001](http://localhost:5001) in your browser.

### Manual Start

```bash
# Terminal 1: Start the NetBox MCP server
source .env
TRANSPORT=http uv --directory netbox-mcp-server run netbox-mcp-server

# Terminal 2: Start the Meraki MCP server
source .env
MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8001 \
    uv --directory meraki-mcp-server run python meraki-mcp-dynamic.py

# Terminal 3: Start the Flask app
source venv/bin/activate
python app.py
```

### CLI Chat

```bash
python chat.py
```

An interactive terminal chat (uses Ollama/Mistral separately — not part of the main web app).

## Features

- Chat with Claude Haiku 4.5 via a browser-based UI with real-time streaming (SSE)
- Query NetBox infrastructure data through natural language (devices, IPs, sites, VLANs, etc.) — read-only operations only; creating, updating, or deleting objects is not supported
- Query Cisco Meraki dashboard data through natural language (networks, devices, SSIDs, clients, etc.)
- Multi-server MCP integration with automatic tool discovery from both NetBox and Meraki MCP servers
- Tool-to-server routing: each tool is automatically mapped to the correct MCP server
- Visual status indicators in the UI while tools execute ("Calling netbox_get_objects...")
- Full MCP tool schemas and descriptions passed through to Claude for accurate tool selection
- Argument fixup layer for defense-in-depth (type coercion, NetBox-specific semantic fixes)
- Graceful degradation: if one MCP server is down, tools from the other server still work
- Conversation history and "New Chat" to start fresh

## How It Works

1. On startup, the app connects to both MCP servers and discovers available tools (`netbox_get_objects`, `netbox_get_object_by_id`, etc. from NetBox; `call_meraki_api` from Meraki)
2. A `tool_server_map` is built to route each tool name to the correct MCP server
3. Full MCP tool schemas and descriptions are passed to Claude (no simplification needed)
4. When you ask a question, Claude decides whether to call a tool or respond directly
5. If a tool is called, the app routes it to the correct MCP server, executes via MCP, feeds the result back, and streams the final summary

## Project Structure

```
app.py               # Flask web server with multi-server MCP tool integration
templates/index.html # Browser-based chat UI
start.sh             # Starts both MCP servers + Flask app
chat.py              # CLI chat interface (uses Ollama separately, no tool support)
.env                 # Anthropic + NetBox + Meraki credentials (gitignored)
netbox-mcp-server/   # Cloned NetBox MCP server (gitignored)
meraki-mcp-server/   # Cloned Meraki MCP server (gitignored)
```
