# GTA 6 Clip-Bot

## Live-Clipper (Hauptmodus, läuft auf dem Mac)
`livebot.py` beobachtet dauerhaft die **Top 5 deutschen Live-Streams in „Grand Theft Auto VI“**,
liest deren Chat mit und erkennt Hype-Momente (Chat-Aktivität ein Vielfaches über normal, viel
KEKW/LUL/WTF). Dann erstellt er **selbst** einen Twitch-Clip, transkribiert ihn, bewertet ihn
(Claude, falls `ANTHROPIC_API_KEY` gesetzt, sonst regelbasiert), rendert 9:16 mit Untertiteln
und schickt Video + Caption per Telegram – meist 1–3 Minuten nach dem Moment.

- Einrichtung: `.env` anlegen (siehe unten), dann einmal `bash setup_mac.sh` im Terminal.
  Danach läuft der Bot als Hintergrunddienst (launchd), startet nach Absturz/Neustart von selbst
  und verhindert den Ruhezustand. Stoppen: `bash stop.sh`.
- Beim ersten Start schickt der Bot per Telegram einen Twitch-Freigabe-Link (Recht „Clips erstellen“).
- Telegram-Befehle: `/status`, `/pause`, `/resume`, `/test <streamer>`
- Logs: `logs/livebot.log`. Änderungen an `.py`/`.env` lösen automatisch einen Neustart aus.

`.env`:
```
TWITCH_CLIENT_ID=…
TWITCH_CLIENT_SECRET=…
TELEGRAM_BOT_TOKEN=…
TELEGRAM_CHAT_ID=…
# optional
ANTHROPIC_API_KEY=…        # KI-Bewertung + Caption
TOP_N=5                    # Anzahl beobachteter Streams
SPIKE_FACTOR=3.0           # Empfindlichkeit (niedriger = mehr Clips)
MIN_SCORE=6                # Mindestbewertung 1–10
COOLDOWN_S=150             # Mindestabstand pro Streamer
MAX_PER_HOUR=20
CLIP_DURATION=40
WHISPER_MODEL=small
```

## Stündlicher Modus (GitHub Actions, nur noch manuell)

Holt jede Stunde die meistgesehenen Twitch-Clips der Top-10 deutschen GTA-VI-Streamer,
macht daraus 9:16-Videos mit deutschen Untertiteln (Whisper) und schickt sie per Telegram –
inklusive kopierfertigem Caption-Text für TikTok und Instagram.

## Ablauf pro Stunde
1. Twitch-API: deutschsprachige Live-Streams in „Grand Theft Auto VI“, Top 10 nach Zuschauern
2. Clips dieser Streamer aus den letzten 90 Minuten, schon verschickte werden übersprungen
3. Die 3 meistgesehenen Clips, max. 1 pro Streamer
4. Download → 9:16 mit unscharfem Hintergrund, Hook oben, Untertitel Wort für Wort, Credit unten
5. Telegram: Video + Info (Views, Link) + Caption-Block mit Kopier-Button

## Einrichtung (GitHub Actions)
1. Repo auf GitHub (öffentlich = unbegrenzte Gratis-Minuten), Code pushen.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TWITCH_CLIENT_ID`
   - `TWITCH_CLIENT_SECRET`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID` – deine Telegram-ID. Herausfinden: dem Bot „hi“ schreiben, dann im Browser
     `https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen → Zahl bei `"chat":{"id": …}`.
     (Fehlt sie, versucht der Bot sie selbst aus dem letzten „hi“ zu lesen.)
3. **Actions → GTA6 Clips → Run workflow**: zuerst mit Haken bei „Nur anzeigen“ (Dry Run), dann einmal scharf.
4. Ab dann läuft es automatisch jede Stunde (um ca. :05).

## Optionale Einstellungen
Unter **Settings → Secrets and variables → Actions → Variables**:

| Variable | Standard | Bedeutung |
|---|---|---|
| `CLIPS_PER_RUN` | 3 | Clips pro Stunde |
| `TOP_STREAMERS` | 10 | Pool: Top-N Live-Streams nach Zuschauern |
| `FIXED_STREAMERS` | – | feste Liste statt automatisch, z. B. `name1,name2,name3` (Twitch-Loginnamen) |
| `LOOKBACK_MIN` | 90 | Zeitfenster für Clips in Minuten |
| `MIN_VIEWS` | 0 | Mindest-Views pro Clip |
| `WHISPER_MODEL` | small | `small` = schnell, `medium` / `turbo` = genauer, langsamer |
| `VIDEO_ZOOM` | 1.0 | 1.0 = ganzes Bild, 1.3 = Gameplay größer, Seiten abgeschnitten |
| `NOTIFY_EMPTY` | – | `1` = auch melden, wenn es keine neuen Clips gibt |
| `HASHTAGS` | #gta6 #gtavi … | Hashtags im Caption-Vorschlag |

## Lokal testen
```bash
pip install -r requirements.txt     # außerdem ffmpeg installieren
python clipbot.py --test mein_clip.mp4   # rendert nach test_output/
TWITCH_CLIENT_ID=… TWITCH_CLIENT_SECRET=… python clipbot.py --dry-run
```

## Hinweis
Die Clips gehören den Streamern. Credit steht automatisch im Video und in der Caption. Am sichersten
fährst du mit Streamern, die Clipper ausdrücklich erlauben.

Schrift: Montserrat (SIL Open Font License, siehe `fonts/OFL.txt`).
