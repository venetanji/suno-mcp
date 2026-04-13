#!/bin/bash
set -e

# Start display services (Xvfb, x11vnc, noVNC) in background
supervisord -c /etc/supervisor/conf.d/supervisord.conf

# Wait for Xvfb to be ready
sleep 2

# Activate venv and run the MCP server
. /app/.venv/bin/activate
exec python -m suno_mcp.server "$@"
