"""Cut a segment out of a source video, draw the product highlight overlay, and join clips."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H, FPS = 720, 1280, 30
ACCENT = (255, 214, 10)
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_FALLBACK = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
PLATFORM_LABEL = {"youtube": "YouTube Shorts", "tiktok": "TikTok"}
PLATFORM_COLOR = {"youtube": (255, 0, 51), "tiktok": (37, 244, 238)}

# Keyframes further apart than this are treated as "product left the frame".
MAX_KEYFRAME_GAP = 1.6
FADE = 0.25
SMOOTHING = 0.35

ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS)]
AUDIO = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]


@dataclass
class Keyframe:
    t: float  # seconds from the start of the source video
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions of the source frame


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in (FONT_BOLD, FONT_FALLBACK):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _fit(text: str, size: int, max_width: int) -> tuple[str, ImageFont.FreeTypeFont]:
    """Shrink, then truncate, until the text fits."""
    font = _font(size)
    while font.getlength(text) > max_width and size > 22:
        size -= 2
        font = _font(size)
    while font.getlength(text) > max_width and len(text) > 4:
        text = text[:-2].rstrip() + "…"
    return text, font


def _pill(text: str, size: int, fg, bg, dot=None, max_width: int = W - 80) -> Image.Image:
    """Rounded label, drawn at 2x and scaled down for clean edges."""
    s = 2
    pad_x, pad_y = 18 * s, 10 * s
    dot_w = (size * s) if dot else 0
    text, font = _fit(text, size * s, (max_width * s) - pad_x * 2 - dot_w)
    left, top, right, bottom = font.getbbox(text)
    w = int(right - left) + pad_x * 2 + dot_w
    h = size * s + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=bg)
    if dot:
        r = size * s * 0.28
        cx, cy = pad_x + r, h / 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dot)
    d.text((pad_x + dot_w, h / 2), text, font=font, fill=fg, anchor="lm")
    return img.resize((w // s, h // s), Image.LANCZOS)


def _ring(bw: int, bh: int) -> Image.Image:
    """Rounded outline with accent corners, sized to wrap a bw x bh box."""
    s = 2
    pad = 10
    w, h = (bw + pad * 2) * s, (bh + pad * 2) * s
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = int(min(bw, bh) * 0.12 + 10) * s
    rect = [pad * s, pad * s, w - pad * s - 1, h - pad * s - 1]
    d.rounded_rectangle(rect, radius=radius, outline=(255, 255, 255, 235), width=3 * s)

    # Accent corners: keep the outline only near the four corners.
    corners = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(corners).rounded_rectangle(rect, radius=radius, outline=ACCENT + (255,), width=7 * s)
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
    platform: str,
    handle: str,
    caption: str = "",
) -> Path:
    duration = end - start
    vf, scale, ox, oy = _layout(src_w, src_h)
    keys = sorted(keyframes, key=lambda k: k.t)

    badge = _pill(
        f"{PLATFORM_LABEL.get(platform, platform)}  ·  {handle}", 26,
        fg=(255, 255, 255, 255), bg=(0, 0, 0, 150), dot=PLATFORM_COLOR.get(platform, ACCENT),
    )
    label = _pill(product, 28, fg=(20, 20, 20, 255), bg=ACCENT + (255,), max_width=W - 120)
    caption_img = None
    if caption:
        caption_img = _pill(caption, 40, fg=(255, 255, 255, 255), bg=(0, 0, 0, 170))

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
    smooth: tuple[float, float, float, float] | None = None
    n = 0
    try:
        while True:
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = Image.frombuffer("RGB", (W, H), raw, "raw", "RGB", 0, 1)
            box, alpha = _box_at(keys, start + n / FPS)

            if box is not None and alpha > 0.01:
                # Source-frame fractions -> canvas pixels.
                target = (
                    box[0] * src_w * scale + ox, box[1] * src_h * scale + oy,
                    box[2] * src_w * scale + ox, box[3] * src_h * scale + oy,
                )
                smooth = target if smooth is None else tuple(
                    s + (t - s) * SMOOTHING for s, t in zip(smooth, target)
                )
                frame = _draw_highlight(frame, smooth, alpha, label)
            else:
                smooth = None

            frame = frame.convert("RGBA")
            frame.alpha_composite(badge, (28, 44))
            if caption_img is not None:
                intro = min(1.0, n / (FPS * 0.3))
                img = _with_alpha(caption_img, intro)
                frame.alpha_composite(img, ((W - img.width) // 2, H - 190 - img.height))
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


def _draw_highlight(frame: Image.Image, box, alpha: float, label: Image.Image) -> Image.Image:
    # The ring starts slightly large and settles onto the product.
    grow = 1 + 0.22 * (1 - alpha) ** 2
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    bw = max(40.0, (box[2] - box[0]) * grow)
    bh = max(40.0, (box[3] - box[1]) * grow)
    x1, y1, x2, y2 = cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2

    # Spotlight: dim everything outside a soft-edged copy of the box.
    q = 4
    mask = Image.new("L", (W // q, H // q), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(x1 - 14) / q, (y1 - 14) / q, (x2 + 14) / q, (y2 + 14) / q],
        radius=(min(bw, bh) * 0.12 + 14) / q, fill=255,
    )
    mask = mask.filter(ImageFilter.GaussianBlur(5)).resize((W, H), Image.BILINEAR)
    dimmed = ImageEnhance.Brightness(frame).enhance(1 - 0.5 * alpha)
    out = Image.composite(frame, dimmed, mask).convert("RGBA")

    _paste(out, _with_alpha(_ring(int(bw), int(bh)), alpha), int(x1) - 10, int(y1) - 10)

    tag = _with_alpha(label, alpha)
    lx = int(min(max(12, cx - tag.width / 2), W - tag.width - 12))
    ly = int(y1 - tag.height - 18)
    if ly < 110:
        # No room above (the badge is there): go below, or inside the box when the caption is below.
        ly = int(y2 + 18)
        if ly + tag.height > H - 280:
            ly = int(y2 - tag.height - 16)
    out.alpha_composite(tag, (lx, ly))
    return out.convert("RGB")


def _paste(canvas: Image.Image, img: Image.Image, x: int, y: int) -> None:
    """Alpha-composite img at (x, y), dropping whatever falls outside the canvas."""
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(img.width, W - x), min(img.height, H - y)
    if right > left and bottom > top:
        canvas.alpha_composite(img, (max(0, x), max(0, y)), (left, top, right, bottom))


def render_end_card(out: Path, product: str, handles: list[str], duration: float = 1.8) -> Path:
    img = Image.new("RGB", (W, H), (12, 12, 16))
    d = ImageDraw.Draw(img)
    d.text((W / 2, H / 2 - 150), "AS SEEN ON", font=_font(30), fill=(170, 170, 180), anchor="mm")
    d.text((W / 2, H / 2 - 104), "YouTube Shorts  ·  TikTok", font=_font(30), fill=(170, 170, 180), anchor="mm")

    words, lines, line = product.split(), [], ""
    title_font = _font(64)
    for word in words:
        trial = f"{line} {word}".strip()
        if title_font.getlength(trial) > W - 100 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    y = H / 2 - 20
    for text in lines[:3]:
        text, font = _fit(text, 64, W - 100)
        d.text((W / 2, y), text, font=font, fill=ACCENT, anchor="mm")
        y += 78

    y += 30
    d.text((W / 2, y), f"Loved by {len(handles)} creator{'s' if len(handles) != 1 else ''}",
           font=_font(34), fill=(255, 255, 255), anchor="mm")
    y += 56
    for handle in handles[:6]:
        text, font = _fit(handle, 28, W - 120)
        d.text((W / 2, y), text, font=font, fill=(200, 200, 210), anchor="mm")
        y += 40

    still = out.with_suffix(".png")
    img.save(still)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-loop", "1", "-t", f"{duration}", "-i", str(still),
            "-f", "lavfi", "-t", f"{duration}", "-i", "anullsrc=r=44100:cl=stereo",
            "-vf", f"fade=t=in:d=0.25,format=yuv420p", *ENCODE, *AUDIO, "-t", f"{duration}", str(out),
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
