#!/bin/bash
# Stoppt den Live-Clipper und entfernt den Autostart
launchctl bootout "gui/$(id -u)/com.leo.gta6clipbot" 2>/dev/null && echo "Gestoppt." || echo "Lief nicht."
rm -f "$HOME/Library/LaunchAgents/com.leo.gta6clipbot.plist"
