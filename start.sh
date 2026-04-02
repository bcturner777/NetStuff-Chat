#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Check required env vars
if [ -z "$NETBOX_URL" ] || [ -z "$NETBOX_TOKEN" ]; then
    echo "Error: NETBOX_URL and NETBOX_TOKEN must be set in .env or environment"
    exit 1
fi

if [ -z "$MERAKI_API_KEY" ] || [ -z "$MERAKI_ORG_ID" ]; then
    echo "Error: MERAKI_API_KEY and MERAKI_ORG_ID must be set in .env or environment"
    exit 1
fi

if [ -z "$CML_URL" ] || [ -z "$CML_USERNAME" ] || [ -z "$CML_PASSWORD" ]; then
    echo "Error: CML_URL, CML_USERNAME, and CML_PASSWORD must be set in .env or environment"
    exit 1
fi

if [ -z "$PYATS_USERNAME" ] || [ -z "$PYATS_PASSWORD" ]; then
    echo "Error: PYATS_USERNAME and PYATS_PASSWORD must be set in .env or environment"
    exit 1
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "Error: ANTHROPIC_API_KEY must be set in .env or environment"
    exit 1
fi

NETBOX_MCP_PID=""
MERAKI_MCP_PID=""
CML_MCP_PID=""

cleanup() {
    echo ""
    echo "Shutting down..."
    for pid in "$NETBOX_MCP_PID" "$MERAKI_MCP_PID" "$CML_MCP_PID"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            wait "$pid" 2>/dev/null
        fi
    done
    exit 0
}

trap cleanup INT TERM

# Start NetBox MCP server in background (HTTP transport on port 8000)
echo "Starting NetBox MCP server on http://127.0.0.1:8000/mcp ..."
TRANSPORT=http HOST=127.0.0.1 PORT=8000 \
    uv --directory "$SCRIPT_DIR/netbox-mcp-server" run netbox-mcp-server --no-verify-ssl &
NETBOX_MCP_PID=$!

# Start Meraki MCP server in background (HTTP transport on port 8001)
echo "Starting Meraki MCP server on http://127.0.0.1:8001/mcp ..."
MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8001 \
    uv --directory "$SCRIPT_DIR/meraki-mcp-server" run python meraki-mcp-dynamic.py &
MERAKI_MCP_PID=$!

# Wait for both MCP servers to accept TCP connections
wait_for_server() {
    local name="$1" port="$2" pid="$3"
    echo "Waiting for $name MCP server (port $port)..."
    local ready=false
    for i in $(seq 1 30); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "Error: $name MCP server process exited."
            return 1
        fi
        if nc -z 127.0.0.1 "$port" 2>/dev/null; then
            echo "$name MCP server is ready."
            ready=true
            break
        fi
        sleep 1
    done
    if [ "$ready" = false ]; then
        echo "Warning: $name MCP server did not become ready in 30s, continuing anyway."
    fi
    return 0
}

wait_for_server "NetBox" 8000 "$NETBOX_MCP_PID"
wait_for_server "Meraki" 8001 "$MERAKI_MCP_PID"

# Start CML MCP server in background (HTTP transport on port 8002)
echo "Starting CML MCP server on http://127.0.0.1:8002/mcp ..."
CML_MCP_TRANSPORT=http CML_MCP_BIND=127.0.0.1 CML_MCP_PORT=8002 \
    "$SCRIPT_DIR/.venv/bin/cml-mcp" &
CML_MCP_PID=$!

wait_for_server "CML" 8002 "$CML_MCP_PID"

# Start Flask app
echo "Starting Flask app on http://localhost:5001 ..."
"$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/app.py"
