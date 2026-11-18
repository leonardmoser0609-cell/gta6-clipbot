#!/bin/bash
# Startet den Live-Clipper (wird von launchd aufgerufen). caffeinate verhindert den Ruhezustand.
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1
exec /usr/bin/caffeinate -is ./.venv/bin/python livebot.py
