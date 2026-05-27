#!/bin/bash
# twenty-crm-setup.sh — Stand up Twenty CRM MCP server
set -e

SERVER_DIR="/tmp/node_modules/twenty-mcp-server"
echo "=== Twenty CRM MCP Server ==="
echo "Server location: $SERVER_DIR"

# Check if installed
if [ ! -d "$SERVER_DIR" ]; then
  echo "Installing twenty-mcp-server..."
  cd /tmp && npm install twenty-mcp-server 2>&1 | tail -3
fi

cd "$SERVER_DIR"

# Check credentials
if grep -q "your-api-key-here" .env 2>/dev/null; then
  echo "ACTION REQUIRED: Add TWENTY_API_URL and TWENTY_API_KEY to $SERVER_DIR/.env"
fi

echo "Start: cd $SERVER_DIR && npm start"
echo "Config: add to ~/.hermes/config.yaml mcp_servers section"
