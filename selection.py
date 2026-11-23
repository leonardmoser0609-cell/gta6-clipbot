"""Top-10-Auswahl: Kandidaten sammeln, Kosten mitschreiben, Listen-Zeitpunkte und Posting-Plan.

Dateien (alle unter state/):
  candidates/<clip_id>.json   gesammelte Kandidaten (Rohclip in library/<clip_id>.mp4)
  digest.json                 die zuletzt verschickte Top-10-Liste
  schedule.json               geplante Posts (/push)
  digest_slots.json           bereits erledigte Listen-Zeitpunkte
  costs.json                  KI-Kosten pro Tag in USD
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
LIB = STATE / "library"
CANDS = STATE / "candidates"
DIGEST_FILE = STATE / "digest.json"
SCHEDULE_FILE = STATE / "schedule.json"
SLOTS_FILE = STATE / "digest_slots.json"
COSTS_FILE = STATE / "costs.json"
INSTANT_FILE = STATE / "instant.json"
_lock = threading.RLock()

# USD pro Million Tokens (Eingabe, Ausgabe)
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Kosten
# ---------------------------------------------------------------------------

def add_cost(model: str, usage: dict) -> float:
    pi, po = PRICES.get(model, (4.0, 20.0))
    usd = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)) / 1e6 * pi \
        + usage.get("cache_read_input_tokens", 0) / 1e6 * pi * 0.1 \
        + usage.get("output_tokens", 0) / 1e6 * po
    day = dt.date.today().isoformat()
    with _lock:
        c = _read(COSTS_FILE, {})
        c[day] = round(c.get(day, 0.0) + usd, 5)
        _write(COSTS_FILE, c)
    return usd


def cost_today() -> float:
    return float(_read(COSTS_FILE, {}).get(dt.date.today().isoformat(), 0.0))


def cost_since(day: str) -> float:
    return float(sum(v for k, v in _read(COSTS_FILE, {}).items() if k >= day))


# ---------------------------------------------------------------------------
# Kandidaten
# ---------------------------------------------------------------------------

def save_candidate(c: dict) -> None:
    with _lock:
        _write(CANDS / f"{c['clip_id']}.json", c)


def candidates(status: str | None = "new") -> list[dict]:
    out = []
    for p in sorted(CANDS.glob("*.json")):
        c = _read(p, None)
        if c and (status is None or c.get("status") == status):
            out.append(c)
    return out


def set_status(clip_ids, status: str) -> None:
    with _lock:
        for cid in clip_ids:
            p = CANDS / f"{cid}.json"
            c = _read(p, None)
            if c:
                c["status"] = status
                _write(p, c)


def cleanup(keep: set[str], max_age_h: float = 48) -> int:
    """Alte Kandidaten samt Videos löschen (außer geplanten/aktueller Liste). Erledigte ohne Platz in der
    Liste verlieren ihren Rohclip sofort."""
    n = 0
    now = time.time()
    for c in candidates(None):
        cid = c["clip_id"]
        if cid in keep:
            continue
        old = now - c.get("created", now) > max_age_h * 3600
        if old or c.get("status") == "done":
            for f in (LIB / f"{cid}.mp4", LIB / f"{cid}_final.mp4"):
                if f.exists():
                    f.unlink(missing_ok=True)
                    n += 1
        if old:
            (CANDS / f"{cid}.json").unlink(missing_ok=True)
    return n


# ---------------------------------------------------------------------------
# Listen-Zeitpunkte (10, 15, 20, 23 Uhr)
# ---------------------------------------------------------------------------

def due_slot(hours: list[int], now: dt.datetime, lead_min: int = 15, grace_h: float = 2.5):
    """Liste fällig? Startet lead_min Minuten vor der vollen Stunde (Prüfen + Rendern dauert),
    holt verpasste Listen bis grace_h Stunden später nach (z. B. nach Ruhezustand)."""
    done = set(_read(SLOTS_FILE, []))
    for day_off in (0, -1):
        d = (now + dt.timedelta(days=day_off)).date()
        for h in hours:
            slot = dt.datetime.combine(d, dt.time(h))
            label = slot.strftime("%Y-%m-%d %H:%M")
            if label in done:
                continue
            if slot - dt.timedelta(minutes=lead_min) <= now <= slot + dt.timedelta(hours=grace_h):
                return label, slot
    return None


def mark_slot(label: str) -> None:
    with _lock:
        done = _read(SLOTS_FILE, [])
        done = [x for x in done if x >= (dt.datetime.now() - dt.timedelta(days=3)).strftime("%Y-%m-%d")]
        done.append(label)
        _write(SLOTS_FILE, done)


def next_slot(hours: list[int], now: dt.datetime) -> dt.datetime:
    for day_off in (0, 1):
        d = (now + dt.timedelta(days=day_off)).date()
        for h in sorted(hours):
            s = dt.datetime.combine(d, dt.time(h))
            if s > now:
                return s
    return now


# ---------------------------------------------------------------------------
# Liste und Posting-Plan
# ---------------------------------------------------------------------------

def save_digest(d: dict) -> None:
    _write(DIGEST_FILE, d)


def load_digest() -> dict | None:
    return _read(DIGEST_FILE, None)


def load_schedule() -> list[dict]:
    return _read(SCHEDULE_FILE, [])


def save_schedule(s: list[dict]) -> None:
    with _lock:
        cutoff = time.time() - 3 * 86400  # alte Einträge nicht ewig mitschleppen
        _write(SCHEDULE_FILE, [e for e in s if e.get("status") == "geplant" or e.get("due", 0) > cutoff])


def plan_times(n: int, start: dt.datetime, gap_min: int = 90, night=(1, 9)) -> list[dt.datetime]:
    """Erster Post sofort, dann alle gap_min Minuten. Was nachts (1–9 Uhr) läge, geht um 9 Uhr raus."""
    out = []
    t = start
    for i in range(n):
        if i:
            t = t + dt.timedelta(minutes=gap_min)
        if i and night[0] <= t.hour < night[1]:  # der erste geht immer sofort raus
            t = t.replace(hour=night[1], minute=0, second=0, microsecond=0)
        if out and t <= out[-1]:
            t = out[-1] + dt.timedelta(minutes=gap_min)
        out.append(t)
    return out


def load_instant() -> dict:
    return _read(INSTANT_FILE, {"counter": 0, "items": {}})


def save_instant(d: dict) -> None:
    with _lock:
        cutoff = time.time() - 3 * 86400  # Sofort-Clips nach 3 Tagen vergessen (Nummern zählen weiter)
        d["items"] = {k: v for k, v in d.get("items", {}).items() if v.get("created", 0) > cutoff}
        _write(INSTANT_FILE, d)
