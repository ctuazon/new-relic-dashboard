"""Generates the futuristic app icon (assets/app_icon.ico) used by the desktop shortcut.

Style: a dark HUD-style circular badge with a neon cyan/magenta glow ring and
an ECG-style "pulse" waveform through the middle, evoking anomaly monitoring.
Re-run this after editing to regenerate the .ico:
    .venv\\Scripts\\python.exe assets\\generate_icon.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parent
OUT_PATH = OUT_DIR / "app_icon.ico"

FINAL = 256          # final base resolution
HI = FINAL * 4        # high-res working canvas for crisp downsampled linework
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]

BG_CENTER = (12, 20, 40, 255)
BG_EDGE = (2, 4, 10, 255)
CYAN = (70, 240, 255, 255)
CYAN_GLOW = (40, 220, 255, 140)
MAGENTA_GLOW = (255, 20, 190, 110)
PULSE = (110, 255, 235, 255)
PULSE_GLOW = (60, 255, 220, 160)
NODE = (255, 255, 255, 255)


def make_background(size):
    """Cheap radial gradient (drawn at final resolution; gradients don't need AA)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    cx = cy = size / 2
    r = size / 2
    for y in range(size):
        dy = y - cy
        for x in range(size):
            dx = x - cx
            d = math.hypot(dx, dy)
            if d > r:
                continue
            t = d / r
            a = 255 if d < r - 1.5 else max(0, int(255 * (r - d)))
            col = tuple(int(BG_CENTER[i] + (BG_EDGE[i] - BG_CENTER[i]) * t) for i in range(3)) + (a,)
            px[x, y] = col
    return img


def make_linework(hi):
    """Rings, glow, HUD ticks and the pulse waveform, drawn big then downsampled for AA."""
    layer = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx = cy = hi / 2
    r = hi * 0.46

    # Outer glow rings (blurred, composited first so crisp rings sit on top)
    glow = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=MAGENTA_GLOW, width=int(hi * 0.03))
    gdraw.ellipse([cx - r * 0.98, cy - r * 0.98, cx + r * 0.98, cy + r * 0.98],
                  outline=CYAN_GLOW, width=int(hi * 0.018))
    glow = glow.filter(ImageFilter.GaussianBlur(hi * 0.02))
    layer = Image.alpha_composite(layer, glow)
    draw = ImageDraw.Draw(layer)

    # Crisp cyan ring
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=CYAN, width=max(2, int(hi * 0.008)))

    # HUD tick marks around the ring (radar/instrument feel)
    for deg in range(0, 360, 30):
        rad = math.radians(deg)
        inner = r * 0.90
        outer = r * 1.0
        x1, y1 = cx + inner * math.cos(rad), cy + inner * math.sin(rad)
        x2, y2 = cx + outer * math.cos(rad), cy + outer * math.sin(rad)
        draw.line([x1, y1, x2, y2], fill=CYAN, width=max(1, int(hi * 0.004)))

    # ECG-style pulse waveform across the middle
    w = r * 1.55
    pts_norm = [
        (-0.95, 0.0), (-0.55, 0.0), (-0.42, 0.10), (-0.30, -0.06), (-0.20, 0.0),
        (-0.08, 0.0), (-0.02, -0.62), (0.04, 0.42), (0.12, 0.0),
        (0.30, 0.0), (0.42, -0.14), (0.55, 0.0), (0.95, 0.0),
    ]
    pulse_points = [(cx + px_ * w, cy + py_ * r * 0.9) for px_, py_ in pts_norm]

    pulse_layer = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(pulse_layer)
    pdraw.line(pulse_points, fill=PULSE_GLOW, width=int(hi * 0.028), joint="curve")
    pulse_layer = pulse_layer.filter(ImageFilter.GaussianBlur(hi * 0.012))
    layer = Image.alpha_composite(layer, pulse_layer)
    draw = ImageDraw.Draw(layer)
    draw.line(pulse_points, fill=PULSE, width=max(2, int(hi * 0.01)), joint="curve")

    # Glowing node at the highest peak of the pulse
    peak = pulse_points[6]
    node_r = hi * 0.022
    node_glow = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    ndraw = ImageDraw.Draw(node_glow)
    ndraw.ellipse([peak[0] - node_r * 2.2, peak[1] - node_r * 2.2,
                   peak[0] + node_r * 2.2, peak[1] + node_r * 2.2], fill=PULSE_GLOW)
    node_glow = node_glow.filter(ImageFilter.GaussianBlur(hi * 0.015))
    layer = Image.alpha_composite(layer, node_glow)
    draw = ImageDraw.Draw(layer)
    draw.ellipse([peak[0] - node_r, peak[1] - node_r, peak[0] + node_r, peak[1] + node_r], fill=NODE)

    return layer


def build():
    bg = make_background(FINAL)
    lines_hi = make_linework(HI)
    lines = lines_hi.resize((FINAL, FINAL), Image.LANCZOS)
    final = Image.alpha_composite(bg, lines)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    final.save(OUT_PATH, format="ICO", sizes=[(s, s) for s in ICON_SIZES])
    print(f"Wrote {OUT_PATH} with sizes {ICON_SIZES}")


if __name__ == "__main__":
    build()
