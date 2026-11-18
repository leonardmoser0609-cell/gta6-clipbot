#!/usr/bin/env python3
"""
GTA 6 Live-Clipper
==================
Beobachtet dauerhaft die Top-5 deutschen Live-Streams in der Twitch-Kategorie
GTA VI, liest deren Chat mit und erkennt Hype-Momente (Chat explodiert,
KEKW/LUL/WTF-Spam). In dem Moment erstellt der Bot selbst einen Twitch-Clip,
transkribiert ihn, lässt ihn (optional) von Claude bewerten, rendert ihn 9:16
mit Untertiteln und schickt ihn samt Caption per Telegram.

Start:  python livebot.py
Konfiguration: .env im selben Ordner (siehe README).
Telegram-Befehle: /status  /pause  /resume  /test <streamer>
"""
from __future__ import annotations

import collections
import datetime as dt
import html
import json
import logging
import logging.handlers
import os
import queue
import random
import shutil
import re
import socket
import subprocess
import ssl
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
LOGS = ROOT / "logs"
STATE.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# .env laden (vor dem Import von clipbot, damit CFG die Werte sieht)
# ---------------------------------------------------------------------------

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(ROOT / ".env")
import clipbot as cb  # noqa: E402
import tiktok_web  # noqa: E402

def _f(name, default):
    try:
        return type(default)(os.getenv(name) or default)
    except ValueError:
        return default


C = {
    "game": os.getenv("GAME_NAME") or "Grand Theft Auto VI",
    "lang": os.getenv("STREAM_LANGUAGE") or "de",
    "top_n": _f("TOP_N", 5),
    "refresh_s": _f("REFRESH_S", 60),
    "window_s": _f("SPIKE_WINDOW_S", 8.0),       # Fenster für "jetzt"
    "baseline_s": _f("BASELINE_S", 300.0),       # Vergleichszeitraum
    "spike_factor": _f("SPIKE_FACTOR", 3.0),     # so viel mehr Chat als normal
    "min_msgs": _f("SPIKE_MIN_MSGS", 15),        # Mindestnachrichten im Fenster
    "hype_min": _f("HYPE_MIN", 0.25),            # Anteil Hype-Nachrichten
    "cooldown_s": _f("COOLDOWN_S", 150.0),       # pro Streamer
    "clip_delay_s": _f("CLIP_DELAY_S", 4.0),     # kurz warten, damit die Reaktion drin ist
    "clip_duration": _f("CLIP_DURATION", 40),
    "max_per_hour": _f("MAX_PER_HOUR", 20),
    "min_score": _f("MIN_SCORE", 6),
    "anthropic_key": os.getenv("ANTHROPIC_API_KEY") or "",
    "anthropic_model": os.getenv("ANTHROPIC_MODEL") or "claude-haiku-5-5",
    "hashtags": os.getenv("HASHTAGS") or "#gta6 #gtavi #gta #twitchclips #gaming #fyp",
    "yt_min_score": _f("YT_MIN_SCORE", 6),
    "yt_max_per_day": _f("YT_MAX_PER_DAY", 60),
    "ig_min_score": _f("IG_MIN_SCORE", 6),
    "ig_max_seconds": _f("IG_MAX_SECONDS", 49.5),
    "ig_max_per_day": _f("IG_MAX_PER_DAY", 40),
    "tt_min_score": _f("TT_MIN_SCORE", 7),
    "tt_max_per_day": _f("TT_MAX_PER_DAY", 12),
    "tt_gap_min": _f("TT_GAP_MIN", 30),
    "tt_headless": os.getenv("TT_HEADLESS", "0") == "1",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log_ = logging.getLogger("livebot")
log_.setLevel(logging.INFO)
_fh = logging.handlers.RotatingFileHandler(LOGS / "livebot.log", maxBytes=5_000_000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
log_.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
log_.addHandler(_sh)
log = log_.info
cb.log = lambda m: log_.info(m)  # clipbot-Logs mit ins File


# ---------------------------------------------------------------------------
# Twitch: App-Token (Lesen) + User-Token (Clips erstellen, Device-Flow)
# ---------------------------------------------------------------------------

CID = os.environ.get("TWITCH_CLIENT_ID", "")
SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
USER_TOKEN_FILE = STATE / "twitch_user.json"


class UserToken:
    def __init__(self, tg: cb.Telegram):
        self.tg = tg
        self.data = json.loads(USER_TOKEN_FILE.read_text()) if USER_TOKEN_FILE.exists() else None
        self.lock = threading.Lock()

    def _save(self, j: dict) -> None:
        self.data = {"access_token": j["access_token"], "refresh_token": j.get("refresh_token"),
                     "expires_at": time.time() + int(j.get("expires_in", 3600))}
        USER_TOKEN_FILE.write_text(json.dumps(self.data))
        try:
            os.chmod(USER_TOKEN_FILE, 0o600)
        except OSError:
            pass

    def device_flow(self) -> None:
        r = requests.post("https://id.twitch.tv/oauth2/device",
                          data={"client_id": CID, "scopes": "clips:edit"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        msg = (f"🔑 Twitch-Freigabe nötig, damit der Bot clippen darf:\n{j['verification_uri']}\n"
               f"(Code: {j['user_code']}) – öffnen und „Autorisieren“ klicken.")
        log(msg.replace("\n", " "))
        try:
            self.tg.message(msg)
        except Exception as e:
            log(f"Telegram: {e}")
        deadline = time.time() + int(j.get("expires_in", 1800))
        while time.time() < deadline:
            time.sleep(int(j.get("interval", 5)))
            t = requests.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": CID, "client_secret": SECRET, "device_code": j["device_code"],
                "scopes": "clips:edit", "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
                timeout=20)
            if t.ok:
                self._save(t.json())
                log("Twitch-Freigabe erhalten ✅")
                try:
                    self.tg.message("✅ Twitch-Freigabe erhalten – der Live-Clipper läuft.")
                except Exception:
                    pass
                return
            if "authorization_pending" not in t.text and "slow_down" not in t.text:
                raise RuntimeError(f"Device-Flow: {t.status_code} {t.text[:200]}")
        raise RuntimeError("Twitch-Freigabe nicht rechtzeitig erteilt")

    def refresh(self) -> None:
        r = requests.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": CID, "client_secret": SECRET, "grant_type": "refresh_token",
            "refresh_token": self.data["refresh_token"]}, timeout=20)
        if not r.ok:
            log(f"Token-Refresh fehlgeschlagen ({r.status_code}) – neue Freigabe nötig")
            USER_TOKEN_FILE.unlink(missing_ok=True)
            self.data = None
            self.device_flow()
            return
        self._save(r.json())

    def get(self) -> str:
        with self.lock:
            if not self.data:
                self.device_flow()
            elif time.time() > self.data["expires_at"] - 300:
                self.refresh()
            return self.data["access_token"]

    def force_refresh(self) -> None:
        with self.lock:
            self.refresh()


def create_clip(ut: UserToken, broadcaster_id: str, title: str | None = None) -> str:
    def call(with_duration: bool) -> requests.Response:
        params = {"broadcaster_id": broadcaster_id}
        if with_duration and C["clip_duration"]:
            params["duration"] = C["clip_duration"]
        if title:
            params["title"] = title[:100]
        return requests.post("https://api.twitch.tv/helix/clips", params=params, timeout=20,
                             headers={"Client-Id": CID, "Authorization": f"Bearer {ut.get()}"})

    r = call(True)
    if r.status_code == 401:
        ut.force_refresh()
        r = call(True)
    if r.status_code == 400 and C["clip_duration"]:
        log(f"  Create Clip mit duration abgelehnt ({r.text[:120]}) – ohne duration")
        r = call(False)
    if not r.ok:
        raise RuntimeError(f"Create Clip {r.status_code}: {r.text[:200]}")
    return r.json()["data"][0]["id"]


def fetch_clip(clip_id: str, dest: Path, timeout: float = 180) -> str:
    """Wartet, bis Twitch den neuen Clip verarbeitet hat, und lädt ihn herunter."""
    url = f"https://clips.twitch.tv/{clip_id}"
    t0 = time.time()
    time.sleep(15)
    last = ""
    while time.time() - t0 < timeout:
        try:
            cb.download_clip(url, dest)
            if dest.exists() and dest.stat().st_size > 50_000:
                log(f"  Clip geladen nach {time.time() - t0:.0f}s")
                return url
        except Exception as e:
            last = str(e)[-160:]
        time.sleep(6)
    raise RuntimeError(f"Clip nicht abrufbar ({last})")


# ---------------------------------------------------------------------------
# Chat (anonymes IRC, nur lesen)
# ---------------------------------------------------------------------------

HYPE_RE = re.compile(
    r"(KEKW|OMEGALUL|LULW?|ICANT|LMAO|\bLOL\b|\bXD\b|HAHA|HEHE|WTF|POGGERS|\bPOG\w*|PogU|monka\w*|"
    r"NOO+|\bO{3,}|ALTER|DIGGA|DIGGER|CLIP|EZ\b|Aware|Clueless|OMG|😂|🤣|💀|😭|\?\?+|!!+)", re.I)
SOLO_RE = re.compile(r"^\s*(W+|L+|\+2|-2|F|\?|!)\s*$", re.I)


def is_hype(text: str) -> bool:
    return bool(HYPE_RE.search(text) or SOLO_RE.match(text))


class Chat(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.wanted: set[str] = set()
        self.joined: set[str] = set()
        self.join_time: dict[str, float] = {}
        self.msgs: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def set_channels(self, logins) -> None:
        with self.lock:
            self.wanted = {x.lower() for x in logins}

    def snapshot(self, ch: str) -> list[tuple[float, str, str]]:
        with self.lock:
            return list(self.msgs.get(ch, ()))

    def run(self) -> None:
        while True:
            try:
                self._session()
            except Exception as e:
                log(f"Chat-Verbindung: {e} – neuer Versuch in 5 s")
            with self.lock:
                self.joined.clear()
            time.sleep(5)

    def _session(self) -> None:
        raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=15)
        s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
        s.settimeout(1.0)
        nick = f"justinfan{random.randint(10000, 99999)}"
        s.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\n".encode())
        buf, last_rx = b"", time.time()
        while True:
            self._sync_joins(s)
            try:
                data = s.recv(65536)
                if not data:
                    raise ConnectionError("Server hat Verbindung beendet")
                buf += data
                last_rx = time.time()
            except socket.timeout:
                if time.time() - last_rx > 360:
                    raise ConnectionError("keine Daten seit 6 min")
                continue
            *lines, buf = buf.split(b"\r\n")
            now = time.time()
            for ln in lines:
                line = ln.decode("utf-8", "replace")
                if line.startswith("PING"):
                    s.sendall(("PONG" + line[4:] + "\r\n").encode())
                    continue
                m = re.match(r":(\w+)!\S+ PRIVMSG #(\w+) :(.*)", line)
                if m:
                    user, ch, text = m.groups()
                    with self.lock:
                        dq = self.msgs[ch]
                        dq.append((now, user, text))
                        while dq and dq[0][0] < now - C["baseline_s"] - 60:
                            dq.popleft()

    def _sync_joins(self, s) -> None:
        with self.lock:
            to_join = self.wanted - self.joined
            to_part = self.joined - self.wanted
        for ch in to_join:
            s.sendall(f"JOIN #{ch}\r\n".encode())
            with self.lock:
                self.joined.add(ch)
                self.join_time[ch] = time.time()
                self.msgs[ch].clear()
            log(f"Chat: {ch} beigetreten")
        for ch in to_part:
            s.sendall(f"PART #{ch}\r\n".encode())
            with self.lock:
                self.joined.discard(ch)
                self.msgs.pop(ch, None)
            log(f"Chat: {ch} verlassen")


def spike_stats(chat: Chat, ch: str, now: float) -> dict | None:
    msgs = chat.snapshot(ch)
    joined = chat.join_time.get(ch, now)
    if now - joined < 90:  # erst Grundrauschen lernen
        return None
    w = C["window_s"]
    recent = [m for m in msgs if m[0] >= now - w]
    base = [m for m in msgs if now - C["baseline_s"] <= m[0] < now - w]
    span = max(min(C["baseline_s"], now - joined) - w, 30)
    rate_now = len(recent) / w
    rate_base = max(len(base) / span, 0.2)
    hype = sum(1 for m in recent if is_hype(m[2])) / max(len(recent), 1)
    uniq = len({m[1] for m in recent})
    return {"n": len(recent), "rate": rate_now, "base": rate_base, "factor": rate_now / rate_base,
            "hype": hype, "uniq": uniq}


def triggered(st: dict) -> bool:
    if st["n"] < C["min_msgs"] or st["uniq"] < 8:
        return False
    if st["factor"] >= C["spike_factor"] and st["hype"] >= C["hype_min"]:
        return True
    return st["factor"] >= C["spike_factor"] * 0.66 and st["hype"] >= 0.6


# ---------------------------------------------------------------------------
# KI-Bewertung + Caption (Claude), mit regelbasiertem Fallback
# ---------------------------------------------------------------------------

def extract_frames(src: Path, dur: float, n: int = 8) -> list[tuple[float, str]]:
    """n Standbilder gleichmäßig über den Clip verteilt, als base64-JPEG."""
    import base64
    import overlay
    frames = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(n):
            t = dur * (i + 0.5) / n
            out = Path(td) / f"f{i}.jpg"
            subprocess.run([overlay.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
                            "-i", str(src), "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "4", str(out)],
                           check=False, timeout=30)
            if out.exists():
                frames.append((t, base64.b64encode(out.read_bytes()).decode()))
    return frames


def ai_judge(streamer: str, words, chat_lines: list[str], st: dict, title: str,
             src: Path | None = None, dur: float = 0, viewer_clip: bool = False) -> dict | None:
    if not C["anthropic_key"]:
        return None
    transcript = " ".join(f"[{w[0]:.0f}s] {w[2]}" if i % 12 == 0 else w[2]
                          for i, w in enumerate(words))[:3500] or "(keine Sprache erkannt)"
    chat_txt = "\n".join(chat_lines[-60:])[:3000] or "(nicht verfügbar)"
    content: list[dict] = []
    if src is not None and dur:
        for t, b64 in extract_frames(src, dur):
            content.append({"type": "text", "text": f"Standbild bei {t:.0f}s:"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    other_game_rule = ("Nur wenn auf den Bildern eindeutig ein ANDERES Spiel (z. B. Minecraft) oder gar kein Spiel "
                       "zu sehen ist, gib score 0.\n") if viewer_clip else ""
    content.append({"type": "text", "text": f"""Oben siehst du Standbilder aus einem {dur:.0f}-Sekunden-Twitch-Clip von {streamer} (GTA 6, deutscher Stream).

Stream-Titel: {title}
Chat-Reaktion im Moment: {st['factor']:.1f}x so viel wie normal, {st['hype']*100:.0f}% Hype-Nachrichten
Transkript (automatisch, kann Fehler haben, [Xs] = Zeitmarke):
{transcript}

Chat-Nachrichten kurz vor dem Clip:
{chat_txt}

Aufgabe: Finde heraus, WAS in dem Clip konkret passiert (Handlung im Spiel und/oder Reaktion des Streamers) – aus Bildern, Transkript und Chat zusammen. Erfinde nichts, was nicht erkennbar ist.
{other_game_rule}Geht es im Moment um Sub-Gifts, Subs, Spenden oder Danksagungen dafür (statt um das Spiel), gib score 0.

Antworte IMMER und NUR mit JSON, auch wenn der Clip unbrauchbar ist:
{{"what": "<1 Satz: was passiert, sachlich>",
 "score": <1-10: wie unterhaltsam/viral für Leute, die den Streamer nicht kennen>,
 "hook": "<Titel mitten im Video, Deutsch, normale Groß-/Kleinschreibung, 3-8 Wörter, beschreibt konkret das Geschehen mit Streamernamen und endet mit 'in GTA 6', z. B. 'Trymacs überfährt Rentner in GTA 6 😂😭' oder 'Monte wird von der Polizei verhaftet in GTA 6 💀'. Am Ende 1-2 passende Emojis, wenn der Moment witzig ist (sonst keine). Keine Hashtags, kein Clickbait, das nicht stimmt>",
 "caption": "<Post-Caption auf Deutsch: GENAU EIN kurzer, lockerer Satz zum Geschehen (höchstens 10 Wörter) mit 1-2 passenden Emojis, dann eine Leerzeile, dann 5-7 Hashtags inkl. #gta6 und dem Streamernamen. KEIN Link, KEIN Credit>"}}"""})
    text = ""
    for attempt in range(2):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": C["anthropic_key"], "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": C["anthropic_model"], "max_tokens": 4000,  # Platz für das Nachdenken des Modells
                      "messages": [{"role": "user", "content": content}]},
                timeout=90)
            r.raise_for_status()
        except Exception as e:  # KI nicht erreichbar -> regelbasierter Fallback
            log(f"  KI-Bewertung fehlgeschlagen: {str(e)[:200]}")
            return None
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        u = r.json().get("usage", {})
        log(f"  KI ({C['anthropic_model']}): {u.get('input_tokens', 0)} Tokens rein, {u.get('output_tokens', 0)} raus")
        try:
            j = json.loads(text[text.index("{"): text.rindex("}") + 1])
            j["score"] = int(j.get("score", 0))
            j["reason"] = j.get("what", "")
            return j
        except Exception:
            log(f"  KI ohne gültiges JSON (Versuch {attempt + 1}): {text[:200]!r}")
    # KI hat geantwortet, aber nichts Brauchbares (oft: kein GTA zu sehen) -> lieber verwerfen
    return {"score": 0, "reason": f"KI-Antwort unbrauchbar: {text[:120]}", "hook": "", "caption": ""}


SUBGIFT_RE = re.compile(r"sub[\s\-]*gift|gift[\s\-]*sub|gifted|giftsub|subs?\s*(verschenkt|geschenkt)|"
                        r"geschenkte[nr]?\s*subs?", re.I)


def subgift_moment(words, chat_lines: list[str], verdict: dict) -> str:
    """Fehlalarm durch Sub-Gifts? Gibt den Grund zurück (leer = kein Sub-Gift-Moment)."""
    said = " ".join(w[2] for w in words or [])
    if SUBGIFT_RE.search(said):
        return "im Gesprochenen"
    if SUBGIFT_RE.search(" ".join(str(verdict.get(k, "")) for k in ("what", "reason", "hook", "caption"))):
        return "laut KI-Beschreibung"
    hits = sum(1 for line in chat_lines if re.search(r"gift", line, re.I))
    if chat_lines and hits >= max(3, 0.15 * len(chat_lines)):
        return f"im Chat ({hits} Gift-Nachrichten)"
    return ""


def fallback_judge(streamer: str, login: str, words, st: dict) -> dict:
    score = min(10, round(2 + st["factor"] * 1.2 + st["hype"] * 3))
    hook = f"{streamer} in GTA 6 😂"
    caption = (f"{streamer} in GTA 6 😂🔥\n\n"
               f"{C['hashtags']} #{re.sub(r'[^0-9a-zA-Z_]', '', login)}")
    return {"score": score, "reason": f"Chat {st['factor']:.1f}x, {st['hype']*100:.0f}% Hype",
            "hook": hook, "caption": caption}


# ---------------------------------------------------------------------------
# YouTube-Upload (Kanal „GTA 6 Highlights DE“)
# ---------------------------------------------------------------------------

_YT = {"access": None, "exp": 0.0}


def yt_enabled() -> bool:
    return all(os.getenv(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))


def yt_access() -> str:
    if _YT["access"] and time.time() < _YT["exp"] - 120:
        return _YT["access"]
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": os.environ["YT_CLIENT_ID"].strip(), "client_secret": os.environ["YT_CLIENT_SECRET"].strip(),
        "refresh_token": os.environ["YT_REFRESH_TOKEN"].strip(), "grant_type": "refresh_token"})
    if not r.ok:
        raise RuntimeError(f"YouTube-Token: {r.status_code} {r.text[:200]}")
    _YT["access"], _YT["exp"] = r.json()["access_token"], time.time() + int(r.json().get("expires_in", 3600))
    return _YT["access"]


def youtube_upload(path: Path, title: str, description: str, tags: list[str]) -> str:
    meta = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:15],
                        "categoryId": "20", "defaultLanguage": "de", "defaultAudioLanguage": "de"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": False, "embeddable": True}}
    size = path.stat().st_size
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers={"Authorization": f"Bearer {yt_access()}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
        data=json.dumps(meta), timeout=60)
    if not init.ok:
        raise RuntimeError(f"YouTube-Start {init.status_code}: {init.text[:250]}")
    with open(path, "rb") as f:
        r = requests.put(init.headers["Location"], data=f, timeout=900,
                         headers={"Authorization": f"Bearer {yt_access()}", "Content-Type": "video/mp4"})
    if not r.ok:
        raise RuntimeError(f"YouTube-Upload {r.status_code}: {r.text[:250]}")
    return f"https://youtube.com/shorts/{r.json()['id']}"


# ---------------------------------------------------------------------------
# Instagram-Reels (Konto über die verknüpfte Facebook-Seite)
# ---------------------------------------------------------------------------

GRAPH = "https://graph.facebook.com/v25.0"


def ig_enabled() -> bool:
    return bool(os.getenv("META_PAGE_TOKEN") and os.getenv("IG_USER_ID"))


def ig_trim(src: Path, max_s: float) -> Path:
    """Instagram-API nimmt bei diesem Konto nur ~50 s: vorne kürzen, das Ende (Höhepunkt) bleibt."""
    import overlay
    dur = cb.probe(src)["dur"]
    if dur <= max_s:
        return src
    out = src.with_name(src.stem + "_ig.mp4")
    subprocess.run([overlay.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{dur - max_s:.2f}",
                    "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "4.5M",
                    "-bufsize", "9M", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)],
                   check=True, timeout=300)
    return out


def _fb_hidden_video_url(path: Path, token: str) -> tuple[str, str]:
    """Lädt das Video unveröffentlicht auf die Facebook-Seite und liefert eine öffentliche MP4-Adresse."""
    page = os.environ["META_PAGE_ID"].strip()
    with open(path, "rb") as f:
        r = requests.post(f"https://graph-video.facebook.com/v25.0/{page}/videos", timeout=900,
                          data={"published": "false", "access_token": token},
                          files={"source": (path.name, f, "video/mp4")})
    if not r.ok:
        raise RuntimeError(f"Facebook-Hilfsupload {r.status_code}: {r.text[:200]}")
    vid = r.json()["id"]
    for _ in range(40):
        q = requests.get(f"{GRAPH}/{vid}", params={"fields": "source", "access_token": token}, timeout=30)
        if q.ok and q.json().get("source"):
            return q.json()["source"], vid
        time.sleep(8)
    raise RuntimeError("Facebook-Hilfsupload: keine Video-Adresse")


def instagram_reel(path: Path, caption: str) -> str:
    ig, token = os.environ["IG_USER_ID"].strip(), os.environ["META_PAGE_TOKEN"].strip()
    base = {"media_type": "REELS", "caption": caption[:2200], "share_to_feed": "true", "access_token": token}
    fb_vid = None
    try:
        for attempt in range(3):  # Instagram meldet manchmal kurzzeitig ProcessingFailedError
            r = requests.post(f"{GRAPH}/{ig}/media", timeout=120, data={**base, "upload_type": "resumable"})
            if not r.ok:
                raise RuntimeError(f"Instagram-Container {r.status_code}: {r.text[:250]}")
            cid = r.json()["id"]
            uri = r.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/{GRAPH.rsplit('/', 1)[1]}/{cid}"
            with open(path, "rb") as f:
                u = requests.post(uri, data=f, timeout=900, headers={
                    "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(path.stat().st_size),
                    "Content-Type": "application/octet-stream"})
            if u.ok:
                break
            if attempt == 2:
                raise RuntimeError(f"Instagram-Upload {u.status_code}: {u.text[:250]}")
            log(f"  Instagram-Upload {u.status_code}, neuer Versuch in {30 * (attempt + 1)}s")
            time.sleep(30 * (attempt + 1))
    except Exception as e:  # Ersatzweg über ein unsichtbares Facebook-Video
        log(f"  Instagram-Direktupload fehlgeschlagen ({str(e)[:120]}) – Ersatzweg über Facebook")
        src_url, fb_vid = _fb_hidden_video_url(path, token)
        r = requests.post(f"{GRAPH}/{ig}/media", timeout=120, data={**base, "video_url": src_url})
        if not r.ok:
            raise RuntimeError(f"Instagram-Container (Link) {r.status_code}: {r.text[:250]}")
        cid = r.json()["id"]
    try:
        for _ in range(110):
            st = requests.get(f"{GRAPH}/{cid}", params={"fields": "status_code,status", "access_token": token},
                              timeout=30).json()
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram-Verarbeitung: {str(st)[:250]}")
            time.sleep(8)
        else:
            raise RuntimeError("Instagram-Verarbeitung dauert zu lange")
        p = requests.post(f"{GRAPH}/{ig}/media_publish", data={"creation_id": cid, "access_token": token},
                          timeout=60)
        if not p.ok:
            raise RuntimeError(f"Instagram-Veröffentlichen {p.status_code}: {p.text[:250]}")
        mid = p.json()["id"]
        link = requests.get(f"{GRAPH}/{mid}", params={"fields": "permalink", "access_token": token}, timeout=30)
        return link.json().get("permalink", mid) if link.ok else mid
    finally:
        if fb_vid:  # Hilfsvideo wieder entfernen
            requests.delete(f"{GRAPH}/{fb_vid}", params={"access_token": token}, timeout=30)


# ---------------------------------------------------------------------------
# Verarbeitung eines Moments
# ---------------------------------------------------------------------------

class Bot:
    def __init__(self):
        self.tg = cb.Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.getenv("TELEGRAM_CHAT_ID"))
        self.tw = cb.Twitch(CID, SECRET)
        self.ut = UserToken(self.tg)
        self.chat = Chat()
        self.jobs: queue.Queue = queue.Queue(maxsize=6)
        self.streams: dict[str, dict] = {}
        self.top_list: list[dict] = []  # Top 10 live (für /status)
        self.refresh_now = False
        try:  # per /erweitere gesetzte Anzahl überlebt Neustarts
            C["top_n"] = int(json.loads((STATE / "top_n.json").read_text())["top_n"])
        except Exception:
            pass
        self.last_clip: dict[str, float] = {}
        self.noclip: dict[str, float] = {}
        self.used_clips: set[str] = set()
        self.yt_on = yt_enabled()
        self.yt_block_until = 0.0  # YouTube-Uploadsperre (Kanal-Tageslimit)
        self.ig_on = ig_enabled()
        self.ig_times: collections.deque = collections.deque()
        self.tt_on = os.getenv("TIKTOK", "1") != "0"  # wirkt erst nach dem TikTok-Login
        self.tt_times: collections.deque = collections.deque()
        self.tt_fail = 0
        self.pub_lock = threading.Lock()
        self.yt_times: collections.deque = collections.deque()
        self.busy = False
        self.pending_file = STATE / "pending.json"
        self.plock = threading.Lock()
        try:
            self.pending: dict[str, dict] = json.loads(self.pending_file.read_text())
        except Exception:
            self.pending = {}
        self.sent_times: collections.deque = collections.deque()
        self.paused = False
        self.stats = collections.Counter()
        self.game_id = os.getenv("GAME_ID") or self.tw.game_id(C["game"])
        self.app_token_born = time.time()

    # --- Streams aktualisieren -------------------------------------------
    def refresh_streams(self) -> None:
        if time.time() - self.app_token_born > 3 * 24 * 3600:  # App-Token erneuern
            self.tw = cb.Twitch(CID, SECRET)
            self.app_token_born = time.time()
        n = int(C["top_n"])
        self.top_list = self.tw.top_streams(self.game_id, C["lang"], max(10, n))
        top = self.top_list[:n]
        new = {s["user_login"].lower(): s for s in top}
        if set(new) != set(self.streams):
            log("Top-Streams: " + (", ".join(f"{s['user_name']} ({s['viewer_count']})" for s in top) or "keine"))
        self.streams = new
        self.chat.set_channels(new.keys())

    # --- Moment erkannt -> sofort clippen ----------------------------------
    def on_spike(self, login: str, st: dict, force: bool = False) -> None:
        s = self.streams.get(login)
        if not s:
            return
        self.last_clip[login] = time.time()
        chat_lines = [f"{u}: {t}" for (_, u, t) in self.chat.snapshot(login)[-80:]]
        log(f"⚡ Spike bei {s['user_name']}: {st['factor']:.1f}x ({st['n']} Nachr./{C['window_s']:.0f}s, "
            f"{st['hype']*100:.0f}% Hype)")
        self.stats["spikes"] += 1

        t_spike = time.time()

        def go():
            time.sleep(C["clip_delay_s"])
            if self.noclip.get(login, 0) > time.time():
                self.viewer_clip(s, st, chat_lines, force, t_spike)
                return
            try:
                clip_id = create_clip(self.ut, s["user_id"])
                log(f"  Clip angelegt: {clip_id}")
                self.used_clips.add(clip_id)
                self.enqueue({"clip_id": clip_id, "stream": s, "stats": st,
                              "chat": chat_lines, "t": t_spike, "force": force})
            except queue.Full:
                log("  Warteschlange voll – Moment verworfen")
            except Exception as e:
                if "403" in str(e):
                    self.noclip[login] = time.time() + 600
                    log(f"  {s['user_name']} erlaubt keine Clips von uns – 10 min Plan B (Zuschauer-Clips)")
                    self.viewer_clip(s, st, chat_lines, force, t_spike)
                else:
                    log(f"  Clip erstellen fehlgeschlagen: {e}")
                    self.stats["errors"] += 1
        threading.Thread(target=go, daemon=True).start()

    # --- Plan B: Zuschauer-Clip abgreifen (Kanal erlaubt keine Clips von uns) ---
    def viewer_clip(self, s: dict, st: dict, chat_lines, force: bool, t_spike: float) -> None:
        name = s["user_name"]
        log(f"  Plan B: warte auf Zuschauer-Clip bei {name}")
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = dt.datetime.fromtimestamp(t_spike - 120, tz=dt.timezone.utc).strftime(fmt)
        time.sleep(60)
        deadline = t_spike + 1200  # Twitch gibt Zuschauer-Clips oft erst nach 5–20 min heraus
        while time.time() < deadline:
            try:
                end = dt.datetime.now(dt.timezone.utc).strftime(fmt)
                clips = self.tw.get("clips", broadcaster_id=s["user_id"], started_at=start,
                                    ended_at=end, first=20)["data"]
            except Exception as e:
                log(f"  Plan B Abfrage: {e}")
                clips = []
            cands = []
            for c in clips:
                try:
                    created = dt.datetime.strptime(c["created_at"], fmt).replace(tzinfo=dt.timezone.utc).timestamp()
                except Exception:
                    continue
                if str(c.get("game_id")) != str(self.game_id):  # z. B. Clip aus altem Minecraft-VOD
                    continue
                if c["id"] not in self.used_clips and t_spike - 60 <= created <= t_spike + 180:
                    cands.append(c)
            if cands:
                best = max(cands, key=lambda c: (c["view_count"], c["created_at"]))
                self.used_clips.add(best["id"])
                log(f"  Plan B: Zuschauer-Clip von {best.get('creator_name')} gefunden ({best['view_count']} Views)")
                try:
                    self.enqueue({"clip_id": best["id"], "stream": s, "stats": st, "chat": chat_lines,
                                  "t": t_spike, "force": force, "viewer_clip": True})
                except queue.Full:
                    log("  Warteschlange voll – Moment verworfen")
                return
            time.sleep(60)
        log(f"  Plan B: kein Zuschauer-Clip bei {name} gefunden")

    # --- Worker: Download, Transkript, Bewertung, Render, Telegram ---------
    def _save_pending(self) -> None:
        with self.plock:
            self.pending_file.write_text(json.dumps(self.pending))

    def enqueue(self, job: dict) -> None:
        self.jobs.put_nowait(job)
        with self.plock:
            self.pending[job["clip_id"]] = job
        self._save_pending()

    def worker(self) -> None:
        while True:
            job = self.jobs.get()
            self.busy = True
            try:
                self.process(job)
            except Exception as e:
                log(f"  Verarbeitung fehlgeschlagen: {e}")
                self.stats["errors"] += 1
            finally:
                with self.plock:
                    self.pending.pop(job["clip_id"], None)
                self._save_pending()
                self.busy = self.jobs.qsize() > 0

    def process(self, job: dict) -> None:
        s, st = job["stream"], job["stats"]
        login, name = s["user_login"], s["user_name"]
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "clip.mp4"
            clip_url = fetch_clip(job["clip_id"], src)
            words = cb.transcribe(src)
            dur = cb.probe(src)["dur"]
            verdict = ai_judge(name, words, job["chat"], st, s.get("title", ""), src, dur,
                               viewer_clip=bool(job.get("viewer_clip"))) \
                or fallback_judge(name, login, words, st)
            log(f"  Bewertung {verdict['score']}/10 – {verdict.get('reason', '')}")
            if verdict["score"] < C["min_score"] and not job.get("force"):
                self.stats["rejected"] += 1
                return
            why = subgift_moment(words, job["chat"], verdict)
            if why and not job.get("force"):
                log(f"  Aussortiert: Sub-Gifts {why} – Fehlalarm, wird nicht gepostet")
                self.stats["rejected"] += 1
                return
            now = time.time()
            while self.sent_times and self.sent_times[0] < now - 3600:
                self.sent_times.popleft()
            if len(self.sent_times) >= C["max_per_hour"]:
                log("  Stundenlimit erreicht – übersprungen")
                return
            hook = re.sub(r"#\w+", "", verdict.get("hook", "")).replace("\n", " ").strip()[:90] \
                or f"{name} in GTA 6 😂"
            if not verdict.get("caption"):
                verdict["caption"] = fallback_judge(name, login, words, st)["caption"]
            out, info = cb.make_vertical(src, td, hook, words=words)
            info_cap = (f"<b>⚡ {html.escape(name)}</b> · {verdict['score']}/10"
                        f"{' · 👥 Zuschauer-Clip' if job.get('viewer_clip') else ''}\n"
                        f"📝 {html.escape(verdict.get('reason', ''))}\n"
                        f"💬 Chat {st['factor']:.1f}x · {st['hype']*100:.0f}% Hype · "
                        f"🔴 {s['viewer_count']} live\n"
                        f"⏱ {round(info['dur'])}s · fertig nach {round(time.time() - job['t'] + C['clip_delay_s'])}s\n"
                        f"🔗 {html.escape(clip_url)}")
            self.tg.video(out, info_cap, cb.W, cb.H, info["dur"])
            self.tg.message("<pre>" + html.escape(verdict["caption"]) + "</pre>", parse_html=True)
            self.sent_times.append(time.time())
            self.stats["sent"] += 1
            only = job.get("platforms")  # optional: nur bestimmte Plattformen (z. B. Test)
            def want(platform: str, min_score) -> bool:  # bei gezielt gewählten Plattformen zählt die Note nicht
                return (platform in only) if only else verdict["score"] >= min_score
            yt = self.yt_on and want("youtube", C["yt_min_score"])
            ig = self.ig_on and want("instagram", C["ig_min_score"])
            tt = self.tt_on and tiktok_web.ready() and want("tiktok", C["tt_min_score"])
            if yt or ig or tt:  # Uploads im Hintergrund, damit der nächste Clip nicht wartet
                outbox = STATE / "outbox"
                outbox.mkdir(exist_ok=True)
                keep = outbox / f"{job['clip_id']}.mp4"
                shutil.copy(out, keep)
                threading.Thread(target=self.publish, args=(keep, verdict, name, login, yt, ig, tt),
                                 daemon=True).start()
            log(f"  ✅ an Telegram geschickt ({time.time() - job['t'] + C['clip_delay_s']:.0f}s nach dem Moment)")

    def post_youtube(self, out: Path, verdict: dict, name: str, login: str) -> None:
        now = time.time()
        while self.yt_times and self.yt_times[0] < now - 86400:
            self.yt_times.popleft()
        if len(self.yt_times) >= C["yt_max_per_day"]:
            log("  YouTube: Tageslimit erreicht – nicht hochgeladen")
            return
        if now < self.yt_block_until:
            log(f"  YouTube: Upload-Limit des Kanals – nächster Versuch ab "
                f"{dt.datetime.fromtimestamp(self.yt_block_until).strftime('%H:%M')}")
            return
        hook = (verdict.get("hook") or f"{name} in GTA 6 😂").strip().rstrip(".")
        title = f"{hook} #gta6 #shorts"
        tags = ["gta 6", "gta6", "gta vi", "gta 6 deutsch", name, "twitch clips", "gaming", "gta 6 highlights"]
        try:
            url = youtube_upload(out, title, verdict.get("caption", ""), tags)
            self.yt_times.append(time.time())
            self.stats["youtube"] += 1
            log(f"  ▶️ YouTube: {url}")
            self.tg.message(f"▶️ Auf YouTube: {url}")
        except Exception as e:
            if "exceeded the number of videos" in str(e):  # YouTube-Limit pro Kanal und Tag
                self.yt_block_until = time.time() + 3 * 3600
                until = dt.datetime.fromtimestamp(self.yt_block_until).strftime("%H:%M")
                log(f"  YouTube: Upload-Limit des Kanals erreicht – Pause bis {until}")
                try:
                    self.tg.message(f"⏸ YouTube hat das Upload-Limit für heute erreicht – nächster Versuch ab {until}. "
                                    "Instagram und Telegram laufen weiter.")
                except Exception:
                    pass
                return
            log(f"  YouTube-Upload fehlgeschlagen: {str(e)[:300]}")
            try:
                self.tg.message(f"⚠️ YouTube-Upload fehlgeschlagen: {str(e)[:300]}")
            except Exception:
                pass

    def publish(self, path: Path, verdict: dict, name: str, login: str, yt: bool, ig: bool,
                tt: bool = False) -> None:
        with self.pub_lock:
            try:
                if yt:
                    self.post_youtube(path, verdict, name, login)
                if ig:
                    self.post_instagram(path, verdict)
                if tt:
                    self.post_tiktok(path, verdict)
            finally:
                for f in (path, path.with_name(path.stem + "_ig.mp4")):
                    f.unlink(missing_ok=True)

    def post_tiktok(self, out: Path, verdict: dict) -> None:
        now = time.time()
        while self.tt_times and self.tt_times[0] < now - 86400:
            self.tt_times.popleft()
        if len(self.tt_times) >= C["tt_max_per_day"]:
            log("  TikTok: Tageslimit erreicht – nicht gepostet")
            return
        if self.tt_times and now - self.tt_times[-1] < C["tt_gap_min"] * 60:
            log(f"  TikTok: letzter Post vor weniger als {C['tt_gap_min']:.0f} min – diesen ausgelassen")
            return
        try:
            url = tiktok_web.post(out, verdict.get("caption", ""), log=log, headless=C["tt_headless"])
            self.tt_times.append(time.time())
            self.tt_fail = 0
            self.stats["tiktok"] += 1
            log(f"  🎵 TikTok: {url}")
            self.tg.message(f"🎵 Auf TikTok: {url}")
        except tiktok_web.ProfileBusy as e:
            log(f"  TikTok übersprungen: {e}")
            if not getattr(self, "_tt_busy_told", False):
                self._tt_busy_told = True
                try:
                    self.tg.message("ℹ️ TikTok wartet: Bitte das TikTok-Browserfenster am Mac schließen, "
                                    "dann postet der Bot dort automatisch.")
                except Exception:
                    pass
        except tiktok_web.NeedsHuman as e:
            self.tt_on = False
            log(f"  TikTok pausiert: {e}")
            try:
                self.tg.message(f"⏸ TikTok pausiert: {e}.\nBitte am Mac „TikTok-Login.command“ doppelklicken und im "
                                "Fenster einloggen bzw. die Abfrage lösen. Danach /tt on schicken.")
            except Exception:
                pass
        except Exception as e:
            self.tt_fail += 1
            log(f"  TikTok-Upload fehlgeschlagen: {str(e)[:300]}")
            msg = f"⚠️ TikTok-Upload fehlgeschlagen: {str(e)[:250]}"
            if self.tt_fail >= 3:
                self.tt_on = False
                msg += "\n⏸ Nach 3 Fehlern in Folge pausiert – mit /tt on wieder einschalten."
            try:
                self.tg.message(msg)
            except Exception:
                pass

    def post_instagram(self, out: Path, verdict: dict) -> None:
        now = time.time()
        while self.ig_times and self.ig_times[0] < now - 86400:
            self.ig_times.popleft()
        if len(self.ig_times) >= C["ig_max_per_day"]:
            log("  Instagram: Tageslimit erreicht – nicht hochgeladen")
            return
        try:
            url = instagram_reel(ig_trim(out, C["ig_max_seconds"]), verdict.get("caption", ""))
            self.ig_times.append(time.time())
            self.stats["instagram"] += 1
            log(f"  📸 Instagram: {url}")
            self.tg.message(f"📸 Auf Instagram: {url}")
        except Exception as e:
            log(f"  Instagram-Upload fehlgeschlagen: {e}")
            try:
                self.tg.message(f"⚠️ Instagram-Upload fehlgeschlagen: {str(e)[:300]}")
            except Exception:
                pass

    # --- Telegram-Befehle ---------------------------------------------------
    def commands(self) -> None:
        offset = None
        base = self.tg.base
        while True:
            try:
                r = requests.get(base + "getUpdates", params={"timeout": 50, "offset": offset},
                                 timeout=60).json()
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != str(self.tg.chat_id):
                        continue
                    self.handle_cmd((msg.get("text") or "").strip())
            except Exception as e:
                log(f"Telegram-Befehle: {e}")
                time.sleep(5)

    def handle_cmd(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        if cmd == "/status":
            now = time.time()
            n = int(C["top_n"])
            lines = [f"{'⏸ Pausiert' if self.paused else '▶️ Läuft'} · {C['game']} · geclippt wird Top {n} "
                     f"(ändern: /erweitere 6)"]
            for i, s in enumerate(self.top_list[:max(10, n)], 1):
                login = s["user_login"].lower()
                viewers = f"{s['viewer_count']:,}".replace(",", ".")
                if login in self.streams:
                    st = spike_stats(self.chat, login, now)
                    rate = f"Chat {st['rate']:.1f}/s (normal {st['base']:.1f}/s)" if st else "lernt noch…"
                    lock = " · 👥 Plan B" if self.noclip.get(login, 0) > now else ""
                    lines.append(f"{i}. 👁 {s['user_name']} – {viewers} – {rate}{lock}")
                else:
                    lines.append(f"{i}. ▫️ {s['user_name']} – {viewers}")
            if not self.top_list:
                lines.append("Gerade keine deutschen GTA-VI-Streams live.")
            lines.append("")
            lines.append(f"YouTube-Autoupload: {'an' if self.yt_on else 'aus'} (ab {C['yt_min_score']}/10) · "
                         f"heute {len(self.yt_times)} hochgeladen")
            lines.append(f"Instagram-Autoupload: {'an' if self.ig_on else 'aus'} (ab {C['ig_min_score']}/10) · "
                         f"heute {len(self.ig_times)} hochgeladen")
            tt_state = ("an" if self.tt_on else "aus") if tiktok_web.ready() else "nicht eingeloggt"
            tt_lim = "" if C["tt_max_per_day"] >= 1000 else f", max. {C['tt_max_per_day']:.0f}/Tag"
            tt_gap = f", alle {C['tt_gap_min']:.0f} min" if C["tt_gap_min"] else ""
            lines.append(f"TikTok-Autoupload: {tt_state} (ab {C['tt_min_score']}/10{tt_lim}{tt_gap}) · "
                         f"heute {len(self.tt_times)} gepostet")
            lines.append(f"Seit Start: {self.stats['spikes']} Momente, {self.stats['sent']} geschickt, "
                         f"{self.stats['rejected']} aussortiert, {self.stats['errors']} Fehler")
            self.tg.message("\n".join(lines))
        elif cmd == "/yt":
            if not yt_enabled():
                self.tg.message("YouTube ist nicht verbunden.")
            else:
                self.yt_on = arg.strip().lower() not in ("off", "aus", "0")
                self.tg.message(f"YouTube-Autoupload {'an ▶️' if self.yt_on else 'aus ⏸'}")
        elif cmd == "/ig":
            if not ig_enabled():
                self.tg.message("Instagram ist nicht verbunden.")
            else:
                self.ig_on = arg.strip().lower() not in ("off", "aus", "0")
                self.tg.message(f"Instagram-Autoupload {'an 📸' if self.ig_on else 'aus ⏸'}")
        elif cmd in ("/erweitere", "/erweitern", "/top"):
            try:
                n = int(arg.strip())
                assert 1 <= n <= 20
            except Exception:
                self.tg.message(f"So: /erweitere 7 (Zahl 1–20). Gerade: Top {int(C['top_n'])}.")
                return
            C["top_n"] = n
            (STATE / "top_n.json").write_text(json.dumps({"top_n": n}))
            self.refresh_now = True
            names = ", ".join(s["user_name"] for s in self.top_list[:n]) or "–"
            self.tg.message(f"👁 Geclippt wird jetzt Top {n}: {names}\n"
                            "Neue Kanäle brauchen ca. 90 s, bis der Chat-Normalwert gelernt ist.")
            log(f"Top-N per Telegram auf {n} gesetzt")
        elif cmd == "/tt":
            if not tiktok_web.ready():
                self.tg.message("TikTok ist noch nicht eingeloggt – am Mac „TikTok-Login.command“ doppelklicken.")
            else:
                self.tt_on = arg.strip().lower() not in ("off", "aus", "0")
                self.tt_fail = 0
                self.tg.message(f"TikTok-Autoupload {'an 🎵' if self.tt_on else 'aus ⏸'}")
        elif cmd == "/clip":
            m = re.search(r"(?:clip/|clips\.twitch\.tv/)?([A-Za-z0-9_-]{10,})\s*(\w+)?\s*$", arg.strip())
            if not m:
                self.tg.message("So: /clip <Twitch-Clip-Link> [streamer]")
                return
            clip_id, login = m.group(1), (m.group(2) or "").lower()
            lm = re.search(r"twitch\.tv/(\w+)/clip/", arg)
            login = login or (lm.group(1).lower() if lm else "")
            try:
                c = self.tw.get("clips", id=clip_id)["data"]
                if c:
                    login = login or c[0]["broadcaster_name"].lower()
                u = self.tw.get("users", login=login)["data"][0] if login else None
                live = self.tw.get("streams", user_login=login)["data"] if login else []
                s = live[0] if live else {"user_id": u["id"], "user_login": u["login"],
                                          "user_name": u["display_name"], "viewer_count": 0, "title": ""}
                fake = {"n": 0, "rate": 0, "base": 1, "factor": 0, "hype": 0, "uniq": 0}
                self.enqueue({"clip_id": clip_id, "stream": s, "stats": fake, "chat": [], "t": time.time(),
                              "force": True})
                self.tg.message(f"🎬 Clip von {s['user_name']} eingereiht – kommt in 1–2 Minuten.")
            except Exception as e:
                self.tg.message(f"Clip nicht gefunden – bitte so: /clip <Link> <streamer> ({str(e)[:100]})")
        elif cmd == "/pause":
            self.paused = True
            self.tg.message("⏸ Pausiert. Mit /resume geht's weiter.")
        elif cmd == "/resume":
            self.paused = False
            self.tg.message("▶️ Läuft wieder.")
        elif cmd == "/test":
            login = arg.strip().lower() or next(iter(self.streams), "")
            if login in self.streams:
                self.tg.message(f"🎬 Test-Clip bei {self.streams[login]['user_name']} …")
                fake = {"n": 0, "rate": 0, "base": 1, "factor": 0, "hype": 0, "uniq": 0}
                self.on_spike(login, fake, force=True)
            else:
                self.tg.message("Streamer ist nicht unter den aktuellen Top-Streams. /status zeigt sie.")
        elif cmd in ("/start", "/help"):
            self.tg.message("Befehle: /status · /pause · /resume · /test <streamer> · /clip <Link> · /erweitere <Zahl> · /yt on|off · /ig on|off · /tt on|off")

    # --- Hauptschleife ------------------------------------------------------
    def run(self) -> None:
        log(f"Live-Clipper startet – {C['game']}, Top {C['top_n']} ({C['lang']}), "
            f"KI-Check: {'an' if C['anthropic_key'] else 'aus'}, YouTube: {'an' if yt_enabled() else 'aus'}, Instagram: {'an' if ig_enabled() else 'aus'}")
        self.ut.get()  # ggf. Twitch-Freigabe einholen
        import overlay
        threading.Thread(target=overlay._cv2, daemon=True).start()  # OpenCV ggf. vorab installieren
        self.chat.start()
        for job in list(self.pending.values()):
            if time.time() - job.get("t", 0) < 1800:
                log(f"Nehme angefangenen Clip wieder auf: {job['stream']['user_name']} {job['clip_id']}")
                self.jobs.put_nowait(job)
            else:
                self.pending.pop(job["clip_id"], None)
        self._save_pending()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.commands, daemon=True).start()
        last_start = STATE / "last_start"
        try:
            quiet = time.time() - float(last_start.read_text()) < 900  # Neustart nach Update: still
        except Exception:
            quiet = False
        last_start.write_text(str(time.time()))
        if not quiet:
            try:
                self.tg.message(f"🟢 Live-Clipper gestartet ({C['game']}, Top {C['top_n']}). /status für Details.")
            except Exception as e:
                log(f"Telegram: {e}")
        watch = {p: p.stat().st_mtime for p in [*ROOT.glob("*.py"), ROOT / ".env"] if p.exists()}
        last_refresh = 0.0
        while True:
            now = time.time()
            if now - last_refresh > C["refresh_s"] or self.refresh_now:
                self.refresh_now = False
                try:
                    self.refresh_streams()
                except Exception as e:
                    log(f"Stream-Abfrage: {e}")
                last_refresh = now
                # Code/Config geändert -> sauber beenden, launchd startet neu
                changed = [p.name for p, m in watch.items() if p.exists() and p.stat().st_mtime != m]
                if changed:
                    if not self.busy and not self.pub_lock.locked():
                        log(f"{', '.join(changed)} geändert – Neustart")
                        os._exit(3)
                    log(f"{', '.join(changed)} geändert – Neustart nach dem aktuellen Clip")
            inject = STATE / "inject.json"
            if inject.exists():
                try:
                    for job in json.loads(inject.read_text()):
                        job.setdefault("t", time.time())
                        self.enqueue(job)
                        log(f"Manueller Clip eingereiht: {job['stream'].get('user_name')} {job['clip_id']}")
                except Exception as e:
                    log(f"inject.json: {e}")
                inject.unlink(missing_ok=True)
            if not self.paused:
                for login in list(self.streams):
                    if now - self.last_clip.get(login, 0) < C["cooldown_s"]:
                        continue
                    st = spike_stats(self.chat, login, now)
                    if st and triggered(st):
                        self.on_spike(login, st)
            time.sleep(1.0)


if __name__ == "__main__":
    missing = [k for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN") if not os.getenv(k)]
    if missing:
        log("Fehlende Einstellungen in .env: " + ", ".join(missing))
        sys.exit(2)
    Bot().run()
