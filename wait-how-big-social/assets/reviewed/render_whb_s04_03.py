#!/usr/bin/env python3
"""BOTS-120 / S04-03: render WHB-017..022 scene graphics (1080x1920). Silent/caption path."""
import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
BG, PANEL, CREAM, MUTED = "#0D1117", "#171D26", "#F7F1E7", "#AEB7C4"
LIME, CORAL, BLUE, PURPLE, YELLOW = "#CFFF47", "#FF6B6B", "#6BCBFF", "#B99CFF", "#FFD166"
ACCENTS = [LIME, CORAL, BLUE, PURPLE, YELLOW]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
REG_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

FRAME = [58, 50, 65, 65, 58, 64]
PHASES = [0.12, 0.28, 0.45, 0.62, 0.80, 1.0]

ITEMS = {
    "WHB-017": {
        "hook": "ONE LIGHT-YEAR IN AUs",
        "big": "≈ 63,241 AU",
        "sub": "IAU definitions — distance math, not a star map.",
        "visual": "light_year_au",
        "frame_counts": FRAME,
        "phases": PHASES,
        "duration_seconds": 12,
    },
    "WHB-018": {
        "hook": "LAKE SUPERIOR VS A POOL",
        "big": "≈ 4.8 BILLION POOLS",
        "sub": "Volume order-of-magnitude — not a packing diagram.",
        "visual": "superior_pool",
        "frame_counts": FRAME,
        "phases": PHASES,
        "duration_seconds": 12,
    },
    "WHB-019": {
        "hook": "HEARTBEATS IN 80 YEARS",
        "big": "≈ 2.9 BILLION BEATS",
        "sub": "Illustrative 70 bpm mid-range — not medical advice.",
        "visual": "heartbeats",
        "frame_counts": FRAME,
        "phases": PHASES,
        "duration_seconds": 12,
    },
    "WHB-020": {
        "hook": "EVEREST VS CRUISE ALTITUDE",
        "big": "FL350 ≈ 10.7 KM",
        "sub": "Altitude band comparison — not a flight path chart.",
        "visual": "everest_cruise",
        "frame_counts": FRAME,
        "phases": PHASES,
        "duration_seconds": 12,
    },
    "WHB-021": {
        "hook": "ANTARCTIC ICE FRESHWATER",
        "big": "~26.5 MILLION KM³",
        "sub": "Ice-sheet volume estimate — cite range, not exact stock.",
        "visual": "antarctica_ice",
        "frame_counts": FRAME,
        "phases": PHASES,
        "duration_seconds": 12,
    },
    "WHB-022": {
        "hook": "WHAT “4K” ACTUALLY MEANS",
        "big": "8,294,400 PIXELS",
        "sub": "3840 × 2160 — arithmetic, not a camera review.",
        "visual": "uhd_pixels",
        "frame_counts": FRAME,
        "phases": PHASES,
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


def rows(draw, triples):
    for label, value, y, color in triples:
        draw.text((150, y), label, font=font(28), fill=CREAM)
        draw.text((150, y + 48), value, font=font(48), fill=color)


def visual(draw, kind: str, accent: str):
    if kind == "light_year_au":
        panel_header(draw, "DISTANCE COMPARISON — NOT A STAR MAP")
        rows(draw, [
            ("1 ASTRONOMICAL UNIT (IAU)", "149,597,870.7 km", 755, BLUE),
            ("1 LIGHT-YEAR (IAU)", "≈ 9.461×10¹² km", 900, LIME),
            ("LIGHT-YEARS IN AU", "≈ 63,241 AU", 1045, CORAL),
        ])
        centered(draw, "9.4607e12 / 1.496e8 ≈ 63,241", 1200, font(30), CREAM, 820)
    elif kind == "superior_pool":
        panel_header(draw, "VOLUME COMPARISON — NOT PACKING")
        rows(draw, [
            ("OLYMPIC POOL (50×25×2 m)", "2,500 m³", 755, BLUE),
            ("LAKE SUPERIOR VOLUME", "~12,100 km³", 900, LIME),
            ("POOLS TO MATCH SUPERIOR", "≈ 4.84 billion", 1045, CORAL),
        ])
        centered(draw, "1.21e13 m³ / 2,500 ≈ 4.84e9", 1200, font(30), CREAM, 820)
    elif kind == "heartbeats":
        panel_header(draw, "TIME COMPARISON — ILLUSTRATIVE RANGE")
        rows(draw, [
            ("ASSUMED RESTING RATE", "70 beats/min", 755, YELLOW),
            ("80-YEAR LIFETIME", "≈ 2.94 billion beats", 900, CORAL),
            ("SECONDS IN ONE DAY", "86,400", 1045, BLUE),
        ])
        centered(draw, "Not medical advice — mid-range illustration", 1200, font(28), MUTED, 820)
    elif kind == "everest_cruise":
        panel_header(draw, "ALTITUDE COMPARISON — NOT A FLIGHT PATH")
        rows(draw, [
            ("MT. EVEREST SUMMIT", "~8,849 m", 755, LIME),
            ("TYPICAL CRUISE FL350", "≈ 10,668 m", 900, BLUE),
            ("CRUISE ABOVE EVEREST", "≈ 1.8 km higher", 1045, CORAL),
        ])
        centered(draw, "10,668 − 8,849 = 1,819 m", 1200, font(34), CREAM, 790)
    elif kind == "antarctica_ice":
        panel_header(draw, "VOLUME ESTIMATE — CITE THE RANGE")
        rows(draw, [
            ("ANTARCTIC ICE SHEET", "~26.5 million km³", 755, BLUE),
            ("IF MELTED (SEA-LEVEL EQ.)", "~58 m SLE (approx.)", 900, YELLOW),
            ("LABEL", "Ice volume ≠ liquid now", 1045, CORAL),
        ])
        centered(draw, "NSIDC/USGS-class estimates — rounded", 1200, font(28), MUTED, 820)
    elif kind == "uhd_pixels":
        panel_header(draw, "PIXEL MATH — NOT A PRODUCT REVIEW")
        rows(draw, [
            ("UHD / 4K FRAME", "3840 × 2160", 755, LIME),
            ("TOTAL PIXELS", "8,294,400", 900, CORAL),
            ("≈ MEGAPIXELS", "≈ 8.3 MP", 1045, BLUE),
        ])
        centered(draw, "3840 × 2160 = 8,294,400", 1200, font(34), CREAM, 790)
    else:
        raise ValueError(f"Unknown visual kind: {kind}")


def render(item: dict, phase: float) -> Image.Image:
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    accent = ACCENTS[int(item["id"][-1]) % len(ACCENTS)]
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
        "duration_seconds": 12,
        "phases": spec["phases"],
        "repair": "Numerical comparison with explicit caveat label; silent/caption path.",
        "jira_key": "BOTS-120",
    }
    (root / "SCENE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python3 render_whb_s04_03.py CONTENT_ID OUTPUT_DIRECTORY")
    content_id = sys.argv[1]
    if content_id not in ITEMS:
        raise SystemExit(f"Unknown content_id {content_id}; expected one of {sorted(ITEMS)}")
    root = Path(sys.argv[2]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(json.dumps(render_item(content_id, root)))


if __name__ == "__main__":
    main()
