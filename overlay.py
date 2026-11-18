"""
9:16-Render mit eingebrannten Untertiteln – ohne libass.

Hook, Credit und Untertitel werden mit Pillow als transparente PNGs gezeichnet
und per ffmpeg-Overlay ins Video gelegt. Funktioniert mit jedem ffmpeg-Build
(z. B. dem schlanken Homebrew-ffmpeg auf dem Mac).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT = ROOT / "fonts" / "Montserrat-ExtraBold.ttf"
W, H = 1080, 1920
CAP_H = 340  # Höhe des Untertitel-Streifens
YELLOW = (255, 229, 0, 255)
WHITE = (255, 255, 255, 255)

_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _fonts:
        _fonts[size] = ImageFont.truetype(str(FONT), size)
    return _fonts[size]


def ffmpeg_bin() -> str:
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(p).exists():
            return p
    return "ffmpeg"


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def layout_for(src_w: int, src_h: int, zoom: float = 1.0) -> dict:
    fg_w = int(round(W * max(zoom, 1.0) / 2) * 2)
    fg_h = min(int(round(fg_w * src_h / src_w / 2) * 2), H)
    top = (H - fg_h) // 2
    return {"fg_w": fg_w, "fg_h": fg_h, "top": top, "bottom": top + fg_h,
            "cap_y": min(top + fg_h + 40, H - CAP_H - 120),
            "hook_y": max(top - 50, 300)}


def _wrap(draw: ImageDraw.ImageDraw, words: list[str], f, max_w: float) -> list[list[int]]:
    lines: list[list[int]] = [[]]
    for i, _ in enumerate(words):
        test = " ".join(words[j] for j in lines[-1] + [i])
        if lines[-1] and draw.textlength(test, font=f) > max_w:
            lines.append([i])
        else:
            lines[-1].append(i)
    return lines


# ---------------------------------------------------------------------------
# PNG-Ebenen
# ---------------------------------------------------------------------------

def static_overlay(path: Path, hook: str, credit: str, hook_y: int) -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if hook:
        f = font(60)
        words = hook.upper().split()
        lines = [" ".join(words[j] for j in ln) for ln in _wrap(d, words, f, 860)][:3]
        lh = 76
        total = lh * len(lines)
        top = hook_y - total
        maxw = max(d.textlength(t, font=f) for t in lines)
        d.rounded_rectangle([(W - maxw) / 2 - 34, top - 26, (W + maxw) / 2 + 34, top + total + 6],
                            radius=20, fill=(0, 0, 0, 230))
        for k, t in enumerate(lines):
            d.text((W / 2, top + k * lh), t, font=f, fill=WHITE, anchor="ma")
    if credit:
        d.text((W / 2, H - 140), credit, font=font(40), fill=(255, 255, 255, 210), anchor="ms",
               stroke_width=3, stroke_fill=(0, 0, 0, 170))
    img.save(path)


def caption_png(path: Path, words: list[str], active: int | None) -> None:
    img = Image.new("RGBA", (W, CAP_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font(88)
    space = d.textlength(" ", font=f)
    y = 24
    for ln in _wrap(d, words, f, 980)[:2]:
        widths = [d.textlength(words[j], font=f) for j in ln]
        x = (W - (sum(widths) + space * (len(ln) - 1))) / 2
        for j, wd in zip(ln, widths):
            d.text((x + 4, y + 6), words[j], font=f, fill=(0, 0, 0, 140), stroke_width=8,
                   stroke_fill=(0, 0, 0, 140))  # Schatten
            d.text((x, y), words[j], font=f, fill=YELLOW if j == active else WHITE,
                   stroke_width=8, stroke_fill=(0, 0, 0, 255))
            x += wd + space
        y += 112
    img.save(path)


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


def caption_track(words: list[tuple[float, float, str]], dur: float, workdir: Path) -> Path:
    """Schreibt PNGs + concat-Liste; Rückgabe: Pfad der concat-Liste."""
    blank = workdir / "cap_blank.png"
    Image.new("RGBA", (W, CAP_H), (0, 0, 0, 0)).save(blank)

    events = []  # (start, end, png)
    groups = group_words(words)
    n = 0
    for gi, g in enumerate(groups):
        next_start = groups[gi + 1][0][0] if gi + 1 < len(groups) else dur
        g_end = min(g[-1][1] + 0.35, next_start) if next_start > g[-1][1] else g[-1][1]
        texts = [x[2].upper() for x in g]
        for wi, w in enumerate(g):
            start = w[0] if wi else g[0][0]
            end = g[wi + 1][0] if wi + 1 < len(g) else g_end
            if end - start < 0.05:
                continue
            png = workdir / f"cap_{n:04d}.png"
            caption_png(png, texts, wi)
            events.append((max(0.0, start), min(end, dur), png))
            n += 1

    lst = workdir / "caps.txt"
    lines, t = [], 0.0
    for start, end, png in events:
        if start > t + 0.01:
            lines += [f"file '{blank}'", f"duration {start - t:.3f}"]
            t = start
        if end <= t:
            continue
        lines += [f"file '{png}'", f"duration {end - t:.3f}"]
        t = end
    if dur > t:
        lines += [f"file '{blank}'", f"duration {dur - t + 0.5:.3f}"]
    lines.append(f"file '{blank}'")  # concat-Demuxer braucht das letzte Bild doppelt
    lst.write_text("ffconcat version 1.0\n" + "\n".join(lines) + "\n")
    return lst


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(src: Path, out: Path, words, dur: float, lay: dict, hook: str, credit: str,
           maxrate: float = 4.5) -> None:
    wd = out.parent
    static = wd / "static.png"
    static_overlay(static, hook, credit, lay["hook_y"])
    caps = caption_track(words, dur, wd)
    fg_w = lay["fg_w"]
    crop = f",crop={W}:ih" if fg_w > W else ""
    fc = (
        f"[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        f"boxblur=12:2,scale={W}:{H},eq=brightness=-0.12:saturation=1.2,setsar=1[bg];"
        f"[0:v]scale={fg_w}:-2{crop},setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v1];"
        f"[1:v]format=rgba[st];[v1][st]overlay=0:0[v2];"
        f"[2:v]format=rgba[cp];[v2][cp]overlay=0:{lay['cap_y']}:eof_action=pass,format=yuv420p[v]"
    )
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src), "-loop", "1", "-i", str(static),
           "-f", "concat", "-safe", "0", "-i", str(caps),
           "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", "-t", f"{dur:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-maxrate", f"{maxrate}M", "-bufsize", f"{maxrate * 2}M", "-profile:v", "high",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, timeout=900)


def render_fit(src: Path, out: Path, words, dur: float, lay: dict, hook: str, credit: str,
               limit_bytes: float = 48e6) -> None:
    """Rendert und drückt die Bitrate, bis die Datei unter das Telegram-Limit passt."""
    for rate in (4.5, 3.0, 2.0):
        render(src, out, words, dur, lay, hook, credit, maxrate=rate)
        if out.stat().st_size <= limit_bytes:
            return


# ===========================================================================
# Neues Layout (ab 18.11.): oben Facecam, unten Gameplay, Titel-Kästen in der Mitte,
# keine Untertitel, kein Credit. Ohne erkennbare Facecam: klassisches Layout mit Titel.
# ===========================================================================

import re as _re
import subprocess as _sp
import sys as _sys
import tempfile as _tf
import urllib.request as _ur

TITLE_FONT = ROOT / "fonts" / "Montserrat-Bold.ttf"
TOP_H = 880                      # Höhe Facecam-Bereich
BOT_H = H - TOP_H                # Höhe Gameplay-Bereich
APPLE_EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"
EMOJI_RE = _re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2}"
    "|[☀-➿⬀-⯿\U0001F000-\U0001FAFF][️\U0001F3FB-\U0001F3FF]*"
    "(?:‍[☀-➿\U0001F000-\U0001FAFF][️\U0001F3FB-\U0001F3FF]*)*)")
_emoji_cache: dict = {}


def _emoji_img(e: str, size: int) -> Image.Image | None:
    key = (e, size)
    if key in _emoji_cache:
        return _emoji_cache[key]
    img = None
    if Path(APPLE_EMOJI).exists():  # auf dem Mac: echte Apple-Emojis wie bei TikTok/Instagram
        try:
            f = ImageFont.truetype(APPLE_EMOJI, 160)
            im = Image.new("RGBA", (220, 220), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((10, 10), e, font=f, embedded_color=True)
            bb = im.getbbox()
            img = im.crop(bb) if bb else None
        except Exception:
            img = None
    if img is None:  # sonst Twemoji-Grafik (wird einmal geladen und gespeichert)
        d = ROOT / "fonts" / "emoji"
        d.mkdir(parents=True, exist_ok=True)
        for code in ("-".join(f"{ord(c):x}" for c in e if ord(c) != 0xFE0F),
                     "-".join(f"{ord(c):x}" for c in e)):
            p = d / f"{code}.png"
            if not p.exists():
                try:
                    _ur.urlretrieve(f"https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/{code}.png", p)
                except Exception:
                    continue
            try:
                img = Image.open(p).convert("RGBA")
                break
            except Exception:
                p.unlink(missing_ok=True)
    if img is not None:
        img = img.resize((size, size), Image.LANCZOS)
    _emoji_cache[key] = img
    return img


def _runs(text: str):
    pos, out = 0, []
    for m in EMOJI_RE.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def _width(draw, text: str, f, esize: int) -> float:
    w = 0.0
    for is_e, t in _runs(text):
        w += esize + 4 if is_e else draw.textlength(t, font=f)
    return w


def title_overlay(path: Path, title: str, center_y: int) -> None:
    """Weiße abgerundete Kästen, schwarze fette Schrift, Emojis farbig – wie TikTok-Text."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if title:
        d = ImageDraw.Draw(img)
        size = 74
        f = ImageFont.truetype(str(TITLE_FONT), size)
        esize = int(size * 1.05)
        words, lines = title.split(), [""]
        for w_ in words:
            test = (lines[-1] + " " + w_).strip()
            if lines[-1] and _width(d, test, f, esize) > 940:
                lines.append(w_)
            else:
                lines[-1] = test
        lines = lines[:3]
        lh, pad_x, pad_y = 104, 30, 14
        top = center_y - (lh * len(lines)) // 2
        for i, ln in enumerate(lines):
            lw = _width(d, ln, f, esize)
            y0 = top + i * lh
            d.rounded_rectangle([(W - lw) / 2 - pad_x, y0 - pad_y, (W + lw) / 2 + pad_x, y0 + lh - pad_y + 6],
                                radius=22, fill=(255, 255, 255, 255))
        for i, ln in enumerate(lines):
            lw = _width(d, ln, f, esize)
            x, y0 = (W - lw) / 2, top + i * lh
            for is_e, t in _runs(ln):
                if is_e:
                    em = _emoji_img(t, esize)
                    if em is not None:
                        img.alpha_composite(em, (int(x + 2), int(y0 + 4)))
                    x += esize + 4
                else:
                    d.text((x, y0 + 4), t, font=f, fill=(0, 0, 0, 255))
                    x += d.textlength(t, font=f)
    img.save(path)


def _cv2():
    import importlib
    try:
        return importlib.import_module("cv2")
    except ImportError:  # einmalig nachinstallieren (uv-venv hat kein pip)
        for cmd in (["uv", "pip", "install", "--python", _sys.executable, "opencv-python-headless"],
                    [_sys.executable, "-m", "pip", "install", "-q", "opencv-python-headless"]):
            try:
                _sp.run(cmd, check=True, timeout=600, capture_output=True)
                break
            except Exception:
                continue
    return importlib.import_module("cv2")


def detect_facecam(src: Path, dur: float, n: int = 10):
    """Findet die Facecam: ein Gesicht, das über den Clip hinweg an derselben Stelle bleibt.
    Rückgabe (x, y, w, h, frame_w, frame_h) des Gesichts oder None."""
    cv2 = _cv2()
    yunet = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
    det_yn = None
    casc = None
    if yunet.exists() and hasattr(cv2, "FaceDetectorYN"):
        det_yn = cv2.FaceDetectorYN.create(str(yunet), "", (320, 320), 0.75, 0.3, 50)
    elif hasattr(cv2, "CascadeClassifier"):
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    else:
        return None
    dets, fw, fh = [], 0, 0
    with _tf.TemporaryDirectory() as td:
        for i in range(n):
            t = dur * (i + 0.5) / n
            jp = Path(td) / f"f{i}.jpg"
            _sp.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(src),
                     "-frames:v", "1", "-q:v", "3", str(jp)], check=False, timeout=30)
            img = cv2.imread(str(jp))
            if img is None:
                continue
            fh, fw = img.shape[:2]
            m = fh * 0.05
            if det_yn is not None:
                det_yn.setInputSize((fw, fh))
                _, faces = det_yn.detect(img)
                for f in (faces if faces is not None else []):
                    x, y, w, h = (float(v) for v in f[:4])
                    if w >= m:
                        dets.append((i, x, y, w, h))
            else:
                gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
                for (x, y, w, h) in casc.detectMultiScale(gray, 1.1, 6, minSize=(int(m), int(m))):
                    dets.append((i, x, y, w, h))
    if not dets:
        return None
    best, best_n = None, 0
    for (i, x, y, w, h) in dets:
        cx, cy = x + w / 2, y + h / 2
        frames = {j for (j, x2, y2, w2, h2) in dets
                  if abs(x2 + w2 / 2 - cx) < fw * 0.05 and abs(y2 + h2 / 2 - cy) < fh * 0.06
                  and 0.6 < w2 / w < 1.6}
        if len(frames) > best_n or (len(frames) == best_n and best and w > best[3]):
            best, best_n = (i, x, y, w, h), len(frames)
    if best_n < max(4, n * 0.4):
        return None
    _, x, y, w, h = best
    group = [(x2, y2, w2, h2) for (j, x2, y2, w2, h2) in dets
             if abs(x2 + w2 / 2 - (x + w / 2)) < fw * 0.05 and abs(y2 + h2 / 2 - (y + h / 2)) < fh * 0.06]
    ax = sum(g[0] for g in group) / len(group)
    ay = sum(g[1] for g in group) / len(group)
    aw = sum(g[2] for g in group) / len(group)
    ah = sum(g[3] for g in group) / len(group)
    return (ax, ay, aw, ah, fw, fh)


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def split_crops(face, fw: int, fh: int) -> tuple[tuple, tuple]:
    x, y, w, h = face[:4]
    ar_top = W / TOP_H
    cw = min(max(w * 3.3, fw * 0.16), fw)
    ch = cw / ar_top
    if ch > fh:
        ch, cw = fh, fh * ar_top
    cx = min(max(x + w / 2 - cw / 2, 0), fw - cw)
    cy = min(max(y + h / 2 - ch * 0.45, 0), fh - ch)
    ar_bot = W / BOT_H
    gh = fh
    gw = min(gh * ar_bot, fw)
    gx = (fw - gw) / 2
    return ((_even(cw), _even(ch), _even(cx), _even(cy)), (_even(gw), _even(gh), _even(gx), 0))


def render_short(src: Path, out: Path, dur: float, title: str, maxrate: float = 4.5, face="auto") -> str:
    """Rendert das Short im neuen Look. Rückgabe: 'split' oder 'classic'."""
    wd = out.parent
    if face == "auto":
        try:
            face = detect_facecam(src, dur)
        except Exception:
            face = None
    tpng = wd / "title.png"
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", f"{maxrate}M",
           "-bufsize", f"{maxrate * 2}M", "-profile:v", "high", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart"]
    if face:
        (cw, ch, cx, cy), (gw, gh, gx, gy) = split_crops(face, face[4], face[5])
        title_overlay(tpng, title, TOP_H)
        fc = (f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={W}:{TOP_H}:flags=lanczos,setsar=1[top];"
              f"[0:v]crop={gw}:{gh}:{gx}:{gy},scale={W}:{BOT_H}:flags=lanczos,setsar=1[bot];"
              f"[top][bot]vstack=inputs=2[stk];[1:v]format=rgba[t];[stk][t]overlay=0:0,format=yuv420p[v]")
        mode = "split"
    else:
        lay = layout_for(16, 9)
        title_overlay(tpng, title, max(lay["top"] - 120, 240))
        fc = (f"[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
              f"boxblur=12:2,scale={W}:{H},eq=brightness=-0.12:saturation=1.2,setsar=1[bg];"
              f"[0:v]scale={W}:-2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v1];"
              f"[1:v]format=rgba[t];[v1][t]overlay=0:0,format=yuv420p[v]")
        mode = "classic"
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-loop", "1", "-i", str(tpng),
           "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", "-t", f"{dur:.3f}", *enc, str(out)]
    _sp.run(cmd, check=True, timeout=900)
    return mode


def render_short_fit(src: Path, out: Path, dur: float, title: str, limit_bytes: float = 48e6) -> str:
    try:
        face = detect_facecam(src, dur)
    except Exception:
        face = None
    mode = "classic"
    for rate in (4.5, 3.0, 2.0):
        mode = render_short(src, out, dur, title, maxrate=rate, face=face)
        if out.stat().st_size <= limit_bytes:
            break
    return mode
