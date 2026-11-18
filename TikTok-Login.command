#!/bin/bash
# Doppelklick: TikTok-Login für den Clip-Bot (eigenes Browserfenster, einmalig nötig).
cd "$(dirname "$0")"
echo "TikTok-Upload für den GTA6-Clipbot einrichten …"
.venv/bin/python tiktok_web.py login 2>&1 | grep -v "objc\[" 
echo ""
echo "Fertig – dieses Fenster kann geschlossen werden."
