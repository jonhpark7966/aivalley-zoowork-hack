"""Cut a segment out of a source video, draw the product highlight overlay, and join clips.

This is the scripted path: a few fixed looks ("presets") drawn with Pillow and encoded with
ffmpeg. The free-form path, where an agent edits the video itself, lives in director.py.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H, FPS = 720, 1280, 30
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_HEAVY = "/System/Library/Fonts/Supplemental/Impact.ttf"
FONT_FALLBACK = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
PLATFORM_LABEL = {"youtube": "YouTube Shorts", "tiktok": "TikTok"}
PLATFORM_COLOR = {"youtube": (255, 0, 51), "tiktok": (37, 244, 238)}

# Keyframes further apart than this are treated as "product left the frame".
MAX_KEYFRAME_GAP = 1.6
FADE = 0.25
SMOOTHING = 0.35

ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS)]
AUDIO = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]


@dataclass(frozen=True)
class Style:
    key: str
    label: str
    description: str
    accent: tuple[int, int, int]
    dim: float  # how far to darken everything except the product
    ring: str  # "full" outline with accent corners, or "corners" only
    zoom: float  # punch-in towards the product
    caption: str  # "pill", "lower" or "block"


STYLES = {
    s.key: s
    for s in [
        Style("spotlight", "Spotlight", "Dims the scene and rings the product", (255, 214, 10), 0.5, "full", 1.0, "pill"),
        Style("clean", "Clean", "Thin corner marks, quiet lower-third captions", (255, 255, 255), 0.0, "corners", 1.0, "lower"),
        Style("bold", "Bold", "Punches in on the product with big block captions", (198, 255, 0), 0.25, "corners", 1.22, "block"),
    ]
}


@dataclass
class Keyframe:
    t: float  # seconds from the start of the source video
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions of the source frame


@dataclass
class Credit:
    """Who a clip came from, as shown on screen."""

    platform: str
    handle: str
    followers: int | None = None
    views: int | None = None
    official: bool = False


def compact(n: int | None) -> str:
    if n is None:
        return ""
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            value = n / limit
            return f"{value:.0f}{suffix}" if value >= 100 else f"{value:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def reach(credit: Credit) -> str:
    """'38.6K followers · 5.7K views', with whichever parts are known."""
    unit = "subscribers" if credit.platform == "youtube" else "followers"
    parts = []
    if credit.followers:
        parts.append(f"{compact(credit.followers)} {unit}")
    if credit.views:
        parts.append(f"{compact(credit.views)} views")
    return "  ·  ".join(parts)


def _font(size: int, heavy: bool = False) -> ImageFont.FreeTypeFont:
    for path in ((FONT_HEAVY, FONT_BOLD) if heavy else (FONT_BOLD,)) + (FONT_FALLBACK,):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _fit(text: str, size: int, max_width: int, heavy: bool = False) -> tuple[str, ImageFont.FreeTypeFont]:
    """Shrink, then truncate, until the text fits."""
    font = _font(size, heavy)
    while font.getlength(text) > max_width and size > 22:
        size -= 2
        font = _font(size, heavy)
    while font.getlength(text) > max_width and len(text) > 4:
        text = text[:-2].rstrip() + "…"
    return text, font


def _pill(text: str, size: int, fg, bg, max_width: int = W - 80) -> Image.Image:
    """Rounded label, drawn at 2x and scaled down for clean edges."""
    s = 2
    pad_x, pad_y = 18 * s, 10 * s
    text, font = _fit(text, size * s, max_width * s - pad_x * 2)
    w = int(font.getlength(text)) + pad_x * 2
    h = size * s + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=bg)
    d.text((pad_x, h / 2), text, font=font, fill=fg, anchor="lm")
    return img.resize((w // s, h // s), Image.LANCZOS)


def _creator_card(credit: Credit, accent) -> Image.Image:
    """Top-left card: platform, handle and how big the creator and the video are."""
    s = 2
    name, name_font = _fit(credit.handle, 30 * s, 400 * s)
    line2 = reach(credit) or PLATFORM_LABEL.get(credit.platform, credit.platform)
    line2, small = _fit(line2, 22 * s, 520 * s)
    tag_font = _font(17 * s)
    tag = "OFFICIAL" if credit.official else ""
    tag_w = int(tag_font.getlength(tag)) + 20 * s if tag else 0

    dot = 30 * s
    pad = 16 * s
    text_x = pad + dot + 12 * s
    w = text_x + int(max(name_font.getlength(name) + (tag_w + 12 * s if tag else 0), small.getlength(line2))) + pad
    h = 92 * s
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=22 * s, fill=(10, 10, 14, 175))
    cy = h // 2
    d.ellipse([pad, cy - dot // 2, pad + dot, cy + dot // 2], fill=PLATFORM_COLOR.get(credit.platform, accent))
    d.polygon(
        [(pad + dot * 0.38, cy - dot * 0.22), (pad + dot * 0.38, cy + dot * 0.22), (pad + dot * 0.72, cy)],
        fill=(255, 255, 255) if credit.platform == "youtube" else (10, 10, 14),
    )
    d.text((text_x, 30 * s), name, font=name_font, fill=(255, 255, 255), anchor="lm")
    if tag:
        tx = text_x + int(name_font.getlength(name)) + 12 * s
        d.rounded_rectangle([tx, 17 * s, tx + tag_w, 43 * s], radius=13 * s, fill=accent)
        d.text((tx + tag_w / 2, 30 * s), tag, font=tag_font, fill=(10, 10, 14), anchor="mm")
    d.text((text_x, 66 * s), line2, font=small, fill=(215, 215, 225), anchor="lm")
    return img.resize((w // s, h // s), Image.LANCZOS)


def _caption(text: str, style: Style) -> Image.Image | None:
    if not text:
        return None
    if style.caption == "pill":
        return _pill(text, 40, fg=(255, 255, 255, 255), bg=(0, 0, 0, 175))
    s = 2
    if style.caption == "block":
        text, font = _fit(text.upper(), 62 * s, (W - 110) * s, heavy=True)
        w, h = int(font.getlength(text)) + 44 * s, 92 * s
        img = Image.new("RGBA", (w, h), style.accent + (255,))
        ImageDraw.Draw(img).text((w / 2, h / 2), text, font=font, fill=(10, 10, 14), anchor="mm")
        img = img.resize((w // s, h // s), Image.LANCZOS)
        return img.rotate(-2.5, expand=True, resample=Image.BICUBIC)
    # Lower third: accent bar, then text with a soft shadow.
    text, font = _fit(text, 40 * s, (W - 130) * s)
    w, h = int(font.getlength(text)) + 40 * s, 64 * s
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((26 * s, h / 2 + 2 * s), text, font=font, fill=(0, 0, 0, 200), anchor="lm")
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(4 * s)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 8 * s, 6 * s, h - 8 * s], radius=3 * s, fill=style.accent + (255,))
    d.text((26 * s, h / 2), text, font=font, fill=(255, 255, 255), anchor="lm")
    return img.resize((w // s, h // s), Image.LANCZOS)


def _scrim() -> Image.Image:
    """Soft dark gradients at the top and bottom so overlays stay readable."""
    column = Image.new("L", (1, H), 0)
    for y in range(H):
        top = max(0.0, 1 - y / 230)
        bottom = max(0.0, (y - (H - 420)) / 420)
        column.putpixel((0, y), int(150 * max(top, bottom) ** 1.6))
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    scrim.putalpha(column.resize((W, H)))
    return scrim


def _ring(bw: int, bh: int, style: Style) -> Image.Image:
    """Outline sized to wrap a bw x bh box: full with accent corners, or corners only."""
    s = 2
    pad = 10
    w, h = (bw + pad * 2) * s, (bh + pad * 2) * s
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    radius = int(min(bw, bh) * 0.12 + 10) * s
    rect = [pad * s, pad * s, w - pad * s - 1, h - pad * s - 1]
    if style.ring == "full":
        ImageDraw.Draw(img).rounded_rectangle(rect, radius=radius, outline=(255, 255, 255, 235), width=3 * s)

    # Keep the accent outline only near the four corners.
    corners = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    thickness = 7 if style.ring == "full" else (9 if style.zoom > 1 else 5)
    ImageDraw.Draw(corners).rounded_rectangle(rect, radius=radius, outline=style.accent + (255,), width=thickness * s)
    arm = int(min(bw, bh) * 0.22 + 14) * s
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    for x in (0, w - arm - pad * s):
        for y in (0, h - arm - pad * s):
            md.rectangle([x, y, x + arm + pad * s, y + arm + pad * s], fill=255)
    corners.putalpha(ImageChops.multiply(corners.getchannel("A"), mask))
    img.alpha_composite(corners)
    return img.resize((w // s, h // s), Image.LANCZOS)


def _with_alpha(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
    return out


def _paste(canvas: Image.Image, img: Image.Image, x: int, y: int) -> None:
    """Alpha-composite img at (x, y), dropping whatever falls outside the canvas."""
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(img.width, W - x), min(img.height, H - y)
    if right > left and bottom > top:
        canvas.alpha_composite(img, (max(0, x), max(0, y)), (left, top, right, bottom))


def _layout(src_w: int, src_h: int) -> tuple[str, float, float, float]:
    """ffmpeg filter that fits the source onto the canvas, plus the scale and offset it applies."""
    if src_w / src_h < 0.7:
        # Vertical source: fill the canvas and crop the overflow.
        scale = max(W / src_w, H / src_h)
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    else:
        # Wide source: centre it over a blurred copy of itself.
        scale = min(W / src_w, H / src_h)
        vf = (
            f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"gblur=sigma=28,eq=brightness=-0.12[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    ox = (W - src_w * scale) / 2
    oy = (H - src_h * scale) / 2
    return vf, scale, ox, oy


def _box_at(keys: list[Keyframe], t: float) -> tuple[tuple[float, float, float, float] | None, float]:
    """Interpolated box at time t and how visible the highlight should be (0..1)."""
    if not keys:
        return None, 0.0
    if t <= keys[0].t:
        return keys[0].box, max(0.0, 1 - (keys[0].t - t) / FADE)
    if t >= keys[-1].t:
        return keys[-1].box, max(0.0, 1 - (t - keys[-1].t) / FADE)
    for a, b in zip(keys, keys[1:]):
        if a.t <= t <= b.t:
            if b.t - a.t > MAX_KEYFRAME_GAP:
                if t - a.t < b.t - t:
                    return a.box, max(0.0, 1 - (t - a.t) / FADE)
                return b.box, max(0.0, 1 - (b.t - t) / FADE)
            f = (t - a.t) / (b.t - a.t) if b.t > a.t else 0.0
            return tuple(a.box[i] + (b.box[i] - a.box[i]) * f for i in range(4)), 1.0
    return None, 0.0


def _ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def _progress_bar(frame: Image.Image, index: int, total: int, fraction: float) -> None:
    """Story-style segments along the top edge: one per clip, the current one filling up."""
    gap, margin, y = 6, 24, 18
    seg = (W - margin * 2 - gap * (total - 1)) / total
    d = ImageDraw.Draw(frame, "RGBA")
    for i in range(total):
        x = margin + i * (seg + gap)
        d.rounded_rectangle([x, y, x + seg, y + 5], radius=3, fill=(255, 255, 255, 80))
        filled = 1.0 if i < index else (fraction if i == index else 0.0)
        if filled > 0:
            d.rounded_rectangle([x, y, x + max(5, seg * filled), y + 5], radius=3, fill=(255, 255, 255, 240))


def render_clip(
    src: Path,
    out: Path,
    *,
    src_w: int,
    src_h: int,
    has_audio: bool,
    start: float,
    end: float,
    keyframes: list[Keyframe],
    product: str,
    credit: Credit,
    caption: str = "",
    style: Style = STYLES["spotlight"],
    index: int = 0,
    total: int = 1,
) -> Path:
    duration = end - start
    vf, scale, ox, oy = _layout(src_w, src_h)
    keys = sorted(keyframes, key=lambda k: k.t)

    scrim = _scrim()
    card = _creator_card(credit, style.accent)
    label = _pill(product, 26, fg=(10, 10, 14, 255), bg=style.accent + (255,), max_width=W - 140)
    caption_img = _caption(caption, style)

    decoder = subprocess.Popen(
        [
            "ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
            "-filter_complex", f"[0:v]{vf},fps={FPS},format=rgb24",
            "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        stdout=subprocess.PIPE,
    )
    audio_in = (
        ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
        if has_audio
        else ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    )
    fade_out = max(0.0, duration - 0.18)
    encoder = subprocess.Popen(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0",
            *audio_in,
            "-map", "0:v", "-map", "1:a",
            "-af", f"afade=t=in:d=0.12,afade=t=out:st={fade_out:.3f}:d=0.18",
            *ENCODE, *AUDIO, "-t", f"{duration:.3f}", str(out),
        ],
        stdin=subprocess.PIPE,
    )

    frame_bytes = W * H * 3
    total_frames = max(1, round(duration * FPS))
    smooth: tuple[float, float, float, float] | None = None
    focus: tuple[float, float] | None = None
    n = 0
    try:
        while True:
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = Image.frombuffer("RGB", (W, H), raw, "raw", "RGB", 0, 1)
            elapsed = n / FPS
            box, alpha = _box_at(keys, start + elapsed)

            if box is not None and alpha > 0.01:
                # Source-frame fractions -> canvas pixels.
                target = (
                    box[0] * src_w * scale + ox, box[1] * src_h * scale + oy,
                    box[2] * src_w * scale + ox, box[3] * src_h * scale + oy,
                )
                smooth = target if smooth is None else tuple(
                    s + (t - s) * SMOOTHING for s, t in zip(smooth, target)
                )
            else:
                smooth = None

            shown = smooth
            if style.zoom > 1 and (smooth or focus):
                # Punch in towards the product and stay there.
                if smooth:
                    centre = ((smooth[0] + smooth[2]) / 2, (smooth[1] + smooth[3]) / 2)
                    focus = centre if focus is None else tuple(f + (c - f) * 0.12 for f, c in zip(focus, centre))
                zoom = 1 + (style.zoom - 1) * _ease((elapsed - 0.15) / 0.55)
                cw, ch = W / zoom, H / zoom
                left = min(max(focus[0], cw / 2), W - cw / 2) - cw / 2
                top = min(max(focus[1], ch / 2), H - ch / 2) - ch / 2
                frame = frame.resize((W, H), Image.BILINEAR, box=(left, top, left + cw, top + ch))
                if smooth:
                    shown = (
                        (smooth[0] - left) * zoom, (smooth[1] - top) * zoom,
                        (smooth[2] - left) * zoom, (smooth[3] - top) * zoom,
                    )

            if shown is not None:
                frame = _draw_highlight(frame, shown, alpha, label, style)

            frame = frame.convert("RGBA")
            frame.alpha_composite(scrim)
            _progress_bar(frame, index, total, (n + 1) / total_frames)
            intro = _ease(elapsed / 0.35)
            _paste(frame, _with_alpha(card, intro), int(24 - 30 * (1 - intro)), 42)
            if caption_img is not None:
                rise = _ease((elapsed - 0.2) / 0.35)
                img = _with_alpha(caption_img, rise)
                x = 40 if style.caption == "lower" else (W - img.width) // 2
                _paste(frame, img, x, H - 170 - img.height + int(18 * (1 - rise)))
            encoder.stdin.write(frame.convert("RGB").tobytes())
            n += 1
    finally:
        decoder.stdout.close()
        encoder.stdin.close()
        decoder.wait()
        encoder.wait()
    if n == 0 or encoder.returncode != 0:
        raise RuntimeError(f"render failed for {src.name} (frames={n}, ffmpeg={encoder.returncode})")
    return out


def _draw_highlight(frame: Image.Image, box, alpha: float, label: Image.Image, style: Style) -> Image.Image:
    # The ring starts slightly large and settles onto the product.
    grow = 1 + 0.22 * (1 - alpha) ** 2
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    bw = max(40.0, (box[2] - box[0]) * grow)
    bh = max(40.0, (box[3] - box[1]) * grow)
    x1, y1, x2, y2 = cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2

    if style.dim > 0:
        # Spotlight: dim everything outside a soft-edged copy of the box.
        q = 4
        mask = Image.new("L", (W // q, H // q), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [(x1 - 14) / q, (y1 - 14) / q, (x2 + 14) / q, (y2 + 14) / q],
            radius=(min(bw, bh) * 0.12 + 14) / q, fill=255,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(5)).resize((W, H), Image.BILINEAR)
        dimmed = ImageEnhance.Brightness(frame).enhance(1 - style.dim * alpha)
        frame = Image.composite(frame, dimmed, mask)
    out = frame.convert("RGBA")

    _paste(out, _with_alpha(_ring(int(bw), int(bh), style), alpha), int(x1) - 10, int(y1) - 10)

    tag = _with_alpha(label, alpha)
    lx = int(min(max(12, cx - tag.width / 2), W - tag.width - 12))
    ly = int(y1 - tag.height - 18)
    if ly < 150:
        # No room above (the creator card is there): go below, or inside the box when the caption is below.
        ly = int(y2 + 18)
        if ly + tag.height > H - 300:
            ly = int(y2 - tag.height - 16)
    _paste(out, tag, lx, ly)
    return out.convert("RGB")


def render_end_card(out: Path, product: str, credits: list[Credit], style: Style, duration: float = 2.2) -> Path:
    img = Image.new("RGB", (W, H), (10, 10, 14))
    d = ImageDraw.Draw(img)
    d.text((W / 2, 300), "AS SEEN ON", font=_font(30), fill=(160, 160, 172), anchor="mm")
    platforms = sorted({PLATFORM_LABEL.get(c.platform, c.platform) for c in credits})
    d.text((W / 2, 346), "  ·  ".join(platforms), font=_font(30), fill=(160, 160, 172), anchor="mm")

    title_font = _font(62)
    lines, line = [], ""
    for word in product.split():
        trial = f"{line} {word}".strip()
        if title_font.getlength(trial) > W - 100 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    y = 450
    for text in lines[:3]:
        text, font = _fit(text, 62, W - 100)
        d.text((W / 2, y), text, font=font, fill=style.accent, anchor="mm")
        y += 76

    # The headline number: how many people these creators reach.
    views = sum(c.views or 0 for c in credits)
    followers = sum(c.followers or 0 for c in credits)
    y += 34
    d.text((W / 2, y), f"Loved by {len(credits)} creator{'s' if len(credits) != 1 else ''}",
           font=_font(38), fill=(255, 255, 255), anchor="mm")
    y += 54
    totals = "  ·  ".join(
        part for part in (
            f"{compact(followers)} combined followers" if followers else "",
            f"{compact(views)} views" if views else "",
        ) if part
    )
    if totals:
        d.text((W / 2, y), totals, font=_font(26), fill=(200, 200, 212), anchor="mm")
    y += 70
    for credit in credits[:6]:
        size = "  ·  " + compact(credit.followers) if credit.followers else ""
        text, font = _fit(credit.handle + size, 28, W - 120)
        d.text((W / 2, y), text, font=font, fill=(200, 200, 212), anchor="mm")
        y += 42

    still = out.with_suffix(".png")
    img.save(still)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-loop", "1", "-t", f"{duration}", "-i", str(still),
            "-f", "lavfi", "-t", f"{duration}", "-i", "anullsrc=r=44100:cl=stereo",
            "-vf", "fade=t=in:d=0.25,format=yuv420p", *ENCODE, *AUDIO, "-t", f"{duration}", str(out),
        ],
        check=True,
    )
    return out


def concat(clips: list[Path], out: Path) -> Path:
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", "-movflags", "+faststart", str(out),
        ],
        check=True,
    )
    return out
