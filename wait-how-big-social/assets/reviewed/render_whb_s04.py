#!/usr/bin/env python3
"""BOTS-122: render WHB-013..016 scene graphics (1080x1920). Numerical comparison, NOT scale charts."""
import json
import sys
from pathlib import Path
from typing import TypedDict
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
BG, PANEL, CREAM, MUTED = "#0D1117", "#171D26", "#F7F1E7", "#AEB7C4"
LIME, CORAL, BLUE, PURPLE, YELLOW = "#CFFF47", "#FF6B6B", "#6BCBFF", "#B99CFF", "#FFD166"
ACCENTS = [LIME, CORAL, BLUE, PURPLE, YELLOW]

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
REG_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

class SceneSpec(TypedDict):
    hook: str
    big: str
    sub: str
    visual: str
    frame_counts: list[int]
    phases: list[float]
    duration_seconds: int


ITEMS: dict[str, SceneSpec] = {
    "WHB-013": {
        "hook": "THIRTY EARTHS TO THE MOON",
        "big": "~30 EARTHS IN THE GAP",
        "sub": "Mean distance ÷ equatorial diameter — not a scale diagram.",
        "visual": "earth_moon",
        "frame_counts": [58, 50, 65, 65, 58, 64],
        "phases": [0.12, 0.28, 0.45, 0.62, 0.80, 1.0],
        "duration_seconds": 12,
    },
    "WHB-014": {
        "hook": "SUN VOLUME VS EARTH",
        "big": "~1.3 MILLION EARTHS",
        "sub": "Volume comparison — not physical packing.",
        "visual": "sun_volume",
        "frame_counts": [58, 50, 65, 65, 58, 64],
        "phases": [0.12, 0.28, 0.45, 0.62, 0.80, 1.0],
        "duration_seconds": 12,
    },
    "WHB-015": {
        "hook": "EVEREST IN CHALLENGER DEEP",
        "big": "SUMMIT STILL ~2.1 KM UNDER",
        "sub": "Deepest ocean point beats tallest mountain above sea level.",
        "visual": "challenger",
        "frame_counts": [58, 50, 65, 65, 58, 64],
        "phases": [0.12, 0.28, 0.45, 0.62, 0.80, 1.0],
        "duration_seconds": 12,
    },
    "WHB-016": {
        "hook": "A MILLION HOURS",
        "big": "114 YEARS vs 114,000 YEARS",
        "sub": "Three extra zeros move lifetime into deep prehistory.",
        "visual": "hours",
        "frame_counts": [58, 50, 65, 65, 58, 64],
        "phases": [0.12, 0.28, 0.45, 0.62, 0.80, 1.0],
        "duration_seconds": 12,
    },
}


def font(size: int, bold: bool = True):
    candidates = FONT_CANDIDATES if bold else REG_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def wrap(text: str, fnt, max_width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)
    for word in words:
        test = f"{current} {word}".strip()
        if draw.textbbox((0, 0), test, font=fnt)[2] <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def centered(draw, text: str, y: int, fnt, fill, max_width=900, spacing=14):
    lines = wrap(text, fnt, max_width)
    total = sum(draw.textbbox((0, 0), line, font=fnt)[3] for line in lines) + spacing * (len(lines) - 1)
    cy = y - total // 2
    for line in lines:
        box = draw.textbbox((0, 0), line, font=fnt)
        draw.text(((W - (box[2] - box[0])) / 2, cy), line, font=fnt, fill=fill)
        cy += box[3] + spacing


def panel_header(draw, title: str):
    draw.rounded_rectangle((100, 630, 980, 1260), 44, fill=PANEL, outline="#2A3340", width=3)
    centered(draw, title, 685, font(27), MUTED, 800)


def visual(draw, kind: str, accent: str):
    if kind == "earth_moon":
        panel_header(draw, "NUMERICAL COMPARISON — NOT A SCALE CHART")
        for label, value, y, color in [
            ("MEAN EARTH–MOON DISTANCE", "384,400 km", 755, LIME),
            ("EARTH EQUATORIAL DIAMETER", "12,756 km", 900, BLUE),
            ("EARTHS IN THE GAP", "≈ 30.1", 1045, CORAL),
        ]:
            draw.text((150, y), label, font=font(30), fill=CREAM)
            draw.text((150, y + 52), value, font=font(58), fill=color)
        centered(draw, "384,400 ÷ 12,756 ≈ 30.1", 1200, font(34), CREAM, 790)
    elif kind == "sun_volume":
        panel_header(draw, "VOLUME COMPARISON — NOT PHYSICAL PACKING")
        for label, value, y, color in [
            ("SUN DIAMETER", "~1,392,700 km", 755, YELLOW),
            ("EARTH DIAMETER", "~12,742 km", 900, BLUE),
            ("SUN VOLUME IN EARTHS", "≈ 1.31 million", 1045, CORAL),
        ]:
            draw.text((150, y), label, font=font(30), fill=CREAM)
            draw.text((150, y + 52), value, font=font(52), fill=color)
        centered(draw, "VOLUME SCALES WITH DIAMETER³", 1200, font(34), CREAM, 790)
    elif kind == "challenger":
        panel_header(draw, "DEPTH COMPARISON — NOT A SCALE CHART")
        for label, value, y, color in [
            ("CHALLENGER DEEP", "~10,935 m", 755, BLUE),
            ("MT. EVEREST (SUMMIT)", "~8,849 m", 900, LIME),
            ("EVEREST SUMMIT IF PLACED AT BOTTOM", "≈ 2,086 m underwater", 1045, CORAL),
        ]:
            draw.text((150, y), label, font=font(28), fill=CREAM)
            draw.text((150, y + 48), value, font=font(46), fill=color)
        centered(draw, "10,935 − 8,849 = 2,086 m", 1200, font(34), CREAM, 790)
    elif kind == "hours":
        panel_header(draw, "TIME COMPARISON — NOT A SCALE CHART")
        for label, value, y, color in [
            ("1 MILLION HOURS", "≈ 114 YEARS", 755, LIME),
            ("1 BILLION HOURS", "≈ 114,000 YEARS", 975, CORAL),
        ]:
            draw.text((150, y), label, font=font(32), fill=CREAM)
            draw.text((150, y + 58), value, font=font(62), fill=color)
        centered(draw, "1 BILLION = 1,000 × 1 MILLION", 1200, font(34), CREAM, 790)
    else:
        raise ValueError(f"Unknown visual kind: {kind}")


def render(item: dict, phase: float) -> Image.Image:
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    accent = ACCENTS[int(item["id"][-1]) % len(ACCENTS)] if item["id"][-1].isdigit() else LIME
    draw.text((72, 60), "WAIT, HOW BIG?", font=font(38), fill=accent)
    draw.text((72, 112), item["id"], font=font(24, False), fill=MUTED)
    draw.rounded_rectangle((72, 170, 1008, 190), 10, fill="#2A3340")
    draw.rounded_rectangle((72, 170, 72 + 936 * phase, 190), 10, fill=accent)
    centered(draw, item["hook"], 390, font(58), CREAM, 900, 12)
    visual(draw, item["visual"], accent)
    if phase > 0.42:
        centered(draw, item["big"], 1435, font(64), accent, 900, 12)
    if phase > 0.70:
        centered(draw, item["sub"], 1650, font(36, False), MUTED, 860, 10)
        centered(draw, "FOLLOW FOR THE NEXT PERSPECTIVE SHIFT", 1810, font(28), CREAM, 860, 8)
    return image


def render_item(content_id: str, root: Path):
    spec = ITEMS[content_id]
    item = {
        "id": content_id,
        "hook": spec["hook"],
        "big": spec["big"],
        "sub": spec["sub"],
        "visual": spec["visual"],
    }
    scene_dir = root / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)
    for index, phase in enumerate(spec["phases"]):
        render(item, phase).save(scene_dir / f"scene-{index}.png", optimize=True)
    render(item, 1.0).save(root / f"{content_id}-cover.png", optimize=True)
    manifest = {
        "content_id": content_id,
        "fps": 30,
        "frame_counts": spec["frame_counts"],
        "duration_seconds": spec["duration_seconds"],
        "phases": spec["phases"],
        "repair": "Numerical comparison with explicit not-a-scale-chart or volume-not-packing label.",
        "jira_key": "BOTS-122",
    }
    (root / "SCENE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python3 render_whb_s04.py CONTENT_ID OUTPUT_DIRECTORY")
    content_id = sys.argv[1]
    if content_id not in ITEMS:
        raise SystemExit(f"Unknown content_id {content_id}; expected one of {sorted(ITEMS)}")
    root = Path(sys.argv[2]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = render_item(content_id, root)
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
