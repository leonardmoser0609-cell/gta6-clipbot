"""Instagram-Reels über instagram.com mit eigenem Browserprofil (für ein privates Konto ohne Business-API).

Einmalig einloggen:  Doppelklick auf „Instagram-Login.command“
Danach postet der Bot Reels selbst: Erstellen → Video → Original-Format → Weiter → Caption → Teilen.

Wie bei TikTok bewusst ohne Tricks gegen Bot-Erkennung: Fragt Instagram nach einer Bestätigung
(Code, „verdächtige Anmeldung“) oder fehlt der Login, bricht der Upload ab und der Bot sagt Bescheid.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

import tiktok_web as tw  # gemeinsame Helfer (Installation, Profil-Sperre)

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "state" / "insta_profile"
DEBUG = ROOT / "logs" / "instagram"
HOME_URL = "https://www.instagram.com/"
CLICK_DELAY = 2.0
_LOCK = threading.Lock()

CREATE = re.compile(r"^\s*(Neuer Beitrag|Erstellen|New post|Create)\s*$", re.I)
POST_ITEM = re.compile(r"^\s*(Beitrag|Post)\s*$", re.I)
NEXT = re.compile(r"^\s*(Weiter|Next)\s*$", re.I)
SHARE = re.compile(r"^\s*(Teilen|Share)\s*$", re.I)
OK_BTN = re.compile(r"^\s*(OK)\s*$", re.I)
CROP = re.compile(r"(Zuschneiden|Select crop|Crop)", re.I)
ORIGINAL = re.compile(r"^\s*(Original)\s*$", re.I)
DISMISS = re.compile(r"^\s*(Jetzt nicht|Not now|Optionale Cookies ablehnen|Decline optional cookies|"
                     r"Nur erforderliche Cookies erlauben|Only allow essential cookies)\s*$", re.I)
DONE = re.compile(r"(wurde geteilt|has been shared|Reel geteilt|Reel shared|Beitrag geteilt|post has been shared)", re.I)
CHALLENGE = re.compile(r"(challenge|checkpoint|suspended|accounts/login)", re.I)


class NeedsHuman(RuntimeError):
    """Login fehlt/abgelaufen oder Instagram verlangt eine Bestätigung – das muss Leo selbst machen."""


class ProfileBusy(RuntimeError):
    """Das Instagram-Browserfenster (z. B. vom Login) ist noch offen."""


def _busy() -> bool:
    import os
    try:
        target = os.readlink(PROFILE / "SingletonLock")
        os.kill(int(target.rsplit("-", 1)[1]), 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


def ready() -> bool:
    if (PROFILE / ".logged_in").exists():
        return True
    db = PROFILE / "Default" / "Cookies"
    if not db.exists():
        return False
    try:
        import shutil
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cp = Path(td) / "c.db"
            shutil.copy(db, cp)
            n = sqlite3.connect(cp).execute(
                "select count(*) from cookies where host_key like '%instagram.com' and name='sessionid'").fetchone()[0]
        if n:
            (PROFILE / ".logged_in").write_text(time.strftime("%Y-%m-%d %H:%M"))
        return bool(n)
    except Exception:
        return False


def _launch(p, headless: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    if headless:
        return p.chromium.launch_persistent_context(str(PROFILE), headless=True, channel="chromium",
                                                    locale="de-DE", viewport={"width": 1280, "height": 900})
    return p.chromium.launch_persistent_context(str(PROFILE), headless=False, locale="de-DE",
                                                viewport={"width": 1280, "height": 900},
                                                args=["--window-position=60,60", "--window-size=1300,1000"])


def _logged_in(ctx) -> bool:
    return any(c.get("name") == "sessionid" and c.get("value") for c in ctx.cookies("https://www.instagram.com"))


def _dump(page, tag: str) -> Path | None:
    DEBUG.mkdir(parents=True, exist_ok=True)
    for old in sorted(DEBUG.glob("*"))[:-40]:
        old.unlink(missing_ok=True)
    base = DEBUG / f"{time.strftime('%m%d-%H%M%S')}_{tag}"
    try:
        page.screenshot(path=str(base.with_suffix(".png")))
        base.with_suffix(".html").write_text(page.content()[:3_000_000])
    except Exception:
        pass
    return base.with_suffix(".png")


def _click(loc, tries: int = 3) -> None:
    for i in range(tries):
        try:
            time.sleep(CLICK_DELAY)
            loc.click(timeout=10_000)
            return
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5)


def _visible(loc) -> bool:
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _dismiss(page) -> None:
    for _ in range(3):
        b = page.get_by_role("button", name=DISMISS)
        if not _visible(b):
            return
        try:
            time.sleep(CLICK_DELAY)
            b.first.click(timeout=4000)
            time.sleep(1)
        except Exception:
            return


def _check(page) -> None:
    if CHALLENGE.search(page.url):
        raise NeedsHuman("Instagram will eine Bestätigung oder den Login – bitte Instagram-Login.command öffnen")


def _latest_reel(page) -> tuple[str, str]:
    """(Benutzername, Link zum neuesten Reel) über den Profil-Link in der Seitenleiste."""
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    time.sleep(3)
    prof = page.get_by_role("link", name=re.compile(r"^\s*(Profil|Profile)\s*$", re.I))
    href = prof.first.get_attribute("href") if prof.count() else ""
    if not href:  # Alternative: Link mit dem Profilbild in der Seitenleiste
        img = page.locator("a[href^='/'] img[alt*='Profilbild'], a[href^='/'] img[alt*='profile picture']")
        if img.count():
            href = img.first.locator("xpath=ancestor::a[1]").get_attribute("href") or ""
    user = (href or "").strip("/").split("/")[0]
    if not user or user in ("explore", "reels", "direct", "accounts"):
        _dump(page, "home")
        return "", ""
    page.goto(f"https://www.instagram.com/{user}/reels/", wait_until="domcontentloaded", timeout=60_000)
    a = page.locator("a[href*='/reel/']").first
    try:
        a.wait_for(state="attached", timeout=20_000)
        link = a.get_attribute("href") or ""
    except Exception:
        link = ""
    if link.startswith("/"):
        link = "https://www.instagram.com" + link
    return user, link


def whoami(log=print) -> tuple[str, str]:
    """Kontoname + neuestes Reel (mit Bildschirmfoto in logs/instagram) – zum Prüfen nach dem Login."""
    if _busy():
        raise ProfileBusy("das Instagram-Browserfenster ist noch offen")
    from playwright.sync_api import sync_playwright
    with _LOCK, sync_playwright() as p:
        ctx = _launch(p, True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            user, link = _latest_reel(page)
            if link:
                page.goto(link, wait_until="domcontentloaded", timeout=60_000)
                time.sleep(5)
                _dump(page, "reel")
            return user, link
        finally:
            ctx.close()


def login(timeout_s: int = 900) -> bool:
    tw.ensure_installed(verbose=True)
    from playwright.sync_api import sync_playwright
    with _LOCK, sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
        print("\n👉 Im geöffneten Fenster bei Instagram einloggen (das neue Konto für die Clips).", flush=True)
        t0 = time.time()
        while time.time() - t0 < timeout_s and not _logged_in(ctx):
            time.sleep(2)
        if not _logged_in(ctx):
            ctx.close()
            print("❌ Kein Login erkannt (Zeit abgelaufen).", flush=True)
            return False
        (PROFILE / ".logged_in").write_text(time.strftime("%Y-%m-%d %H:%M"))
        print("✅ Eingeloggt. Falls Instagram noch etwas fragt (Login speichern, Mitteilungen), im Fenster "
              "erledigen und das Fenster danach schließen.", flush=True)
        t0 = time.time()
        while time.time() - t0 < 600:
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
        print("✅ Fertig – der Bot kann jetzt Reels auf diesem Konto posten.", flush=True)
        return True


def post(video: Path, caption: str, log=print, headless: bool = True, dry_run: bool = False) -> str:
    """Postet im Hintergrund; scheitert das unsichtbare Hochladen VOR „Teilen“, einmal mit Fenster.
    dry_run: alles bis vor „Teilen“, mit Bildschirmfotos jedes Schritts in logs/instagram."""
    if dry_run:
        return _post(video, caption, log, headless, dry_run=True)
    try:
        return _post(video, caption, log, headless)
    except (NeedsHuman, ProfileBusy):
        raise
    except Exception as e:
        if not headless or getattr(e, "shared_clicked", True):
            raise
        log(f"  Instagram im Hintergrund fehlgeschlagen ({str(e)[:80]}) – nochmal mit Fenster")
        return _post(video, caption, log, False)


def _post(video: Path, caption: str, log=print, headless: bool = True, dry_run: bool = False) -> str:
    if _busy():
        raise ProfileBusy("das Instagram-Browserfenster ist noch offen")
    from playwright.sync_api import sync_playwright
    shared = False
    with _LOCK, sync_playwright() as p:
        ctx = _launch(p, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if not _logged_in(ctx):
                raise NeedsHuman("Instagram-Login fehlt oder ist abgelaufen")
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90_000)
            time.sleep(4)
            _check(page)
            _dismiss(page)
            # 1. Erstellen öffnen (Seitenleiste), ggf. Untermenü „Beitrag“
            create = page.get_by_role("link", name=CREATE)
            if not _visible(create):
                create = page.locator("svg[aria-label='Neuer Beitrag'], svg[aria-label='New post']").first.locator("xpath=..")
            _click(create.first)
            time.sleep(1.5)
            sub = page.get_by_role("link", name=POST_ITEM)
            if _visible(sub):
                _click(sub.first)
            # 2. Video auswählen
            def snap(tag):
                if dry_run:
                    _dump(page, tag)
            snap("0_erstellen")
            page.locator("input[type='file']").last.set_input_files(str(video), timeout=60_000)
            log("  Instagram: Video wird hochgeladen …")
            time.sleep(6)
            snap("1_hochgeladen")
            ok = page.get_by_role("button", name=OK_BTN)  # „Videobeiträge werden jetzt als Reels geteilt“
            if _visible(ok):
                _click(ok.first)
            # 3. Format „Original“ (9:16 bleibt 9:16)
            crop = page.get_by_role("button", name=CROP)
            if not _visible(crop):
                crop = page.locator("svg[aria-label*='Zuschneiden'], svg[aria-label*='crop']").first.locator("xpath=..")
            snap("2_vor_format")
            try:
                _click(crop.first, tries=1)
                time.sleep(1)
                snap("3_formatmenue")
                orig = page.get_by_text(ORIGINAL)
                if _visible(orig):
                    _click(orig.first, tries=1)
            except Exception:
                log("  Instagram: Format-Auswahl nicht gefunden – nehme Standard")
                _dump(page, "format")
            # 4. Weiter (Zuschnitt) → Weiter (Bearbeiten)
            for step in range(2):
                nxt = page.get_by_role("button", name=NEXT)
                t0 = time.time()
                while not _visible(nxt) and time.time() - t0 < 120:
                    time.sleep(2)
                snap(f"{4 + step}_vor_weiter")
                _click(nxt.first)
                time.sleep(3)
            # 5. Caption
            cap = page.locator("div[contenteditable='true'][aria-label*='Bildunterschrift'], "
                               "div[contenteditable='true'][aria-label*='caption'], "
                               "div[contenteditable='true'][role='textbox']").first
            cap.wait_for(state="visible", timeout=60_000)
            _click(cap)
            page.keyboard.type(caption.strip(), delay=20)  # Zeilenumbruch nur nach dem Satz, nicht nach Hashtags
            time.sleep(2)
            snap("6_caption")
            if dry_run:
                return "Probelauf bis vor „Teilen“ – Bilder in logs/instagram"
            # 6. Teilen
            share = page.get_by_role("button", name=SHARE)
            shared = True
            _click(share.first)
            t0 = time.time()
            while time.time() - t0 < 240:
                _check(page)
                try:
                    if page.get_by_text(DONE).first.is_visible(timeout=500):
                        break
                except Exception:
                    pass
                time.sleep(3)
            else:
                raise RuntimeError("keine Bestätigung von Instagram nach dem Teilen")
            time.sleep(4)
            try:
                _, link = _latest_reel(page)
            except Exception:
                link = ""
            return link or "gepostet (neuestes Reel im Profil)"
        except Exception as e:
            shot = _dump(page, "fehler")
            log(f"  Instagram: Bildschirmfoto vom Fehler in logs/instagram/{shot.name if shot else '-'}")
            try:
                e.shared_clicked = shared
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
        print(post(Path(sys.argv[2]), sys.argv[3], headless=False))
        sys.exit(0)
    print("Aufruf: insta_web.py login | post <video.mp4> <caption>")
