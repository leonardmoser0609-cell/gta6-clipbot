#!/bin/bash
# Einmalige Einrichtung auf dem Mac: Python-Umgebung + Hintergrunddienst (launchd)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
LABEL="com.leo.gta6clipbot"
echo "== GTA6 Live-Clipper: Einrichtung =="
command -v ffmpeg >/dev/null || { echo "ffmpeg fehlt – installiere über Homebrew"; brew install ffmpeg; }
if ! command -v uv >/dev/null; then
  echo "Installiere uv (Python-Manager) …"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -q -r requirements.txt
echo "Lade Whisper-Modell (einmalig) …"
.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8'); print('Whisper ok')"
chmod +x run.sh
mkdir -p logs "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/run.sh</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo ""
echo "✅ Live-Clipper läuft im Hintergrund (startet auch nach Neustart automatisch)."
echo "   Logs:   $DIR/logs/livebot.log"
echo "   Stop:   bash $DIR/stop.sh"
echo "   Laptop bitte aufgeklappt und am Strom lassen."
