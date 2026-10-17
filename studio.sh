#!/usr/bin/env bash
# Start Viral Studio locally: http://127.0.0.1:4320  (Ctrl+C to stop)
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
exec "$PY" -m vstudio serve --open "$@"
