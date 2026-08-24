#!/usr/bin/env python3
from pathlib import Path
import json, subprocess, sys
ROOT=Path(__file__).resolve().parents[1]
files=sorted((ROOT/"assets"/"launch"/"videos").glob("*.mp4"))
errors=[]; report=[]
for p in files:
    cp=subprocess.run(["ffprobe","-v","error","-show_entries","stream=codec_type,codec_name,width,height,duration","-of","json",str(p)],capture_output=True,text=True)
    if cp.returncode: errors.append(f"{p.name}: ffprobe failed"); continue
    data=json.loads(cp.stdout); streams=data.get("streams",[])
    video=next((s for s in streams if s.get("codec_type")=="video"),{})
    audio=next((s for s in streams if s.get("codec_type")=="audio"),{})
    if video.get("width")!=1080 or video.get("height")!=1920: errors.append(f"{p.name}: wrong dimensions")
    if not audio: errors.append(f"{p.name}: no audio")
    report.append({"file":p.name,"video":video,"audio":audio,"bytes":p.stat().st_size})
(ROOT/"asset_verification.json").write_text(json.dumps({"files":report,"errors":errors},indent=2),encoding="utf-8")
print(json.dumps({"count":len(report),"errors":errors},indent=2))
sys.exit(1 if errors else 0)
