#!/bin/bash
# Doppelklick: Instagram-Login für den Clip-Bot (privates Konto, eigenes Browserfenster, einmalig nötig).
cd "$(dirname "$0")"
echo "Instagram-Upload (privates Konto) für den GTA6-Clipbot einrichten …"
.venv/bin/python insta_web.py login 2>&1 | grep -v "objc\["
echo ""
echo "Fertig – dieses Fenster kann geschlossen werden."
