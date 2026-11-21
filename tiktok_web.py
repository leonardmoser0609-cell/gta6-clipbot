"""TikTok-Upload über die TikTok-Webseite (TikTok Studio) mit einem eigenen Browserprofil.

Einmalig einloggen:  Doppelklick auf „TikTok-Login.command“
                     (oder: .venv/bin/python tiktok_web.py login)
Danach lädt der Bot Clips selbst über https://www.tiktok.com/tiktokstudio/upload hoch.

Bewusst ohne Tricks gegen Bot-Erkennung: Zeigt TikTok eine Sicherheitsabfrage
oder fehlt der Login, bricht der Upload ab und der Bot sagt per Telegram Bescheid.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "state" / "tiktok_profile"
DEBUG = ROOT / "logs" / "tiktok"
UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=webapp"
_LOCK = threading.Lock()
_LAST_LINK = ""  # zuletzt gemeldeter Video-Link (nicht doppelt melden)

CAPTCHA = ("#captcha-verify-container, .captcha_verify_container, .captcha-verify-container, "
           "iframe[src*='captcha'], div[class*='captcha_verify']")
POST_NAMES = re.compile(r"^\s*(Posten|Post|Veröffentlichen|Publish)\s*$", re.I)
CONFIRM_NAMES = re.compile(r"^\s*(Jetzt (posten|veröffentlichen)|Post now|Publish now|"
                           r"Trotzdem (posten|veröffentlichen)|Post anyway|Publish anyway)\s*$", re.I)
DISMISS_NAMES = re.compile(r"^\s*(Optionale Cookies ablehnen|Decline optional cookies|Verstanden|Got it|OK|Last)\s*$",
                           re.I)
TUTORIAL_BTN = (".tutorial-tooltip button, .react-joyride__tooltip button[data-action='primary'], "
                "button[data-test-id='button-primary']")
DONE_TEXT = re.compile(r"(Video wurde veröffentlicht|wurde gepostet|Video veröffentlicht|has been published|"
                       r"Your video (is being|has been) (uploaded|posted)|Video wird veröffentlicht)", re.I)


CLICK_DELAY = 2.0  # Sekunden Pause vor jedem Klick


class NeedsHuman(RuntimeError):
    """Login fehlt oder TikTok verlangt eine Sicherheitsabfrage – das muss Leo selbst erledigen."""


class ProfileBusy(RuntimeError):
    """Das TikTok-Browserfenster (z. B. vom Login) ist noch offen."""


def _profile_busy() -> bool:
    import os
    lock = PROFILE / "SingletonLock"
    try:
        target = os.readlink(lock)  # "<rechnername>-<pid>"
    except OSError:
        return False
    try:
        os.kill(int(target.rsplit("-", 1)[1]), 0)
        return True
    except (ValueError, IndexError, ProcessLookupError):
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# Installation (Playwright + Chromium)
# ---------------------------------------------------------------------------

def _uv() -> str | None:
    home = Path.home()
    return shutil.which("uv") or next((str(p) for p in (home / ".local/bin/uv", Path("/opt/homebrew/bin/uv"),
                                                        home / ".cargo/bin/uv") if p.exists()), None)


def ensure_installed(verbose: bool = False) -> None:
    try:
        import importlib
        importlib.import_module("playwright")
    except ImportError:
        uv = _uv()
        if not uv:
            raise RuntimeError("Playwright fehlt und uv wurde nicht gefunden")
        if verbose:
            print("Installiere Playwright …", flush=True)
        subprocess.run([uv, "pip", "install", "--python", sys.executable, "playwright"], check=True, timeout=600)
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    if not any(cache.glob("chromium-*")):
        if verbose:
            print("Lade den Browser (Chromium, ca. 150 MB) – dauert ein paar Minuten …", flush=True)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, timeout=1800,
                       capture_output=not verbose)


def ready() -> bool:
    """Profil mit TikTok-Login vorhanden (Markierung vom Login-Skript oder Sitzungs-Cookie im Profil)."""
    if (PROFILE / ".logged_in").exists():
        return True
    db = PROFILE / "Default" / "Cookies"
    if not db.exists():
        return False
    try:
        import shutil as _sh
        import sqlite3
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:  # Kopie lesen, falls der Browser die Datei gerade offen hat
            cp = Path(td) / "c.db"
            _sh.copy(db, cp)
            n = sqlite3.connect(cp).execute(
                "select count(*) from cookies where host_key like '%tiktok.com' and name='sessionid'").fetchone()[0]
        if n:
            (PROFILE / ".logged_in").write_text(time.strftime("%Y-%m-%d %H:%M"))
        return bool(n)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def _launch(p, headless: bool = False):
    PROFILE.mkdir(parents=True, exist_ok=True)
    if headless:  # unsichtbar im Hintergrund: voller Chromium im „new headless“-Modus, kein Fenster, kein Fokus-Klau
        return p.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chromium", locale="de-DE",
            viewport={"width": 1280, "height": 860})
    return p.chromium.launch_persistent_context(
        str(PROFILE), headless=False, locale="de-DE", viewport={"width": 1280, "height": 860},
        args=["--window-position=60,60", "--window-size=1300,960"])


def _logged_in(ctx) -> bool:
    return any(c.get("name") == "sessionid" and c.get("value") for c in ctx.cookies("https://www.tiktok.com"))


def _dump(page, tag: str) -> Path | None:
    DEBUG.mkdir(parents=True, exist_ok=True)
    for old in sorted(DEBUG.glob("*"))[:-40]:  # nur die letzten Fehlerbilder behalten
        try:
            old.unlink()
        except Exception:
            pass
    base = DEBUG / f"{time.strftime('%m%d-%H%M%S')}_{tag}"
    try:
        page.screenshot(path=str(base.with_suffix(".png")))
    except Exception:
        pass
    try:
        base.with_suffix(".html").write_text(page.content()[:3_000_000])
    except Exception:
        pass
    return base.with_suffix(".png")


def _captcha(page) -> bool:
    try:
        return page.locator(CAPTCHA).first.is_visible(timeout=300)
    except Exception:
        return False


def _dismiss(page) -> None:
    """Cookie-Banner (optionale ablehnen) und Einführungs-Hinweise („Verstanden“) wegklicken."""
    for _ in range(4):
        clicked = False
        try:
            for b in page.locator(TUTORIAL_BTN).all()[:2]:
                if b.is_visible():
                    time.sleep(CLICK_DELAY)
                    b.click(timeout=3000)
                    clicked = True
                    time.sleep(0.8)
                    break
        except Exception:
            pass
        if not clicked:
            try:
                for b in page.get_by_role("button", name=DISMISS_NAMES).all()[:3]:
                    if b.is_visible():
                        time.sleep(CLICK_DELAY)
                        b.click(timeout=3000)
                        clicked = True
                        time.sleep(0.8)
                        break
            except Exception:
                pass
        if not clicked:
            return


def _click(page, loc, tries: int = 4) -> None:
    """Klicken; liegt ein Hinweis-Overlay darüber, erst wegklicken und nochmal versuchen."""
    for i in range(tries):
        try:
            time.sleep(CLICK_DELAY)
            loc.click(timeout=8000)
            return
        except Exception:
            if i == tries - 1:
                raise
            _dismiss(page)
            page.keyboard.press("Escape")
            time.sleep(1)


def login(timeout_s: int = 900) -> bool:
    ensure_installed(verbose=True)
    from playwright.sync_api import sync_playwright
    with _LOCK, sync_playwright() as p:
        ctx = _launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if not _logged_in(ctx):
            page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
            print("\n👉 Im geöffneten Fenster bei TikTok einloggen (das Konto, auf dem die Clips landen sollen).",
                  flush=True)
        t0 = time.time()
        while time.time() - t0 < timeout_s and not _logged_in(ctx):
            time.sleep(2)
        if not _logged_in(ctx):
            ctx.close()
            print("❌ Kein Login erkannt (Zeit abgelaufen).", flush=True)
            return False
        (PROFILE / ".logged_in").write_text(time.strftime("%Y-%m-%d %H:%M"))
        try:
            page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        print("✅ Eingeloggt.", flush=True)
        print("Falls TikTok noch etwas fragt (Sicherheitsabfrage, Hinweise), im Fenster erledigen.\n"
              "Danach das Browserfenster schließen – erst dann kann der Bot posten.", flush=True)
        t0 = time.time()
        while time.time() - t0 < 600:  # Fenster offen lassen, bis Leo es schließt (max. 10 min)
            try:
                if not ctx.pages or all(pg.is_closed() for pg in ctx.pages):
                    break
            except Exception:
                break
            time.sleep(1)
        try:
            ctx.close()
        except Exception:
            pass
        (PROFILE / ".logged_in").write_text(time.strftime("%Y-%m-%d %H:%M"))
        print("✅ Fertig – der Bot postet ab jetzt auf TikTok.", flush=True)
        return True


def _post_button(page):
    b = page.locator("button[data-e2e='post_video_button']")
    if b.count():
        return b.first
    return page.get_by_role("button", name=POST_NAMES).last


def _enabled(btn) -> bool:
    try:
        return (btn.is_enabled() and btn.get_attribute("data-disabled") != "true"
                and btn.get_attribute("aria-disabled") != "true")
    except Exception:
        return False


def _type_caption(page, caption: str) -> None:
    editor = page.locator("div[contenteditable='true']").first
    editor.wait_for(state="visible", timeout=120_000)
    _dismiss(page)
    _click(page, editor)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")
    time.sleep(0.5)
    lines = caption.strip().split("\n")
    for i, line in enumerate(lines):
        for j, word in enumerate(line.split(" ")):
            if j:
                page.keyboard.type(" ")
            page.keyboard.type(word, delay=25)
            if word.startswith("#"):  # Hashtag-Vorschläge schließen, ohne einen auszuwählen
                time.sleep(1.2)
                page.keyboard.press("Escape")
        if i < len(lines) - 1:
            page.keyboard.press("Enter")
    time.sleep(1)


def post(video: Path, caption: str, log=print, headless: bool = False) -> str:
    """Postet im Hintergrund; scheitert das unsichtbare Hochladen VOR dem Veröffentlichen, einmal mit Fenster."""
    try:
        return _post(video, caption, log, headless)
    except (NeedsHuman, ProfileBusy):
        raise
    except Exception as e:
        if not headless or getattr(e, "published_clicked", True):
            raise
        log(f"  TikTok im Hintergrund fehlgeschlagen ({str(e)[:80]}) – nochmal mit Fenster")
        return _post(video, caption, log, False)


def _post(video: Path, caption: str, log=print, headless: bool = False) -> str:
    clicked = False
    if _profile_busy():
        raise ProfileBusy("das TikTok-Browserfenster ist noch offen")
    from playwright.sync_api import sync_playwright
    with _LOCK, sync_playwright() as p:
        ctx = _launch(p, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if not _logged_in(ctx):
                raise NeedsHuman("TikTok-Login fehlt oder ist abgelaufen")
            page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=90_000)
            time.sleep(3)
            if "login" in page.url:
                raise NeedsHuman("TikTok-Login fehlt oder ist abgelaufen")
            if _captcha(page):
                raise NeedsHuman("TikTok verlangt eine Sicherheitsabfrage")
            _dismiss(page)
            page.locator("input[type='file']").first.set_input_files(str(video), timeout=60_000)
            log("  TikTok: Video wird hochgeladen …")
            btn = None
            t0 = time.time()
            while time.time() - t0 < 300:  # warten, bis TikTok das Video verarbeitet hat
                if _captcha(page):
                    raise NeedsHuman("TikTok verlangt eine Sicherheitsabfrage")
                _dismiss(page)
                btn = _post_button(page)
                if btn.count() and _enabled(btn):
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Upload nicht fertig geworden (Posten-Knopf blieb gesperrt)")
            _dismiss(page)
            _type_caption(page, caption)
            time.sleep(2)
            _dismiss(page)
            btn = _post_button(page)
            btn.scroll_into_view_if_needed()
            clicked = True
            _click(page, btn)
            t0 = time.time()
            while time.time() - t0 < 120:  # ggf. Rückfrage „Jetzt posten“ bestätigen, dann auf Erfolg warten
                if _captcha(page):
                    raise NeedsHuman("TikTok verlangt eine Sicherheitsabfrage")
                try:
                    c = page.get_by_role("button", name=CONFIRM_NAMES)
                    if c.count() and c.first.is_visible():
                        time.sleep(CLICK_DELAY)
                        c.first.click()
                except Exception:
                    pass
                if "/tiktokstudio/content" in page.url or "/tiktokstudio/upload" not in page.url:
                    break
                try:
                    if page.get_by_text(DONE_TEXT).first.is_visible(timeout=300):
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError("keine Bestätigung von TikTok nach dem Posten")
            global _LAST_LINK
            link = ""
            for _ in range(4):  # neues Video taucht in der Liste manchmal erst nach ein paar Sekunden auf
                time.sleep(5)
                try:
                    a = page.locator("a[href*='/video/']").first
                    href = (a.get_attribute("href") or "") if a.count() else ""
                    if href.startswith("/"):
                        href = "https://www.tiktok.com" + href
                except Exception:
                    href = ""
                if href and href != _LAST_LINK:
                    link = href
                    break
                try:
                    page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
            if link:
                _LAST_LINK = link
            return link or "gepostet (Link in TikTok Studio)"
        except Exception as e:
            shot = _dump(page, "fehler")
            log(f"  TikTok: Bildschirmfoto vom Fehler in logs/tiktok/{shot.name if shot else '-'}")
            try:
                e.published_clicked = clicked
            except Exception:
                pass
            raise
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        sys.exit(0 if login() else 1)
    if len(sys.argv) > 3 and sys.argv[1] == "post":
        print(post(Path(sys.argv[2]), sys.argv[3]))
        sys.exit(0)
    print("Aufruf: tiktok_web.py login | post <video.mp4> <caption>")
