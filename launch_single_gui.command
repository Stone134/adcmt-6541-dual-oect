#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
for plugin_dir in "$SCRIPT_DIR"/.venv/lib/python*/site-packages/PyQt5/Qt5/plugins; do
    if [ -d "$plugin_dir" ]; then
        chflags -R nohidden "$plugin_dir"
    fi
done
exec "$SCRIPT_DIR/.venv/bin/python" oect_single_gui.py
