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
import html
import json
import logging
import logging.handlers
import os
import queue
import random
import re
import socket
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

def ai_judge(streamer: str, words, chat_lines: list[str], st: dict, title: str) -> dict | None:
    if not C["anthropic_key"]:
        return None
    transcript = " ".join(w[2] for w in words)[:3000] or "(keine Sprache erkannt)"
    chat_txt = "\n".join(chat_lines[-60:])[:3000]
    prompt = f"""Du bewertest einen Twitch-Clip für einen deutschen GTA-6-Clip-Account auf TikTok/Instagram Reels.

Streamer: {streamer}
Stream-Titel: {title}
Chat-Aktivität im Moment: {st['factor']:.1f}x so viel wie normal, {st['hype']*100:.0f}% Hype-Nachrichten
Transkript des Clips (automatisch, kann Fehler haben):
{transcript}

Chat-Nachrichten rund um den Moment:
{chat_txt}

Antworte NUR mit JSON:
{{"score": <1-10, wie viral/unterhaltsam für Leute, die den Streamer nicht kennen>,
 "reason": "<max. 12 Wörter>",
 "hook": "<Text oben im Video, Deutsch, max. 7 Wörter, neugierig machend, KEINE Emojis>",
 "caption": "<Post-Caption auf Deutsch: 1-2 kurze Sätze mit 1-3 Emojis, dann eine Leerzeile, dann 5-7 Hashtags inkl. #gta6. KEIN Credit, KEIN Link>"}}"""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": C["anthropic_key"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": C["anthropic_model"], "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        j = json.loads(text[text.index("{"): text.rindex("}") + 1])
        j["score"] = int(j.get("score", 0))
        return j
    except Exception as e:
        log(f"  KI-Bewertung fehlgeschlagen: {e}")
        return None


def fallback_judge(streamer: str, login: str, words, st: dict) -> dict:
    score = min(10, round(2 + st["factor"] * 1.2 + st["hype"] * 3))
    # kurzen, prägnanten Satz aus dem Transkript als Hook nehmen
    hook = ""
    text = " ".join(w[2] for w in words)
    for sent in re.split(r"(?<=[.!?])\s+", text):
        s = sent.strip(" .")
        if 2 <= len(s.split()) <= 7 and ("!" in sent or "?" in sent):
            hook = s
            break
    if not hook:
        hook = f"{streamer} in GTA 6"
    caption = (f"{streamer} in GTA 6 😂🔥\n\n"
               f"{C['hashtags']} #{re.sub(r'[^0-9a-zA-Z_]', '', login)}")
    return {"score": score, "reason": f"Chat {st['factor']:.1f}x, {st['hype']*100:.0f}% Hype",
            "hook": hook, "caption": caption}


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
        self.last_clip: dict[str, float] = {}
        self.noclip: dict[str, float] = {}
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
        top = self.tw.top_streams(self.game_id, C["lang"], C["top_n"])
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

        def go():
            time.sleep(C["clip_delay_s"])
            try:
                clip_id = create_clip(self.ut, s["user_id"])
                log(f"  Clip angelegt: {clip_id}")
                self.jobs.put_nowait({"clip_id": clip_id, "stream": s, "stats": st,
                                      "chat": chat_lines, "t": time.time(), "force": force})
            except queue.Full:
                log("  Warteschlange voll – Moment verworfen")
            except Exception as e:
                if "403" in str(e):
                    self.noclip[login] = time.time() + 1800
                    log(f"  {s['user_name']} erlaubt keine Clips von uns – 30 min übersprungen")
                else:
                    log(f"  Clip erstellen fehlgeschlagen: {e}")
                    self.stats["errors"] += 1
        threading.Thread(target=go, daemon=True).start()

    # --- Worker: Download, Transkript, Bewertung, Render, Telegram ---------
    def worker(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                self.process(job)
            except Exception as e:
                log(f"  Verarbeitung fehlgeschlagen: {e}")
                self.stats["errors"] += 1

    def process(self, job: dict) -> None:
        s, st = job["stream"], job["stats"]
        login, name = s["user_login"], s["user_name"]
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "clip.mp4"
            clip_url = fetch_clip(job["clip_id"], src)
            words = cb.transcribe(src)
            verdict = ai_judge(name, words, job["chat"], st, s.get("title", "")) \
                or fallback_judge(name, login, words, st)
            log(f"  Bewertung {verdict['score']}/10 – {verdict.get('reason', '')}")
            if verdict["score"] < C["min_score"] and not job.get("force"):
                self.stats["rejected"] += 1
                return
            now = time.time()
            while self.sent_times and self.sent_times[0] < now - 3600:
                self.sent_times.popleft()
            if len(self.sent_times) >= C["max_per_hour"]:
                log("  Stundenlimit erreicht – übersprungen")
                return
            hook = re.sub(r"[^\w\s.,!?äöüÄÖÜß'\"-]", "", verdict.get("hook", "")).strip()[:70] \
                or f"{name} in GTA 6"
            out, info = cb.make_vertical(src, td, hook, f"twitch.tv/{login}", words=words)
            info_cap = (f"<b>⚡ {html.escape(name)}</b> · {verdict['score']}/10\n"
                        f"💬 Chat {st['factor']:.1f}x · {st['hype']*100:.0f}% Hype · "
                        f"🔴 {s['viewer_count']} live\n"
                        f"⏱ {round(info['dur'])}s · fertig nach {round(time.time() - job['t'] + C['clip_delay_s'])}s\n"
                        f"🔗 {html.escape(clip_url)}")
            self.tg.video(out, info_cap, cb.W, cb.H, info["dur"])
            self.tg.message("<pre>" + html.escape(verdict["caption"]) + "</pre>", parse_html=True)
            self.sent_times.append(time.time())
            self.stats["sent"] += 1
            log(f"  ✅ an Telegram geschickt ({time.time() - job['t'] + C['clip_delay_s']:.0f}s nach dem Moment)")

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
            lines = [f"{'⏸ Pausiert' if self.paused else '▶️ Läuft'} · Kategorie: {C['game']}"]
            for login, s in self.streams.items():
                st = spike_stats(self.chat, login, now)
                rate = f"{st['rate']:.1f}/s (normal {st['base']:.1f}/s)" if st else "lernt noch…"
                lock = " – 🚫 Clips gesperrt" if self.noclip.get(login, 0) > now else ""
                lines.append(f"• {s['user_name']} – {s['viewer_count']} Zuschauer – Chat {rate}{lock}")
            if not self.streams:
                lines.append("Gerade keine deutschen GTA-VI-Streams live.")
            lines.append(f"Seit Start: {self.stats['spikes']} Momente, {self.stats['sent']} geschickt, "
                         f"{self.stats['rejected']} aussortiert, {self.stats['errors']} Fehler")
            self.tg.message("\n".join(lines))
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
            self.tg.message("Befehle: /status · /pause · /resume · /test <streamer>")

    # --- Hauptschleife ------------------------------------------------------
    def run(self) -> None:
        log(f"Live-Clipper startet – {C['game']}, Top {C['top_n']} ({C['lang']}), "
            f"KI-Check: {'an' if C['anthropic_key'] else 'aus'}")
        self.ut.get()  # ggf. Twitch-Freigabe einholen
        self.chat.start()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.commands, daemon=True).start()
        try:
            self.tg.message(f"🟢 Live-Clipper gestartet ({C['game']}, Top {C['top_n']}). /status für Details.")
        except Exception as e:
            log(f"Telegram: {e}")
        watch = {p: p.stat().st_mtime for p in [*ROOT.glob("*.py"), ROOT / ".env"] if p.exists()}
        last_refresh = 0.0
        while True:
            now = time.time()
            if now - last_refresh > C["refresh_s"]:
                try:
                    self.refresh_streams()
                except Exception as e:
                    log(f"Stream-Abfrage: {e}")
                last_refresh = now
                # Code/Config geändert -> sauber beenden, launchd startet neu
                for p, m in watch.items():
                    if p.exists() and p.stat().st_mtime != m:
                        log(f"{p.name} geändert – Neustart")
                        os._exit(3)
            if not self.paused:
                for login in list(self.streams):
                    if now - self.last_clip.get(login, 0) < C["cooldown_s"] or self.noclip.get(login, 0) > now:
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
