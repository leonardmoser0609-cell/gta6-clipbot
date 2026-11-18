#!/usr/bin/env python3
"""
GTA 6 Clip-Bot
==============
Holt jede Stunde die meistgesehenen Twitch-Clips der Top-10 deutschen
GTA-VI-Streamer, macht daraus 9:16-Videos mit eingebrannten deutschen
Untertiteln und schickt sie per Telegram – fertig zum Posten auf TikTok/Reels.

Aufruf:
  python clipbot.py                 # normaler Lauf (Twitch -> Render -> Telegram)
  python clipbot.py --dry-run       # nur zeigen, welche Clips gewählt würden
  python clipbot.py --test in.mp4   # lokales Video rendern (ohne Twitch/Telegram)

Konfiguration über Umgebungsvariablen (siehe README.md).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "sent.json"
FONT_DIR = ROOT / "fonts"
FONT_NAME = "Montserrat ExtraBold"


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


CFG = {
    "game_name": os.getenv("GAME_NAME") or "Grand Theft Auto VI",
    "game_id": os.getenv("GAME_ID") or "",
    "language": os.getenv("STREAM_LANGUAGE") or "de",
    "top_streamers": env_int("TOP_STREAMERS", 10),     # Pool: Top-N Live-Streams nach Zuschauern
    "clips_per_run": env_int("CLIPS_PER_RUN", 3),      # so viele Clips pro Stunde
    "per_streamer": env_int("CLIPS_PER_STREAMER", 1),  # max. Clips pro Streamer und Lauf
    "lookback_min": env_int("LOOKBACK_MIN", 90),       # Clips der letzten X Minuten
    "min_views": env_int("MIN_VIEWS", 0),
    "max_duration": env_float("MAX_CLIP_SECONDS", 90),
    "fixed_streamers": [s.strip().lower() for s in (os.getenv("FIXED_STREAMERS") or "").split(",") if s.strip()],
    "whisper_model": os.getenv("WHISPER_MODEL") or "small",
    "zoom": env_float("VIDEO_ZOOM", 1.0),              # 1.0 = ganzes Bild sichtbar, >1 = größer, Seiten abgeschnitten
    "notify_empty": os.getenv("NOTIFY_EMPTY") == "1",
    "hashtags": os.getenv("HASHTAGS") or "#gta6 #gtavi #gta #twitchclips #gaming #fyp",
}

W, H = 1080, 1920  # Ausgabeformat 9:16


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# State (bereits verschickte Clips, damit nichts doppelt kommt)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"sent": []}


def save_state(state: dict) -> None:
    state["sent"] = state["sent"][-1000:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Twitch
# ---------------------------------------------------------------------------

class Twitch:
    API = "https://api.twitch.tv/helix/"

    def __init__(self, client_id: str, client_secret: str):
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={"client_id": client_id, "client_secret": client_secret,
                    "grant_type": "client_credentials"},
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"Twitch-Login fehlgeschlagen ({r.status_code}): {r.text[:200]}")
        self.s = requests.Session()
        self.s.headers.update({"Client-Id": client_id,
                               "Authorization": f"Bearer {r.json()['access_token']}"})

    def get(self, path: str, **params) -> dict:
        for attempt in range(3):
            r = self.s.get(self.API + path, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + attempt * 3)
                continue
            if not r.ok:
                raise RuntimeError(f"Twitch {path} {r.status_code}: {r.text[:200]}")
            return r.json()
        raise RuntimeError(f"Twitch {path}: zu viele Fehlversuche")

    def game_id(self, name: str) -> str:
        data = self.get("games", name=name)["data"]
        if data:
            return data[0]["id"]
        data = self.get("search/categories", query=name, first=10)["data"]
        for g in data:
            if g["name"].lower() == name.lower():
                return g["id"]
        if data:
            log(f"Kategorie '{name}' nicht exakt gefunden, nehme '{data[0]['name']}'")
            return data[0]["id"]
        raise RuntimeError(f"Twitch-Kategorie '{name}' nicht gefunden")

    def top_streams(self, game_id: str, language: str, n: int) -> list[dict]:
        data = self.get("streams", game_id=game_id, language=language, type="live",
                        first=min(100, max(n, 20)))["data"]
        return sorted(data, key=lambda s: s["viewer_count"], reverse=True)[:n]

    def streams_for_logins(self, logins: list[str], game_id: str) -> list[dict]:
        data = self.get("streams", user_login=logins[:100], type="live", first=100)["data"]
        data = [s for s in data if s.get("game_id") == game_id]
        return sorted(data, key=lambda s: s["viewer_count"], reverse=True)

    def clips(self, broadcaster_id: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return self.get("clips", broadcaster_id=broadcaster_id, first=20,
                        started_at=start.strftime(fmt), ended_at=end.strftime(fmt))["data"]


def select_clips(tw: Twitch, state: dict) -> tuple[list[dict], list[dict]]:
    gid = CFG["game_id"] or tw.game_id(CFG["game_name"])
    if CFG["fixed_streamers"]:
        streams = tw.streams_for_logins(CFG["fixed_streamers"], gid)
    else:
        streams = tw.top_streams(gid, CFG["language"], CFG["top_streamers"])
    log("Live: " + (", ".join(f"{s['user_name']} ({s['viewer_count']})" for s in streams) or "keiner"))

    sent = {x["id"] for x in state["sent"]}
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(minutes=CFG["lookback_min"])

    candidates = []
    for s in streams:
        try:
            clips = tw.clips(s["user_id"], start, end)
        except Exception as e:  # ein Streamer darf den Lauf nicht kippen
            log(f"Clips von {s['user_name']} nicht abrufbar: {e}")
            continue
        ok = [c for c in clips
              if c["id"] not in sent
              and c.get("game_id") in (gid, "", None)
              and c["view_count"] >= CFG["min_views"]
              and float(c.get("duration") or 0) <= CFG["max_duration"]]
        ok.sort(key=lambda c: c["view_count"], reverse=True)
        for c in ok[:CFG["per_streamer"]]:
            c["_stream"] = s
            candidates.append(c)

    candidates.sort(key=lambda c: c["view_count"], reverse=True)
    return candidates[:CFG["clips_per_run"]], streams


# ---------------------------------------------------------------------------
# Download + Probe
# ---------------------------------------------------------------------------

def download_clip(url: str, out: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
         "-f", "best[ext=mp4]/best", "-o", str(out), url],
        check=True, timeout=300,
    )


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    j = json.loads(out)
    st = j["streams"][0]
    return {"w": int(st["width"]), "h": int(st["height"]),
            "dur": float(j["format"].get("duration") or 0)}


# ---------------------------------------------------------------------------
# Transkription (faster-whisper)
# ---------------------------------------------------------------------------

_MODEL = None
# Typische Whisper-Halluzinationen auf Deutsch (bei Musik/Stille)
HALLU = re.compile(r"untertitel|amara\.org|zdf|funk\b|copyright|swr \d|www\.|\.de\b", re.I)


def transcribe(path: Path) -> list[tuple[float, float, str]]:
    global _MODEL
    from faster_whisper import WhisperModel

    import numpy as np

    if _MODEL is None:
        log(f"Lade Whisper-Modell '{CFG['whisper_model']}' …")
        _MODEL = WhisperModel(CFG["whisper_model"], device="cpu", compute_type="int8")
    # Audio selbst per ffmpeg dekodieren (16 kHz mono) – robuster als PyAV
    pcm = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        check=True, capture_output=True, timeout=300,
    ).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    if audio.size == 0:
        return []
    segments, _ = _MODEL.transcribe(
        audio, language="de", word_timestamps=True, vad_filter=True,
        beam_size=5, condition_on_previous_text=False,
    )
    words = []
    for seg in segments:
        if HALLU.search(seg.text) or (seg.no_speech_prob > 0.7 and seg.avg_logprob < -0.8):
            continue
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append((float(w.start), float(w.end), t))
    return words


# ---------------------------------------------------------------------------
# Untertitel (ASS) – TikTok-Stil: 1–3 Wörter, aktives Wort gelb
# ---------------------------------------------------------------------------

def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_text(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_words(words, max_words=3, max_chars=18, max_gap=0.5):
    groups, cur = [], []
    for w in words:
        if cur:
            joined = " ".join(x[2] for x in cur + [w])
            if (len(cur) >= max_words or len(joined) > max_chars
                    or w[0] - cur[-1][1] > max_gap or cur[-1][2][-1] in ".!?"):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def clean_hook(clip_title: str, stream_title: str, name: str) -> str:
    t = clip_title or ""
    # Default-Clip-Titel = Stream-Titel -> taugt nicht als Hook
    norm = lambda x: re.sub(r"\W+", "", x.lower())
    if not t or norm(t) == norm(stream_title or "") or norm(t) in norm(stream_title or ""):
        t = ""
    t = re.sub(r"![\w-]+", "", t)                         # !commands
    t = re.sub(r"[#@][\w-]+", "", t)                      # Hashtags/Mentions
    t = re.sub(r"\s*[|/•].*$", "", t)                      # alles nach | / •
    t = re.sub(r"[^\w\s.,!?äöüÄÖÜß'\"-]", "", t)          # Emojis etc. raus (Schrift kann die nicht)
    t = re.sub(r"\s+", " ", t).strip(" -.,")
    if len(t) < 4:
        return f"{name} in GTA 6"
    return t[:70]


def build_ass(words, dur: float, layout: dict, hook: str, credit: str) -> str:
    vid_top, vid_bot = layout["top"], layout["bottom"]
    cap_y = min(vid_bot + 70, H - 330)
    hook_y = max(vid_top - 60, 260)

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT_NAME},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,7,3,8,60,60,0,1
Style: Hook,{FONT_NAME},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,3,18,0,2,90,90,0,1
Style: Credit,{FONT_NAME},40,&H30FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,0,2,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    end_all = ass_time(dur + 1)
    if hook:
        # schwarze Box hinter dem Hook (BorderStyle 3 nutzt OutlineColour als Boxfarbe)
        ev.append(f"Dialogue: 1,0:00:00.00,{end_all},Hook,,0,0,0,,{{\\an2\\pos({W // 2},{hook_y})}}{ass_text(hook.upper())}")
    if credit:
        ev.append(f"Dialogue: 1,0:00:00.00,{end_all},Credit,,0,0,0,,{{\\an2\\pos({W // 2},{H - 150})}}{ass_text(credit)}")

    groups = group_words(words)
    for gi, g in enumerate(groups):
        next_start = groups[gi + 1][0][0] if gi + 1 < len(groups) else dur
        g_end = min(max(g[-1][1] + 0.35, g[-1][1]), next_start) if next_start > g[-1][1] else g[-1][1]
        for wi, w in enumerate(g):
            start = w[0] if wi else g[0][0]
            end = g[wi + 1][0] if wi + 1 < len(g) else g_end
            if end <= start:
                end = start + 0.08
            parts = []
            for wj, x in enumerate(g):
                txt = ass_text(x[2].upper())
                parts.append("{\\c&H00E5FF&}" + txt + "{\\c&HFFFFFF&}" if wj == wi else txt)
            pop = "{\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}" if wi == 0 else ""
            ev.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,"
                      f"{{\\an8\\pos({W // 2},{cap_y})}}{pop}{' '.join(parts)}")
    return head + "\n".join(ev) + "\n"


# ---------------------------------------------------------------------------
# Render 9:16
# ---------------------------------------------------------------------------

def layout_for(src_w: int, src_h: int, zoom: float) -> dict:
    fg_w = int(round(W * max(zoom, 1.0) / 2) * 2)
    fg_h = int(round(fg_w * src_h / src_w / 2) * 2)
    fg_h = min(fg_h, H)
    top = (H - fg_h) // 2
    return {"fg_w": fg_w, "fg_h": fg_h, "top": top, "bottom": top + fg_h}


def ff_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def render(src: Path, ass: Path, out: Path, layout: dict) -> None:
    fg_w = layout["fg_w"]
    crop = f",crop={W}:ih" if fg_w > W else ""
    vf = (
        f"[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        f"boxblur=12:2,scale={W}:{H},eq=brightness=-0.12:saturation=1.2,setsar=1[bg];"
        f"[0:v]scale={fg_w}:-2{crop},setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"ass='{ff_path(ass)}':fontsdir='{ff_path(FONT_DIR)}',format=yuv420p[v]"
    )
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-maxrate", "6M", "-bufsize", "12M", "-profile:v", "high",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, timeout=900)


def make_vertical(src: Path, workdir: Path, hook: str, credit: str) -> tuple[Path, dict]:
    info = probe(src)
    lay = layout_for(info["w"], info["h"], CFG["zoom"])
    words = transcribe(src)
    log(f"  {len(words)} Wörter erkannt")
    ass = workdir / "subs.ass"
    ass.write_text(build_ass(words, info["dur"], lay, hook, credit), encoding="utf-8")
    out = workdir / "out.mp4"
    render(src, ass, out, lay)
    info["words"] = len(words)
    return out, info


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class Telegram:
    def __init__(self, token: str, chat_id: str | None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id or self._discover_chat()

    def _discover_chat(self) -> str:
        r = requests.get(self.base + "getUpdates", timeout=20).json()
        for u in reversed(r.get("result", [])):
            chat = (u.get("message") or {}).get("chat")
            if chat:
                log(f"TELEGRAM_CHAT_ID nicht gesetzt – nutze Chat {chat['id']} (bitte als Secret eintragen)")
                return str(chat["id"])
        raise RuntimeError("Keine Telegram-Chat-ID: Schreib dem Bot einmal 'hi' oder setze TELEGRAM_CHAT_ID")

    def message(self, text: str, parse_html: bool = False) -> None:
        data = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
        if parse_html:
            data["parse_mode"] = "HTML"
        r = requests.post(self.base + "sendMessage", data=data, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Telegram sendMessage {r.status_code}: {r.text[:200]}")

    def video(self, path: Path, caption_html: str, w: int, h: int, dur: float) -> None:
        with open(path, "rb") as f:
            r = requests.post(
                self.base + "sendVideo",
                data={"chat_id": self.chat_id, "caption": caption_html[:1024], "parse_mode": "HTML",
                      "supports_streaming": "true", "width": w, "height": h, "duration": int(dur)},
                files={"video": (path.name, f, "video/mp4")},
                timeout=600,
            )
        if not r.ok:
            raise RuntimeError(f"Telegram sendVideo {r.status_code}: {r.text[:200]}")


def post_text(clip: dict, hook: str) -> str:
    login = clip["_stream"]["user_login"]
    tag = "#" + re.sub(r"\W", "", login)
    return f"{hook} 😂\n\n🎥 Credit: twitch.tv/{login}\n\n{CFG['hashtags']} {tag}"


def info_caption(clip: dict, rank: int, total: int) -> str:
    s = clip["_stream"]
    e = html.escape
    return (f"<b>#{rank}/{total} · {e(s['user_name'])}</b>\n"
            f"👀 {clip['view_count']} Clip-Views · 🔴 {s['viewer_count']} Zuschauer live · "
            f"⏱ {round(float(clip.get('duration') or 0))}s\n"
            f"🔗 {e(clip['url'])}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry: bool) -> int:
    need = ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"] + ([] if dry else ["TELEGRAM_BOT_TOKEN"])
    missing = [k for k in need if not os.getenv(k)]
    if missing:
        log("Fehlende Umgebungsvariablen: " + ", ".join(missing))
        return 2

    tg = None if dry else Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.getenv("TELEGRAM_CHAT_ID"))
    try:
        tw = Twitch(os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"])
        state = load_state()
        picks, streams = select_clips(tw, state)
    except Exception as e:
        log(f"FEHLER: {e}")
        if tg:
            tg.message(f"⚠️ Clipbot: Twitch-Abfrage fehlgeschlagen\n{e}")
        return 1

    if not picks:
        log("Keine neuen Clips in diesem Zeitfenster.")
        if tg and CFG["notify_empty"]:
            tg.message(f"🕐 Keine neuen GTA-6-Clips (live: {len(streams)} Streams)")
        return 0

    for i, c in enumerate(picks, 1):
        log(f"#{i}: {c['_stream']['user_name']} – {c['title']!r} ({c['view_count']} Views) {c['url']}")
    if dry:
        return 0

    sent_ok = 0
    for i, c in enumerate(picks, 1):
        s = c["_stream"]
        hook = clean_hook(c.get("title", ""), s.get("title", ""), s["user_name"])
        try:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                src = td / "clip.mp4"
                log(f"[{i}/{len(picks)}] Lade {c['url']}")
                download_clip(c["url"], src)
                out, info = make_vertical(src, td, hook, f"twitch.tv/{s['user_login']}")
                size_mb = out.stat().st_size / 1e6
                log(f"  gerendert: {size_mb:.1f} MB")
                if size_mb > 49:
                    raise RuntimeError(f"Video zu groß für Telegram ({size_mb:.0f} MB)")
                tg.video(out, info_caption(c, i, len(picks)), W, H, info["dur"])
                tg.message("<pre>" + html.escape(post_text(c, hook)) + "</pre>", parse_html=True)
            state["sent"].append({"id": c["id"], "ts": int(time.time()), "by": s["user_login"]})
            save_state(state)
            sent_ok += 1
        except Exception as e:
            log(f"  FEHLER bei Clip {c['id']}: {e}")
            traceback.print_exc()
            try:
                tg.message(f"⚠️ Clip von {s['user_name']} übersprungen: {str(e)[:300]}\n{c['url']}")
            except Exception:
                pass
    log(f"Fertig: {sent_ok}/{len(picks)} Clips verschickt.")
    return 0 if sent_ok else 1


def local_test(path: Path) -> int:
    out_dir = ROOT / "test_output"
    out_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        out, info = make_vertical(path, Path(td), "Das ist ein Test-Hook", "twitch.tv/teststreamer")
        target = out_dir / (path.stem + "_916.mp4")
        target.write_bytes(out.read_bytes())
    log(f"Fertig: {target} ({info['words']} Wörter)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", type=Path, help="lokales Video zum Testen rendern")
    a = ap.parse_args()
    if a.test:
        sys.exit(local_test(a.test))
    sys.exit(run(a.dry_run or os.getenv("DRY_RUN") == "1"))
