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
