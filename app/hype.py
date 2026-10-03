"""Hype cut: a beat-synced ad with camera moves, a tracked product glow, kinetic type and
synthesized sound, rendered locally with OpenCV.

    uv run python -m app.hype plan.json out.mp4

The plan names the shots; everything else (camera, transitions, overlays, music, effects) is
decided here. All timing hangs off a 120 BPM grid so cuts, text pops and sounds land together.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS, SR = 1080, 1920, 30, 44100
BEAT = 0.5  # 120 BPM
ACCENT = (10, 214, 255)  # BGR
INK = (16, 12, 12)
DISPLAY = ("/System/Library/Fonts/Supplemental/Futura.ttc", 4)  # Condensed ExtraBold
HEAVY = ("/System/Library/Fonts/Avenir Next.ttc", 8)
DEMI = ("/System/Library/Fonts/Avenir Next.ttc", 2)
PLATFORM = {"youtube": ("YouTube Shorts", (51, 0, 255)), "tiktok": ("TikTok", (238, 244, 37))}

INTRO_SHOT = BEAT
RECAP_SHOT = BEAT / 2
END_SECONDS = 3.5
PUNCH = 2 * BEAT  # the beat inside each clip where the camera punches in
TRANSITIONS = ["whip_left", "whip_up", "zoom", "whip_right", "whip_down"]


# --- small helpers -------------------------------------------------------------------------


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ease_out(x: float) -> float:
    return 1 - (1 - clamp(x)) ** 3


def ease_in(x: float) -> float:
    return clamp(x) ** 3


def back_out(x: float, s: float = 1.9) -> float:
    """Overshoots past 1 and settles, for pops."""
    x = clamp(x) - 1
    return x * x * ((s + 1) * x + s) + 1


def compact(n: float | None) -> str:
    if not n:
        return "0"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= limit:
            value = n / limit
            return (f"{value:.0f}" if value >= 100 else f"{value:.1f}".rstrip("0").rstrip(".")) + suffix
    return str(int(n))


def font(spec: tuple[str, int], size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(spec[0], size, index=spec[1])


def to_bgra(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGBA2BGRA)


def rgb(bgr: tuple[int, int, int]) -> tuple[int, int, int]:
    return bgr[2], bgr[1], bgr[0]


def text_sprite(
    text: str, spec, size: int, fill=(255, 255, 255), stroke: int = 0, box: tuple | None = None,
    max_width: int | None = None, pad: int = 0,
) -> np.ndarray:
    """Text as a BGRA sprite, optionally outlined or on a filled box, shrunk to fit max_width."""
    f = font(spec, size)
    while max_width and f.getlength(text) + 2 * (pad + stroke) > max_width and size > 20:
        size -= 4
        f = font(spec, size)
    left, top, right, bottom = f.getbbox(text, stroke_width=stroke)
    w, h = int(right - left) + 2 * pad + 8, int(bottom - top) + 2 * pad + 8
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if box:
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(h * 0.18), fill=box)
    d.text((pad + 4 - left, pad + 4 - top), text, font=f, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
    return to_bgra(img)


def blit(dst: np.ndarray, spr: np.ndarray, cx: float, cy: float, alpha: float = 1.0,
         scale: float = 1.0, angle: float = 0.0) -> None:
    """Alpha-composite a BGRA sprite centred at (cx, cy), with optional scale and rotation."""
    if alpha <= 0.004 or scale <= 0.01:
        return
    if scale != 1.0 or angle:
        h, w = spr.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, scale)
        cos, sin = abs(m[0, 0]), abs(m[0, 1])
        nw, nh = int(w * cos + h * sin) + 2, int(w * sin + h * cos) + 2
        m[0, 2] += nw / 2 - w / 2
        m[1, 2] += nh / 2 - h / 2
        spr = cv2.warpAffine(spr, m, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0, 0))
    h, w = spr.shape[:2]
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    sx0, sy0 = max(0, -x0), max(0, -y0)
    sx1, sy1 = min(w, dst.shape[1] - x0), min(h, dst.shape[0] - y0)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    part = spr[sy0:sy1, sx0:sx1]
    a = part[:, :, 3:4].astype(np.float32) * (alpha / 255.0)
    region = dst[y0 + sy0:y0 + sy1, x0 + sx0:x0 + sx1]
    region[:] = (region * (1 - a) + part[:, :, :3] * a).astype(np.uint8)


def probe(src: str) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", src], capture_output=True, check=True, text=True,
    ).stdout.strip().split(",")
    return int(out[0]), int(out[1])


def decode(src: str, start: float, seconds: float) -> np.ndarray:
    """Frames of the stretch as an (n, h, w, 3) BGR array at the output frame rate."""
    w, h = probe(src)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{seconds + 0.2:.3f}", "-i", src,
         "-vf", f"fps={FPS}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True, check=True,
    ).stdout
    n = len(raw) // (w * h * 3)
    return np.frombuffer(raw[: n * w * h * 3], np.uint8).reshape(n, h, w, 3)


def voice(src: str, start: float, seconds: float) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", src,
         "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True,
    ).stdout
    audio = np.frombuffer(raw, np.float32).copy()
    fade = int(0.04 * SR)
    if len(audio) > 2 * fade:
        audio[:fade] *= np.linspace(0, 1, fade)
        audio[-fade:] *= np.linspace(1, 0, fade)
    return audio


# --- sound ---------------------------------------------------------------------------------


def _t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * SR)) / SR


def _noise(seconds: float, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).uniform(-1, 1, int(seconds * SR)).astype(np.float32)


def _lowpass(x: np.ndarray, width: int) -> np.ndarray:
    return np.convolve(x, np.hanning(width) / np.hanning(width).sum(), mode="same")


def kick() -> np.ndarray:
    t = _t(0.36)
    freq = 46 + 120 * np.exp(-t / 0.03)
    body = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t / 0.13)
    return body + 0.25 * _noise(0.36, 1) * np.exp(-t / 0.004)


def clap() -> np.ndarray:
    t = _t(0.22)
    bright = _noise(0.22, 2) - _lowpass(_noise(0.22, 2), 24)
    bursts = sum(np.exp(-np.clip(t - d, 0, None) / 0.012) * (t >= d) for d in (0.0, 0.011, 0.023))
    return bright * (0.4 * bursts + np.exp(-t / 0.07) * (t >= 0.023))


def hat() -> np.ndarray:
    t = _t(0.06)
    n = _noise(0.06, 3)
    return (n - _lowpass(n, 8)) * np.exp(-t / 0.014)


def tone(freq: float, seconds: float, decay: float, harmonics=(1.0, 0.5, 0.25), attack: float = 0.004) -> np.ndarray:
    t = _t(seconds)
    wave = sum(a * np.sin(2 * np.pi * freq * (i + 1) * t) for i, a in enumerate(harmonics))
    return wave * np.exp(-t / decay) * np.clip(t / attack, 0, 1)


def whoosh(seconds: float = 0.5, seed: int = 5) -> np.ndarray:
    """Noise that brightens then darkens while it swells: something flying past."""
    t = _t(seconds)
    n = _noise(seconds, seed)
    dark = _lowpass(n, 90) * 3
    bright = n - _lowpass(n, 14)
    x = t / seconds
    swell = np.sin(np.pi * x) ** 2
    mix = np.sin(np.pi * x) ** 3
    return (dark * (1 - mix) + bright * mix * 0.8) * swell


def impact() -> np.ndarray:
    t = _t(0.9)
    freq = 34 + 70 * np.exp(-t / 0.06)
    sub = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t / 0.28)
    thud = _lowpass(_noise(0.9, 7), 60) * 4 * np.exp(-t / 0.09)
    return sub + 0.6 * thud + 0.3 * _noise(0.9, 8) * np.exp(-t / 0.006)


def riser(seconds: float) -> np.ndarray:
    t = _t(seconds)
    x = t / seconds
    n = _noise(seconds, 9)
    air = (n - _lowpass(n, 20)) * x**2
    sweep = np.sin(2 * np.pi * np.cumsum(180 * (12 ** x)) / SR) * x**1.5 * 0.4
    return air * 0.7 + sweep


def pop(freq: float = 920) -> np.ndarray:
    t = _t(0.07)
    return np.sin(2 * np.pi * np.cumsum(freq * (1 + 0.8 * np.exp(-t / 0.012))) / SR) * np.exp(-t / 0.02)


def shimmer() -> np.ndarray:
    return sum(tone(f, 1.6, 0.5, (1.0, 0.2)) * g for f, g in ((1760, 0.5), (2217, 0.4), (2637, 0.35), (3520, 0.25)))


class Mixer:
    def __init__(self, seconds: float) -> None:
        self.buf = np.zeros((int((seconds + 2) * SR), 2), np.float32)

    def add(self, t: float, sound: np.ndarray, gain: float = 1.0, pan: float = 0.0) -> None:
        i = max(0, int(t * SR))
        sound = sound[: len(self.buf) - i].astype(np.float32) * gain
        self.buf[i:i + len(sound), 0] += sound * (1 - max(0.0, pan))
        self.buf[i:i + len(sound), 1] += sound * (1 + min(0.0, pan))

    def master(self, seconds: float) -> np.ndarray:
        out = np.tanh(self.buf[: int(seconds * SR)] * 0.8)
        return out / max(1e-6, np.abs(out).max()) * 0.8


# Am, F, C, G: bass root and three chord tones per bar.
BARS = [
    (55.00, (220.00, 261.63, 329.63)),
    (43.65, (174.61, 220.00, 261.63)),
    (65.41, (261.63, 329.63, 392.00)),
    (49.00, (196.00, 246.94, 293.66)),
]


def music(mixer: Mixer, start: float, end: float, drop: float) -> None:
    """A simple four-on-the-floor loop from `start`; after `drop` it thins out under the end card."""
    k, c, h = kick(), clap(), hat()
    arp = [0, 1, 2, 1, 0, 2, 1, 2]
    beat = 0
    t = start
    while t < end - 0.01:
        bar = BARS[(beat // 4) % 4]
        full = t < drop
        if full or beat % 2 == 0:
            mixer.add(t, k, 0.6 if full else 0.4)
        if full and beat % 4 in (1, 3):
            mixer.add(t, c, 0.42)
        if not full:
            mixer.add(t + BEAT / 2, h, 0.1, pan=0.3)
        if full:
            mixer.add(t + BEAT / 2, h, 0.2, pan=0.3)
            mixer.add(t + BEAT / 4, h, 0.09, pan=-0.3)
            mixer.add(t + 3 * BEAT / 4, h, 0.09, pan=-0.3)
            # Off-beat bass leaves room for the kick.
            mixer.add(t + BEAT / 2, tone(bar[0], 0.24, 0.16, (1.0, 0.6, 0.3, 0.15)), 0.5)
            mixer.add(t + BEAT / 2, tone(bar[0] * 2, 0.24, 0.12, (1.0, 0.3)), 0.2)
        for step in range(4):
            note = bar[1][arp[(beat * 4 + step) % 8]] * 2
            mixer.add(t + step * BEAT / 4, tone(note, 0.3, 0.09 if full else 0.3, (1.0, 0.4, 0.15)),
                      0.2 if full else 0.2, pan=0.5 if step % 2 else -0.5)
        if beat % 4 == 0:
            for f in bar[1]:
                mixer.add(t, tone(f, 4 * BEAT, 1.4, (1.0, 0.3, 0.1), attack=0.15), 0.12 if full else 0.16)
        beat += 1
        t += BEAT


# --- picture -------------------------------------------------------------------------------


def camera_matrix(sw: int, sh: int, zoom: float, focus: tuple[float, float], angle: float,
                  shift: tuple[float, float]) -> np.ndarray:
    """Affine map from source pixels to the output frame: cover-fit, then zoom about `focus`."""
    scale = max(W / sw, H / sh) * zoom
    # Keep the focus far enough from the edges that the frame stays covered.
    fx = clamp(focus[0], W / (2 * scale), sw - W / (2 * scale))
    fy = clamp(focus[1], H / (2 * scale), sh - H / (2 * scale))
    m = cv2.getRotationMatrix2D((fx, fy), angle, scale)
    m[0, 2] += W / 2 - fx + shift[0]
    m[1, 2] += H / 2 - fy + shift[1]
    return m


def radial_blur(src: np.ndarray, m: np.ndarray, strength: float) -> np.ndarray:
    acc = np.zeros((H, W, 3), np.float32)
    steps = 6
    for i in range(steps):
        s = 1 + strength * 0.05 * i
        mi = m.copy()
        mi[:, :2] *= s
        mi[0, 2] = m[0, 2] * s - (s - 1) * W / 2
        mi[1, 2] = m[1, 2] * s - (s - 1) * H / 2
        acc += cv2.warpAffine(src, mi, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
    return (acc / steps).astype(np.uint8)


def transition(kind: str, p: float, incoming: bool) -> tuple[tuple[float, float], float, tuple[int, int], float]:
    """Camera shift, extra zoom, motion-blur kernel and radial-blur strength at progress p (0..1)."""
    if kind == "zoom":
        return (0.0, 0.0), 1 + (1.3 if incoming else 1.8) * p, (1, 1), 2.5 * p
    dx, dy = {"whip_left": (-1, 0), "whip_right": (1, 0), "whip_up": (0, -1), "whip_down": (0, 1)}[kind]
    sign = -1 if incoming else 1
    travel = 0.85 * p * sign
    blur = int(150 * p) | 1
    return (dx * travel * W, dy * travel * H), 1.0, (blur if dx else 1, blur if dy else 1), 0.0


def grade(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    frame = cv2.addWeighted(frame, 1.18, gray, -0.18, 0)
    return cv2.convertScaleAbs(frame, alpha=1.07, beta=-7)


def scrim_rows() -> np.ndarray:
    y = np.arange(H, dtype=np.float32)
    top = np.clip(1 - y / 420, 0, 1) ** 1.5 * 0.55
    bottom = np.clip((y - (H - 760)) / 760, 0, 1) ** 1.4 * 0.7
    return (1 - np.maximum(top, bottom)).reshape(H, 1, 1)


def ring_points(box: tuple[float, float, float, float], n: int = 240) -> np.ndarray:
    """Points along a rounded rectangle, clockwise from the top-left corner."""
    x1, y1, x2, y2 = box
    r = min(x2 - x1, y2 - y1) * 0.16
    pts = []
    for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        for a in np.linspace(a0, a0 + 90, n // 4, endpoint=False):
            pts.append((cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))))
    return np.array(pts, np.float32)


def star(layer: np.ndarray, x: float, y: float, size: float, color) -> None:
    p = (int(x), int(y))
    cv2.line(layer, (p[0] - int(size), p[1]), (p[0] + int(size), p[1]), color, 2, cv2.LINE_AA)
    cv2.line(layer, (p[0], p[1] - int(size)), (p[0], p[1] + int(size)), color, 2, cv2.LINE_AA)
    cv2.circle(layer, p, max(1, int(size * 0.3)), color, -1, cv2.LINE_AA)


def highlight(frame: np.ndarray, box, t: float, seed: int, dim: float = 0.5) -> np.ndarray:
    """Spotlight, glowing ring that draws itself on, a comet running round it, and sparkles."""
    x1, y1, x2, y2 = box
    reveal = ease_out((t - 0.12) / 0.45)
    if reveal <= 0:
        return frame

    # Spotlight: darken away from the product.
    q = 8
    mask = np.zeros((H // q, W // q), np.float32)
    pad = 40
    cv2.rectangle(mask, (int((x1 - pad) / q), int((y1 - pad) / q)), (int((x2 + pad) / q), int((y2 + pad) / q)), 1.0, -1)
    mask = cv2.resize(cv2.GaussianBlur(mask, (0, 0), 9), (W, H), interpolation=cv2.INTER_LINEAR)
    light = 1 - dim * reveal * (1 - mask)
    frame = (frame * light[:, :, None]).astype(np.uint8)

    pts = ring_points(box)
    n = len(pts)
    layer = np.zeros((H, W, 3), np.uint8)
    # Two strokes start at opposite corners and meet.
    count = int(n / 2 * reveal)
    for offset in (0, n // 2):
        seg = np.roll(pts, -offset, axis=0)[: count + 1].astype(np.int32)
        if len(seg) > 1:
            cv2.polylines(layer, [seg], False, ACCENT, 7, cv2.LINE_AA)
    if reveal >= 1:
        head = int((t * 0.9 % 1) * n)
        comet = np.roll(pts, -head, axis=0)[:26].astype(np.int32)
        cv2.polylines(layer, [comet], False, (255, 255, 255), 9, cv2.LINE_AA)
        pulse = 0.5 + 0.5 * math.sin(t * 9)
    else:
        pulse = 1.0

    # Sparkles burst from the ring as it closes, then twinkle.
    rng = np.random.default_rng(seed)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    for i in range(22):
        anchor = pts[rng.integers(0, n)]
        born = 0.35 + rng.uniform(0, 0.9)
        age = t - born
        if not 0 < age < 0.7:
            continue
        direction = (anchor - (cx, cy)) / (np.linalg.norm(anchor - (cx, cy)) + 1e-6)
        pos = anchor + direction * (30 + 120 * ease_out(age / 0.7))
        star(layer, pos[0], pos[1], 16 * (1 - age / 0.7) + 3, (255, 255, 255) if i % 3 else ACCENT)

    small = cv2.resize(layer, (W // 4, H // 4), interpolation=cv2.INTER_AREA)
    glow = cv2.resize(cv2.GaussianBlur(small, (0, 0), 7), (W, H), interpolation=cv2.INTER_LINEAR)
    frame = cv2.add(frame, cv2.convertScaleAbs(glow, alpha=1.3 + 0.5 * pulse))
    return cv2.add(frame, layer)


def shine(frame: np.ndarray, box, t: float) -> np.ndarray:
    """A diagonal band of light crossing the product once, right after the punch-in."""
    p = (t - PUNCH - 0.1) / 0.45
    if not 0 < p < 1:
        return frame
    x1, y1, x2, y2 = (int(v) for v in box)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return frame
    h, w = y2 - y1, x2 - x1
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    band = np.exp(-(((xx + yy * 0.5) - (-0.3 * w + p * (1.6 * w + 0.5 * h))) / (0.12 * w)) ** 2)
    region = frame[y1:y2, x1:x2].astype(np.float32)
    frame[y1:y2, x1:x2] = np.clip(region + band[:, :, None] * 120 * math.sin(math.pi * p), 0, 255).astype(np.uint8)
    return frame


LEAK_COLORS = [(60, 120, 255), (255, 80, 200), (255, 200, 60), (80, 255, 200), (120, 60, 255)]


def light_leak(frame: np.ndarray, t: float, index: int) -> np.ndarray:
    """A soft coloured flare that sweeps across the frame just after a cut."""
    p = t / 0.6
    if not 0 < p < 1:
        return frame
    small = np.zeros((H // 8, W // 8, 3), np.float32)
    side = index % 2
    x = (0.1 + 0.8 * p) if side else (0.9 - 0.8 * p)
    centre = (int(x * W / 8), int((0.25 + 0.5 * p) * H / 8))
    cv2.circle(small, centre, 70, LEAK_COLORS[index % len(LEAK_COLORS)], -1)
    small = cv2.GaussianBlur(small, (0, 0), 38) * (0.9 * math.sin(math.pi * p))
    leak = cv2.resize(small, (W, H), interpolation=cv2.INTER_LINEAR)
    return cv2.add(frame, leak.astype(np.uint8))


def rgb_split(frame: np.ndarray, amount: float) -> np.ndarray:
    k = int(amount)
    if k < 1:
        return frame
    out = frame.copy()
    out[:, k:, 2] = frame[:, :-k, 2]
    out[:, :-k, 0] = frame[:, k:, 0]
    return out


def flash(frame: np.ndarray, amount: float) -> np.ndarray:
    if amount <= 0.01:
        return frame
    return cv2.addWeighted(frame, 1 - amount, np.full_like(frame, 255), amount, 0)


class Grain:
    def __init__(self) -> None:
        self.tex = np.random.default_rng(11).normal(0, 2.2, (H + 64, W + 64)).astype(np.int16)

    def apply(self, frame: np.ndarray, n: int) -> np.ndarray:
        oy, ox = (n * 37) % 64, (n * 53) % 64
        g = self.tex[oy:oy + H, ox:ox + W, None]
        return np.clip(frame.astype(np.int16) + g, 0, 255).astype(np.uint8)


def box_at(boxes: list[list[float]], t: float) -> tuple[float, float, float, float] | None:
    """Product box (source fractions) at source time t, interpolated between keyframes."""
    if not boxes:
        return None
    if t <= boxes[0][0]:
        return tuple(boxes[0][1:])
    if t >= boxes[-1][0]:
        return tuple(boxes[-1][1:])
    for a, b in zip(boxes, boxes[1:]):
        if a[0] <= t <= b[0]:
            f = (t - a[0]) / max(1e-6, b[0] - a[0])
            return tuple(a[i] + (b[i] - a[i]) * f for i in range(1, 5))
    return None


def creator_card(clip: dict, progress: float) -> np.ndarray:
    """Glass card with the handle and the creator's numbers counting up to their real values."""
    label, color = PLATFORM[clip["platform"]]
    unit = "subscribers" if clip["platform"] == "youtube" else "followers"
    grow = ease_out(progress)
    parts = []
    if clip.get("followers"):
        parts.append((compact(clip["followers"] * grow), unit))
    if clip.get("views"):
        parts.append((compact(clip["views"] * grow), "views"))

    name_font, num_font, small_font = font(HEAVY, 54), font(HEAVY, 42), font(DEMI, 34)
    tag_font = font(HEAVY, 24)
    # Size the card for the final numbers so it does not wobble while they count.
    final = "   ".join(f"{compact(clip.get(k))} {u}" for k, u in (("followers", unit), ("views", "views")) if clip.get(k))
    name_w = name_font.getlength(clip["handle"]) + (150 if clip.get("official") else 0)
    w = int(max(name_w, num_font.getlength(final) + 30, 380)) + 170
    h = 170
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=40, fill=(14, 14, 20, 205), outline=(255, 255, 255, 60), width=2)
    d.ellipse([26, 37, 122, 133], fill=rgb(color))
    if clip["platform"] == "youtube":
        d.polygon([(62, 62), (62, 108), (100, 85)], fill=(255, 255, 255))
    else:
        d.ellipse([52, 92, 76, 112], fill=(14, 14, 20))
        d.rectangle([69, 56, 76, 102], fill=(14, 14, 20))
        d.arc([62, 44, 104, 86], 95, 180, fill=(14, 14, 20), width=8)
    d.text((146, 56), clip["handle"], font=name_font, fill=(255, 255, 255), anchor="lm")
    if clip.get("official"):
        tx = 146 + name_font.getlength(clip["handle"]) + 18
        d.rounded_rectangle([tx, 36, tx + 124, 76], radius=20, fill=rgb(ACCENT))
        d.text((tx + 62, 56), "OFFICIAL", font=tag_font, fill=(14, 14, 20), anchor="mm")
    x = 146
    for number, word in parts:
        d.text((x, 116), number, font=num_font, fill=rgb(ACCENT), anchor="lm")
        x += num_font.getlength(number) + 10
        d.text((x, 118), word, font=small_font, fill=(220, 220, 230), anchor="lm")
        x += small_font.getlength(word) + 30
    return to_bgra(img)


def progress_bars(frame: np.ndarray, index: int, total: int, fraction: float) -> None:
    gap, margin, y = 10, 40, 56
    seg = (W - 2 * margin - gap * (total - 1)) / total
    overlay = frame.copy()
    for i in range(total):
        x = int(margin + i * (seg + gap))
        cv2.rectangle(overlay, (x, y), (int(x + seg), y + 8), (255, 255, 255), -1)
    cv2.addWeighted(overlay, 0.3, frame, 0.7, 0, frame)
    for i in range(total):
        filled = 1.0 if i < index else (fraction if i == index else 0.0)
        if filled > 0:
            x = int(margin + i * (seg + gap))
            cv2.rectangle(frame, (x, y), (int(x + seg * filled), y + 8), (255, 255, 255), -1)


class Renderer:
    def __init__(self, plan: dict, out: Path) -> None:
        self.plan = plan
        self.out = out
        clips = plan["clips"]
        self.t_intro = len(plan["intro"]) * INTRO_SHOT
        self.t_recap = self.t_intro + sum(c["seconds"] for c in clips)
        self.t_end = self.t_recap + len(plan["recap"]) * RECAP_SHOT
        self.total = self.t_end + END_SECONDS
        self.mixer = Mixer(self.total)
        self.scrim = scrim_rows()
        self.grain = Grain()
        self.n = 0
        self.encoder = subprocess.Popen(
            ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
             "-r", str(FPS), "-i", "pipe:0", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
             "-pix_fmt", "yuv420p", str(out.with_suffix(".video.mp4"))],
            stdin=subprocess.PIPE,
        )

    @property
    def now(self) -> float:
        return self.n / FPS

    def emit(self, frame: np.ndarray) -> None:
        self.encoder.stdin.write(self.grain.apply(frame, self.n).tobytes())
        self.n += 1

    # -- intro: three slammed words over three shots --

    def intro(self) -> None:
        for i, shot in enumerate(self.plan["intro"]):
            frames = decode(shot["src"], shot["start"], INTRO_SHOT)
            sh, sw = frames.shape[1:3]
            last = i == len(self.plan["intro"]) - 1
            word = text_sprite(shot["word"], DISPLAY, 250, fill=rgb(ACCENT) if last else (255, 255, 255),
                               stroke=10, max_width=W - 80)
            self.mixer.add(self.now, impact(), 0.5 if i else 0.65)
            self.mixer.add(self.now, pop(520 + 160 * i), 0.3)
            count = round(INTRO_SHOT * FPS)
            for k in range(count):
                t = k / FPS
                shake = 22 * math.exp(-t / 0.07)
                m = camera_matrix(sw, sh, 1.1 + 0.22 * ease_out(t / INTRO_SHOT), (sw / 2, sh * 0.45),
                                  2.0 * math.exp(-t / 0.1) * (-1) ** i,
                                  (shake * math.sin(k * 2.4), shake * math.cos(k * 3.1)))
                frame = cv2.warpAffine(frames[min(k, len(frames) - 1)], m, (W, H), flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_REFLECT_101)
                frame = cv2.convertScaleAbs(grade(frame), alpha=0.55)
                frame = rgb_split(frame, 18 * math.exp(-t / 0.08))
                blit(frame, word, W / 2, H * 0.47, 1.0, 0.55 + 0.45 * back_out(t / 0.18), -3 + 6 * (i % 2))
                self.emit(flash(frame, 0.85 * math.exp(-t / 0.05)))

    # -- creator clips --

    def clip(self, index: int, clip: dict) -> None:
        clips = self.plan["clips"]
        seconds = clip["seconds"]
        count = round(seconds * FPS)
        kind_in = TRANSITIONS[(index - 1) % len(TRANSITIONS)]
        kind_out = TRANSITIONS[index % len(TRANSITIONS)]
        last = index == len(clips) - 1
        t0 = self.now

        # Sound: the creator's own voice, a hit on the cut, a whoosh out, pops under the caption.
        self.mixer.add(t0, impact(), 0.32)
        offset = 0.0
        for shot in clip["shots"]:
            self.mixer.add(t0 + offset, voice(shot["src"], shot["start"], shot["end"] - shot["start"]), 0.75)
            offset += shot["end"] - shot["start"]
        self.mixer.add(t0 + PUNCH, kick(), 0.5)
        self.mixer.add(t0 + seconds - 0.3, whoosh(0.55, seed=20 + index), 0.6, pan=0.6 * (-1) ** index)

        words = clip["caption"].upper().split()
        word_sprites = [
            text_sprite(w, DISPLAY, 150, fill=(14, 14, 20) if i == len(words) - 1 else (255, 255, 255),
                        stroke=0 if i == len(words) - 1 else 8,
                        box=rgb(ACCENT) + (255,) if i == len(words) - 1 else None, pad=14, max_width=W - 120)
            for i, w in enumerate(words)
        ]
        word_times = [PUNCH + 0.02 + i * BEAT / 2 for i in range(len(words))]
        for wt in word_times:
            self.mixer.add(t0 + wt, pop(), 0.28)
        # Lay the words out on one or two centred lines.
        lines, line, width = [], [], 0
        for spr in word_sprites:
            if line and width + spr.shape[1] + 24 > W - 100:
                lines.append(line)
                line, width = [], 0
            line.append(spr)
            width += spr.shape[1] + 24
        lines.append(line)

        tag = text_sprite(self.plan["tag"], HEAVY, 34, fill=(14, 14, 20), box=rgb(ACCENT) + (255,), pad=14)
        card_done = None
        k = 0
        smooth = None
        focus = None
        for shot_index, shot in enumerate(clip["shots"]):
            frames = decode(shot["src"], shot["start"], shot["end"] - shot["start"])
            sh, sw = frames.shape[1:3]
            shot_frames = round((shot["end"] - shot["start"]) * FPS)
            cut_time = k / FPS
            if shot_index:
                smooth = focus = None
                self.mixer.add(t0 + cut_time, impact(), 0.4)
            for j in range(shot_frames):
                t = k / FPS
                since_cut = t - cut_time
                src_box = box_at(shot.get("boxes") or [], shot["start"] + j / FPS)
                if src_box:
                    px = (src_box[0] * sw, src_box[1] * sh, src_box[2] * sw, src_box[3] * sh)
                    smooth = px if smooth is None else tuple(s + (p - s) * 0.4 for s, p in zip(smooth, px))
                centre = ((smooth[0] + smooth[2]) / 2, (smooth[1] + smooth[3]) / 2) if smooth else (sw / 2, sh / 2)
                focus = centre if focus is None else tuple(f + (c - f) * 0.15 for f, c in zip(focus, centre))

                # Camera: settle in, drift forward, punch towards the product on the beat.
                punch = ease_out((t - PUNCH) / 0.16)
                zoom = 1.0 + 0.14 * (1 - ease_out(since_cut / 0.3)) + 0.08 * t / seconds + 0.2 * punch
                pull = 0.3 + 0.7 * punch
                look = (sw / 2 + (focus[0] - sw / 2) * pull, sh / 2 + (focus[1] - sh / 2) * pull)
                shake = 18 * math.exp(-since_cut / 0.08) + 14 * math.exp(-(t - PUNCH) / 0.07) * (t >= PUNCH)
                shift = (shake * math.sin(k * 2.7), shake * math.cos(k * 3.3))
                angle = 1.4 * math.exp(-since_cut / 0.14) * math.sin(since_cut * 34) + 0.7 * math.sin(t * 1.7 + index)

                blur, radial = (1, 1), 0.0
                edge = 5
                if k < edge and index > 0:
                    ts, tz, blur, radial = transition(kind_in, 1 - ease_out(k / edge), True)
                    shift, zoom = (shift[0] + ts[0], shift[1] + ts[1]), zoom * tz
                elif k >= count - edge and not last:
                    ts, tz, blur, radial = transition(kind_out, ease_in((k - (count - edge) + 1) / edge), False)
                    shift, zoom = (shift[0] + ts[0], shift[1] + ts[1]), zoom * tz

                m = camera_matrix(sw, sh, zoom, look, angle, shift)
                src = frames[min(j, len(frames) - 1)]
                if radial > 0.05:
                    frame = radial_blur(src, m, radial)
                else:
                    frame = cv2.warpAffine(src, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
                if blur != (1, 1):
                    frame = cv2.blur(frame, blur)
                frame = grade(frame)

                moving = blur != (1, 1) or radial > 0.05
                if smooth and not moving:
                    corners = np.array([[smooth[0], smooth[1], 1], [smooth[2], smooth[3], 1]], np.float32) @ m.T
                    out_box = (corners[0][0], corners[0][1], corners[1][0], corners[1][1])
                    frame = highlight(frame, out_box, since_cut, seed=index * 10 + shot_index)
                    frame = shine(frame, out_box, t)
                    # Product tag on a leader line from the ring's corner.
                    show = ease_out((since_cut - 0.55) / 0.3)
                    if show > 0 and shot_index == 0:
                        ax, ay = out_box[2], out_box[1]
                        left = ax > W - tag.shape[1] - 60
                        tx = (out_box[0] - 30 - tag.shape[1] / 2) if left else (ax + 30 + tag.shape[1] / 2)
                        tx = clamp(tx, tag.shape[1] / 2 + 20, W - tag.shape[1] / 2 - 20)
                        ty = clamp(ay - 50, 330, H - 800)
                        blit(frame, tag, tx, ty, show, 0.7 + 0.3 * back_out(show))

                frame = light_leak(frame, since_cut, index + shot_index)
                frame = (frame * self.scrim).astype(np.uint8)
                progress_bars(frame, index, len(clips), (k + 1) / count)

                slide = back_out((t - 0.1) / 0.45, 1.2)
                counting = (t - 0.3) / 0.9
                if counting < 1:
                    card = creator_card(clip, counting)
                else:
                    card = card_done = card_done if card_done is not None else creator_card(clip, 1)
                blit(frame, card, -card.shape[1] / 2 + (40 + card.shape[1]) * slide, 190, clamp((t - 0.1) / 0.2))

                y = H - 330 - (len(lines) - 1) * 190
                seen = 0
                for row in lines:
                    row_w = sum(s.shape[1] for s in row) + 24 * (len(row) - 1)
                    x = (W - row_w) / 2
                    for spr in row:
                        age = t - word_times[seen]
                        if age > 0:
                            blit(frame, spr, x + spr.shape[1] / 2, y, clamp(age / 0.08),
                                 0.4 + 0.6 * back_out(age / 0.2), -2.5 + 5 * (seen % 2))
                        x += spr.shape[1] + 24
                        seen += 1
                    y += 190

                frame = rgb_split(frame, 14 * math.exp(-since_cut / 0.07) + 10 * math.exp(-(t - PUNCH) / 0.06) * (t >= PUNCH))
                frame = flash(frame, 0.55 * math.exp(-since_cut / 0.05))
                self.emit(frame)
                k += 1

    # -- recap: a burst of very short cuts under a riser --

    def recap(self) -> None:
        shots = self.plan["recap"]
        self.mixer.add(self.now - 0.5, riser(0.5 + len(shots) * RECAP_SHOT), 0.5)
        for i, shot in enumerate(shots):
            frames = decode(shot["src"], shot["start"], RECAP_SHOT)
            sh, sw = frames.shape[1:3]
            self.mixer.add(self.now, clap(), 0.35 + 0.08 * i)
            count = round(RECAP_SHOT * FPS)
            for k in range(count):
                t = k / FPS
                m = camera_matrix(sw, sh, 1.15 + 0.5 * ease_in(t / RECAP_SHOT) * (i == len(shots) - 1) + 0.1 * t,
                                  (sw / 2, sh * 0.45), 3 * (-1) ** i * (1 - t / RECAP_SHOT), (0, 0))
                frame = grade(cv2.warpAffine(frames[min(k, len(frames) - 1)], m, (W, H), flags=cv2.INTER_LINEAR,
                                             borderMode=cv2.BORDER_REFLECT_101))
                frame = rgb_split(frame, 16 * math.exp(-t / 0.05))
                self.emit(flash(frame, 0.7 * math.exp(-t / 0.04)))

    # -- end card --

    def end(self) -> None:
        plan = self.plan
        hero = plan["hero"]
        frames = decode(hero["src"], hero["start"], min(END_SECONDS, hero["end"] - hero["start"]))
        sh, sw = frames.shape[1:3]
        clips = plan["clips"]
        followers = sum(c.get("followers") or 0 for c in clips)
        views = sum(c.get("views") or 0 for c in clips)
        t0 = self.now
        self.mixer.add(t0, impact(), 0.7)
        self.mixer.add(t0, shimmer(), 0.22)
        for i in range(14):
            self.mixer.add(t0 + 0.7 + i * 0.06, pop(1500 + 40 * i), 0.1)
        self.mixer.add(t0 + 1.9, shimmer(), 0.3)
        self.mixer.add(t0 + 1.9, pop(660), 0.4)

        seen = text_sprite("AS SEEN ON  TIKTOK  ·  YOUTUBE SHORTS", HEAVY, 34, fill=(215, 215, 225))
        name = [text_sprite(line, DISPLAY, 150, fill=(255, 255, 255), max_width=W - 100) for line in plan["product_lines"]]
        handles = text_sprite("   ".join(c["handle"] for c in clips), DEMI, 38, fill=(200, 200, 212), max_width=W - 80)
        cta = text_sprite(plan["cta"], HEAVY, 72, fill=(14, 14, 20), box=rgb(ACCENT) + (255,), pad=34)
        labels = [text_sprite(s, HEAVY, 36, fill=(200, 200, 212)) for s in ("CREATORS", "FOLLOWERS", "VIEWS")]
        win_w, win_h = 620, 800
        win_cx, win_cy = W / 2, 560

        count = round(END_SECONDS * FPS)
        for k in range(count):
            t = k / FPS
            src = frames[min(k, len(frames) - 1)]
            # Background: the hero shot, blurred and dark, drifting.
            m = camera_matrix(sw, sh, 1.25 + 0.05 * t, (sw / 2, sh / 2), 0, (0, 0))
            bg = cv2.warpAffine(src, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
            bg = cv2.resize(cv2.GaussianBlur(cv2.resize(bg, (W // 6, H // 6)), (0, 0), 9), (W, H))
            frame = cv2.convertScaleAbs(grade(bg), alpha=0.42)

            # Hero window: the product shot playing in a rounded frame.
            grow = back_out(t / 0.5, 1.4)
            ww, wh = int(win_w * grow), int(win_h * grow)
            if ww > 20:
                box = box_at(hero.get("boxes") or [], hero["start"] + t)
                centre = ((box[0] + box[2]) / 2 * sw, (box[1] + box[3]) / 2 * sh) if box else (sw / 2, sh / 2)
                scale = max(ww / sw, wh / sh) * (1.12 + 0.04 * t)
                fx = clamp(centre[0], ww / (2 * scale), sw - ww / (2 * scale))
                fy = clamp(centre[1], wh / (2 * scale), sh - wh / (2 * scale))
                wm = np.array([[scale, 0, ww / 2 - fx * scale], [0, scale, wh / 2 - fy * scale]], np.float32)
                window = grade(cv2.warpAffine(src, wm, (ww, wh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101))
                mask = Image.new("L", (ww, wh), 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, ww - 1, wh - 1], radius=56, fill=255)
                sprite = np.dstack([window, np.asarray(mask)])
                x1, y1 = win_cx - ww / 2, win_cy - wh / 2
                frame = highlight(frame, (x1, y1, x1 + ww, y1 + wh), t + 0.3, seed=99, dim=0.0)
                blit(frame, sprite, win_cx, win_cy)

            blit(frame, seen, W / 2, 110, ease_out((t - 0.1) / 0.3))
            y = 1090
            for i, spr in enumerate(name):
                a = ease_out((t - 0.25 - 0.08 * i) / 0.35)
                blit(frame, spr, W / 2, y + 60 * (1 - a), a)
                y += 150

            # The three numbers count up together.
            grow_n = ease_out((t - 0.7) / 0.9)
            numbers = [str(max(1, round(len(clips) * grow_n))), compact(followers * grow_n), compact(views * grow_n)]
            a = ease_out((t - 0.6) / 0.3)
            for i, (num, lab) in enumerate(zip(numbers, labels)):
                x = W / 2 + (i - 1) * 345
                blit(frame, text_sprite(num, DISPLAY, 124, fill=rgb(ACCENT), max_width=320), x, 1410, a)
                blit(frame, lab, x, 1506, a)
            blit(frame, handles, W / 2, 1596, ease_out((t - 1.3) / 0.4))

            p = (t - 1.9) / 0.3
            if p > 0:
                pulse = 1 + 0.03 * math.sin((t - 1.9) * 7)
                blit(frame, cta, W / 2, 1756, clamp(p * 3), back_out(p) * pulse)
            frame = flash(frame, 0.9 * math.exp(-t / 0.07))
            fade = clamp((END_SECONDS - t) / 0.25)
            self.emit(cv2.convertScaleAbs(frame, alpha=fade))

    def run(self) -> Path:
        self.intro()
        for i, clip in enumerate(self.plan["clips"]):
            self.clip(i, clip)
        self.recap()
        self.end()
        self.encoder.stdin.close()
        if self.encoder.wait() != 0:
            raise RuntimeError("video encode failed")

        music(self.mixer, self.t_intro, self.total - 0.2, drop=self.t_end)
        audio = (self.mixer.master(self.total) * 32767).astype("<i2")
        wav = self.out.with_suffix(".wav")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0", str(wav)],
            input=audio.tobytes(), check=True,
        )
        video = self.out.with_suffix(".video.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(wav), "-c:v", "copy", "-c:a", "aac",
             "-b:a", "192k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st=%.2f:d=0.3" % (self.total - 0.3),
             "-ar", str(SR), "-shortest",
             "-movflags", "+faststart", str(self.out)],
            check=True,
        )
        video.unlink()
        wav.unlink()
        return self.out


CLIP_SECONDS = 5 * BEAT
HOOK = ["THE INTERNET", "IS OBSESSED", "WITH THIS"]
STYLE = {
    "key": "hype", "label": "Hype",
    "description": "Beat-synced cuts, camera moves, a glowing product track, kinetic captions and sound design",
}


def _two_lines(text: str) -> list[str]:
    """Split a product name into two lines of similar length."""
    words = text.upper().split()
    if len(words) < 2:
        return words or [""]
    best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    return [" ".join(words[:best]), " ".join(words[best:])]


def _tag(product: str, limit: int = 26) -> str:
    """The product name for the on-screen tag, cut at a word boundary."""
    words = product.upper().split()
    while len(words) > 1 and len(" ".join(words)) > limit:
        words.pop()
    return " ".join(words)[:limit]


def build_plan(product: str, clips: list[dict]) -> dict:
    """Everything around the chosen clips: the hook, the recap burst and the end card.

    Each clip needs src, start, end, boxes ([t, x1, y1, x2, y2] in source seconds and frame
    fractions), handle, platform, followers, views, official and caption.
    """
    def area(clip: dict) -> float:
        boxes = clip["boxes"]
        return sum((b[3] - b[1]) * (b[4] - b[2]) for b in boxes) / len(boxes) if boxes else 0.0

    hero = max(clips, key=area)  # the shot where the product fills the most frame
    return {
        "tag": _tag(product),
        "product_lines": _two_lines(product),
        "cta": "GET YOURS TODAY",
        "intro": [
            {"src": c["src"], "start": c["start"] + 0.4, "word": word}
            for word, c in zip(HOOK, (clips[i % len(clips)] for i in range(len(HOOK))))
        ],
        "clips": [
            {
                **{k: c.get(k) for k in ("handle", "platform", "followers", "views", "official")},
                "caption": c.get("caption") or "",
                "shots": [{"src": c["src"], "start": c["start"], "end": c["end"], "boxes": c["boxes"]}],
            }
            for c in clips
        ],
        "recap": [
            {"src": c["src"], "start": c["start"] + min(1.4, max(0.0, c["end"] - c["start"] - RECAP_SHOT - 0.1))}
            for c in (clips[i % len(clips)] for i in range(4))
        ],
        "hero": {"src": hero["src"], "start": hero["start"], "end": hero["end"], "boxes": hero["boxes"]},
    }


def render(plan: dict, out: Path) -> Path:
    for clip in plan["clips"]:
        clip["seconds"] = sum(s["end"] - s["start"] for s in clip["shots"])
    return Renderer(plan, out).run()


if __name__ == "__main__":
    print(render(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2])))
