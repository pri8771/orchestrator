#!/usr/bin/env python3
"""BOTS-117: reproduce only repaired WHB-001 scene graphics. Requires existing Pillow.
Derived from original render_media.py at a528dce41833c6abdea3be713cb30f02379e7220.
Writes only to an explicitly supplied new output directory; no publishing behavior.
"""
import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
W, H = 1080, 1920
BG, PANEL, CREAM, MUTED = "#0D1117", "#171D26", "#F7F1E7", "#AEB7C4"
LIME, CORAL, BLUE, PURPLE, YELLOW = "#CFFF47", "#FF6B6B", "#6BCBFF", "#B99CFF", "#FFD166"
ACCENTS = [LIME, CORAL, BLUE, PURPLE, YELLOW]


FONT_CANDIDATES = [
 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
 "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
 "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
]

REG_CANDIDATES = [
 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
 "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
 "/System/Library/Fonts/Supplemental/Arial.ttf"
]

def font(size: int, bold: bool = True):
    candidates = FONT_CANDIDATES if bold else REG_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

def wrap(text: str, fnt, max_width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    probe = Image.new("RGB", (10, 10)); draw = ImageDraw.Draw(probe)
    for word in words:
        test = f"{current} {word}".strip()
        if draw.textbbox((0, 0), test, font=fnt)[2] <= max_width or not current:
            current = test
        else:
            lines.append(current); current = word
    if current: lines.append(current)
    return lines

def centered(draw, text: str, y: int, fnt, fill, max_width=900, spacing=14):
    lines = wrap(text, fnt, max_width)
    total = sum(draw.textbbox((0,0), line, font=fnt)[3] for line in lines) + spacing * (len(lines)-1)
    cy = y - total // 2
    for line in lines:
        box = draw.textbbox((0,0), line, font=fnt)
        draw.text(((W-(box[2]-box[0]))/2, cy), line, font=fnt, fill=fill)
        cy += box[3] + spacing

def visual(draw, kind: str, phase: float, accent: str):
    if kind != "seconds":
        raise ValueError("Only WHB-001 is supported")
    draw.rounded_rectangle((100, 630, 980, 1260), 44, fill=PANEL, outline="#2A3340", width=3)
    centered(draw, "TIME COMPARISON — NOT A SCALE CHART", 685, font(27), MUTED, 800)
    for label, value, y, color in [
        ("1 MILLION SECONDS", "≈ 11.6 DAYS", 755, LIME),
        ("1 BILLION SECONDS", "≈ 31.7 YEARS", 975, CORAL),
    ]:
        draw.text((150, y), label, font=font(32), fill=CREAM)
        draw.text((150, y + 58), value, font=font(62), fill=color)
    centered(draw, "1 BILLION = 1,000 × 1 MILLION", 1200, font(34), CREAM, 790)


def render(item: dict, phase: float, size=(W,H)) -> Image.Image:
    image=Image.new("RGB", size, BG); draw=ImageDraw.Draw(image)
    accent=ACCENTS[int(item['id'][-1])%len(ACCENTS)] if item['id'][-1].isdigit() else LIME
    draw.text((72,60), "WAIT, HOW BIG?", font=font(38), fill=accent)
    draw.text((72,112), item['id'], font=font(24,False), fill=MUTED)
    draw.rounded_rectangle((72,170,1008,190),10,fill="#2A3340")
    draw.rounded_rectangle((72,170,72+936*phase,190),10,fill=accent)
    centered(draw,item['hook'],390,font(58),CREAM,900,12)
    visual(draw,item['visual'],phase,accent)
    if phase>0.42:
        centered(draw,item['big'],1435,font(64),accent,900,12)
    if phase>0.70:
        centered(draw,item['sub'],1650,font(36,False),MUTED,860,10)
        centered(draw,"FOLLOW FOR THE NEXT PERSPECTIVE SHIFT",1810,font(28),CREAM,860,8)
    return image

def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 render_whb001.py NEW_OUTPUT_DIRECTORY")
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    scene_dir = root / "scenes"
    scene_dir.mkdir()
    item = {"id":"WHB-001", "hook":"A MILLION SECONDS", "big":"11.6 DAYS vs 31.7 YEARS", "sub":"A billion is one thousand millions.", "visual":"seconds"}
    phases = [0.12, 0.28, 0.45, 0.62, 0.80, 1.0]
    frame_counts = [58, 50, 65, 65, 58, 64]
    for index, phase in enumerate(phases):
        render(item, phase).save(scene_dir / f"scene-{index}.png", optimize=True)
    render(item, 1.0).save(root / "WHB-001-cover-repaired.png", optimize=True)
    (root / "SCENE_MANIFEST.json").write_text(json.dumps({
        "content_id": "WHB-001", "fps": 30, "frame_counts": frame_counts,
        "duration_seconds": 12, "phases": phases,
        "repair": "Replace arbitrary 220/800 bars with numerical duration comparison and explicit not-a-scale-chart label."
    }, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
