#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
VIDEO_DIR = ASSETS / "launch" / "videos"
COVER_DIR = ASSETS / "launch" / "covers"
CAROUSEL_DIR = ASSETS / "launch" / "carousels"
BRAND_DIR = ASSETS / "brand"
TMP_DIR = ROOT / ".tmp"
for path in (VIDEO_DIR, COVER_DIR, CAROUSEL_DIR, BRAND_DIR, TMP_DIR):
    path.mkdir(parents=True, exist_ok=True)

W, H = 1080, 1920
CW, CH = 1080, 1350
BG, PANEL, CREAM, MUTED = "#0D1117", "#171D26", "#F7F1E7", "#AEB7C4"
LIME, CORAL, BLUE, PURPLE, YELLOW = "#CFFF47", "#FF6B6B", "#6BCBFF", "#B99CFF", "#FFD166"
ACCENTS = [LIME, CORAL, BLUE, PURPLE, YELLOW]

CONTENT = [
 {"id":"WHB-000","slug":"brand-trailer","hook":"BIG NUMBERS LIE TO YOUR BRAIN.","big":"WE TURN THEM INTO PICTURES.","sub":"Wait, How Big? — one perspective shift a day.","visual":"brand","duration":8},
 {"id":"WHB-001","slug":"million-vs-billion-seconds","hook":"A MILLION SECONDS","big":"11.6 DAYS vs 31.7 YEARS","sub":"A billion is one thousand millions.","visual":"seconds","duration":12},
 {"id":"WHB-002","slug":"thirty-earths-to-the-moon","hook":"MOST EARTH–MOON DIAGRAMS CHEAT.","big":"30 EARTHS FIT IN THE GAP.","sub":"Average distance: 384,400 km.","visual":"earth_gap","duration":13},
 {"id":"WHB-003","slug":"sun-volume-earths","hook":"HOW MANY EARTHS FIT IN THE SUN?","big":"ABOUT 1.3 MILLION.","sub":"A volume comparison—not literal packing.","visual":"sun","duration":12},
 {"id":"WHB-004","slug":"challenger-deep","hook":"DROP EVEREST INTO THE DEEPEST OCEAN.","big":"ITS SUMMIT STAYS UNDERWATER.","sub":"Challenger Deep: ~10,935 m. Everest: 8,849 m.","visual":"ocean","duration":13},
 {"id":"WHB-005","slug":"pacific-vs-land","hook":"THE PACIFIC IS BIGGER THAN ALL LAND.","big":"155+ MILLION km².","sub":"One ocean exceeds every continent combined.","visual":"pacific","duration":12},
 {"id":"WHB-006","slug":"people-standing-square","hook":"8.2 BILLION PEOPLE. 1 m² EACH.","big":"A SQUARE ~91 km WIDE.","sub":"Standing room only—not a livable city.","visual":"people","duration":13},
 {"id":"WHB-007","slug":"billion-vs-trillion-dollars","hook":"STACK $100 BILLS.","big":"$1B ≈ 0.69 mi  •  $1T ≈ 690 mi","sub":"A trillion is one thousand billions.","visual":"money","duration":13},
 {"id":"WHB-008","slug":"earth-history-one-year","hook":"EARTH'S HISTORY AS ONE YEAR.","big":"HUMANS ARRIVE ~11:25 PM, DEC 31.","sub":"Our species occupies the final ~35 minutes.","visual":"calendar","duration":14},
 {"id":"WHB-009","slug":"saturn-between-earth-and-moon","hook":"COULD SATURN FIT IN THE EARTH–MOON GAP?","big":"YES—WITH ITS MAIN RINGS.","sub":"Main rings: ~273,550 km. Gap: ~384,400 km.","visual":"saturn","duration":13},
 {"id":"WHB-010","slug":"mid-ocean-ridge","hook":"EARTH'S LONGEST MOUNTAIN RANGE IS HIDDEN.","big":"MID-OCEAN RIDGE: ~65,000 km.","sub":"About 1.6 times around the equator.","visual":"ridge","duration":12},
 {"id":"WHB-011","slug":"blue-whale-basketball-court","hook":"A BLUE WHALE CAN OUTLENGTH AN NBA COURT.","big":"WHALE: 100–110 ft  •  COURT: 94 ft","sub":"The largest animal, shown to scale.","visual":"whale","duration":12},
 {"id":"WHB-012","slug":"million-vs-billion-hours","hook":"A MILLION HOURS IS LONGER THAN A LIFE.","big":"114 YEARS vs ~114,000 YEARS","sub":"Three zeros move from a lifetime to prehistory.","visual":"hours","duration":12}
]

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
    left, top, right, bottom = 100, 630, 980, 1260
    draw.rounded_rectangle((left, top, right, bottom), 44, fill=PANEL, outline="#2A3340", width=3)
    cx, cy = 540, 945
    if kind in ("seconds", "hours"):
        labels = ("1 MILLION", "1 BILLION")
        widths = (220, 800)
        for i, (label, width) in enumerate(zip(labels, widths)):
            y = 800 + i*260
            draw.rounded_rectangle((150,y,150+width*min(1,phase*1.3),y+92), 25, fill=ACCENTS[i])
            draw.text((150,y-70), label, font=font(34), fill=CREAM)
    elif kind == "earth_gap":
        draw.ellipse((125,825,365,1065), fill=BLUE)
        draw.ellipse((865,890,955,980), fill="#DDE2E8")
        n=max(1,int(30*phase))
        for i in range(n):
            x=385+i*15
            draw.ellipse((x,930,x+10,940), fill=LIME)
        draw.text((405,1035), f"{n}/30 EARTHS", font=font(34), fill=CREAM)
    elif kind == "sun":
        radius=int(250+80*phase)
        draw.ellipse((cx-radius,cy-radius,cx+radius,cy+radius), fill=YELLOW)
        for row in range(5):
            for col in range(8):
                if row*8+col < int(40*phase):
                    x=365+col*50; y=815+row*55
                    draw.ellipse((x,y,x+20,y+20), fill=BLUE)
    elif kind == "ocean":
        draw.rectangle((180,720,900,1170), fill="#103758")
        depth=int(390*phase)
        draw.rectangle((260,760,400,760+depth), fill=BLUE)
        draw.polygon([(550,1130),(700,790),(850,1130)], fill="#E8EDF4")
        draw.line((120,1130,960,1130), fill=CREAM, width=5)
    elif kind == "pacific":
        draw.ellipse((150,735,930,1160), fill=BLUE)
        draw.text((360,900), "PACIFIC", font=font(60), fill=BG)
        for i in range(5):
            x=210+i*150
            draw.rounded_rectangle((x,1180,x+85,1225), 14, fill=ACCENTS[i])
    elif kind == "people":
        n=max(4,int(144*phase))
        for i in range(n):
            r=i//12; c=i%12
            x=225+c*52; y=700+r*42
            draw.ellipse((x,y,x+14,y+14), fill=accent)
        draw.rectangle((200,680,880,1200), outline=CREAM, width=4)
    elif kind == "money":
        base=1160
        draw.rectangle((270,base-int(110*phase),430,base), fill=LIME)
        draw.rectangle((650,base-int(430*phase),810,base), fill=CORAL)
        draw.text((260,1190), "$1B", font=font(46), fill=CREAM)
        draw.text((640,1190), "$1T", font=font(46), fill=CREAM)
    elif kind == "calendar":
        draw.rounded_rectangle((245,730,835,1170), 40, fill=CREAM)
        draw.rectangle((245,730,835,860), fill=CORAL)
        draw.text((325,760), "DECEMBER 31", font=font(52), fill=BG)
        draw.text((355,910), "11:25 PM", font=font(76), fill=BG)
        draw.arc((440,1000,640,1200),0,int(360*phase),fill=CORAL,width=18)
    elif kind == "saturn":
        draw.ellipse((380,795,700,1115), fill=YELLOW)
        draw.ellipse((190,880,890,1040), outline=PURPLE, width=34)
        draw.ellipse((125,885,285,1045), fill=BLUE)
        draw.ellipse((875,930,935,990), fill="#DDE2E8")
    elif kind == "ridge":
        pts=[]
        for i in range(18):
            x=150+i*46; y=1030-int((80+120*math.sin(i*1.7))*phase)
            pts.append((x,y))
        draw.line(pts, fill=LIME, width=18, joint="curve")
        draw.line((150,1120,930,1120), fill=BLUE, width=8)
    elif kind == "whale":
        draw.rectangle((180,770,900,1110), outline=CREAM, width=5)
        draw.line((540,770,540,1110), fill=CREAM, width=4)
        draw.ellipse((210,905,850,1045), fill=BLUE)
        draw.polygon([(835,955),(930,900),(900,1015)], fill=BLUE)
        draw.text((360,1150), "94 ft COURT", font=font(38), fill=MUTED)
    else:
        for i in range(5):
            r=int((60+i*55)*phase)
            draw.ellipse((cx-r,cy-r,cx+r,cy+r), outline=ACCENTS[i], width=16)
        draw.text((455,875), "HOW", font=font(64), fill=CREAM)
        draw.text((420,965), "BIG?", font=font(92), fill=LIME)

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

def render_video(item: dict):
    work=TMP_DIR/item['id']; shutil.rmtree(work,ignore_errors=True); work.mkdir(parents=True)
    phases=[0.12,0.28,0.45,0.62,0.80,1.0]
    fractions=[0.16,0.14,0.18,0.18,0.16,0.18]
    frames=[]
    for i,p in enumerate(phases):
        frame=work/f"scene-{i}.png"; render(item,p).save(frame,optimize=True); frames.append(frame)
    duration=float(item['duration']); durs=[duration*f for f in fractions]
    cmd=["ffmpeg","-y","-loglevel","error"]
    for frame,d in zip(frames,durs):
        cmd += ["-loop","1","-framerate","15","-t",f"{d:.3f}","-i",str(frame)]
    cmd += ["-f","lavfi","-t",f"{duration:.3f}","-i","anullsrc=channel_layout=stereo:sample_rate=44100"]
    filters=[]
    for i,d in enumerate(durs):
        filters.append(f"[{i}:v]fps=15,trim=duration={d:.3f},settb=AVTB,setpts=PTS-STARTPTS[v{i}]")
    filters.append("".join(f"[v{i}]" for i in range(6))+"concat=n=6:v=1:a=0[outv]")
    out=VIDEO_DIR/f"{item['id']}_{item['slug']}.mp4"
    cmd += ["-filter_complex",";".join(filters),"-map","[outv]","-map","6:a:0","-c:v","libx264","-preset","ultrafast","-crf","20","-pix_fmt","yuv420p","-r","30","-c:a","aac","-b:a","96k","-shortest","-movflags","+faststart",str(out)]
    subprocess.run(cmd,check=True)
    render(item,1.0).save(COVER_DIR/f"{item['id']}_{item['slug']}.png",optimize=True)
    shutil.rmtree(work,ignore_errors=True)

def brand_assets():
    avatar=Image.new("RGB",(1024,1024),BG); d=ImageDraw.Draw(avatar)
    for i,c in enumerate(ACCENTS):
        r=390-i*62; d.ellipse((512-r,512-r,512+r,512+r),outline=c,width=28)
    d.text((318,372),"HOW",font=font(120),fill=CREAM); d.text((286,512),"BIG?",font=font(150),fill=LIME)
    avatar.save(BRAND_DIR/"avatar.png",optimize=True)
    for name,size in (("x-banner.png",(1500,500)),("youtube-banner.png",(2560,1440))):
        img=Image.new("RGB",size,BG); dr=ImageDraw.Draw(img); sw,sh=size
        dr.text((sw*0.08,sh*0.25),"WAIT, HOW BIG?",font=font(int(sh*0.16)),fill=LIME)
        dr.text((sw*0.08,sh*0.48),"THE WORLD, FINALLY IN PERSPECTIVE.",font=font(int(sh*0.07)),fill=CREAM)
        img.save(BRAND_DIR/name,optimize=True)

def carousels():
    for item in CONTENT[:6]:
        folder=CAROUSEL_DIR/item['id']; folder.mkdir(parents=True,exist_ok=True)
        texts=[item['hook'],item['big'],item['sub'],"FOLLOW @WAITHOWBIG"]
        for i,text in enumerate(texts,1):
            img=Image.new("RGB",(CW,CH),BG); dr=ImageDraw.Draw(img); accent=ACCENTS[(i-1)%len(ACCENTS)]
            dr.text((70,65),"WAIT, HOW BIG?",font=font(34),fill=accent)
            dr.rounded_rectangle((65,170,CW-65,CH-100),44,fill=PANEL,outline="#2A3340",width=3)
            lines=wrap(text,font(62),820); y=430
            for line in lines:
                box=dr.textbbox((0,0),line,font=font(62)); dr.text(((CW-(box[2]-box[0]))/2,y),line,font=font(62),fill=CREAM); y+=box[3]+22
            dr.text((70,CH-70),f"{i}/4",font=font(28),fill=MUTED)
            img.save(folder/f"slide-{i}.png",optimize=True)

def main():
    brand_assets()
    for idx,item in enumerate(CONTENT,1):
        print(f"Rendering {idx}/{len(CONTENT)}: {item['id']}",flush=True); render_video(item)
    carousels()
    summary={"videos":len(list(VIDEO_DIR.glob('*.mp4'))),"covers":len(list(COVER_DIR.glob('*.png'))),"carousel_sets":len([p for p in CAROUSEL_DIR.iterdir() if p.is_dir()])}
    (ROOT/"render_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps(summary,indent=2))

if __name__ == "__main__": main()
