#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DISPLAY="${DISPLAY:-:0}"
cd "$DIR"
exec "$DIR/venv/bin/python3" "$DIR/main.py" "$@"
