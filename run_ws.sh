#!/usr/bin/env bash
# Run Django with WebSocket support (ASGI). Use this instead of runserver if you need real-time notifications.
cd "$(dirname "$0")"
echo "Starting server with WebSocket support on http://127.0.0.1:8000"
echo "Stop with Ctrl+C"
uv run uvicorn config.asgi:application --reload --host 127.0.0.1 --port 8000
