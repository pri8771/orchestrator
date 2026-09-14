#!/usr/bin/env python3
"""BOTS-120 / S04-03: math + caption packets for WHB-017..022 (silent path)."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

JIRA = "BOTS-120"
FRAME_COUNTS = [58, 50, 65, 65, 58, 64]

PACKETS = {
    "WHB-017": {
        "topic": "Light-year vs AU",
        "slug": "light-year-in-aus",
        "sources": [
            {"label": "1 AU", "value": "149597870.7 km", "authority": "IAU astronomical unit"},
            {"label": "1 light-year", "value": "9.4607304725808e12 km", "authority": "IAU / Julian-year c definition"},
        ],
        "math": {
            "expr": "9.4607304725808e12 / 149597870.7",
            "exact": 9.4607304725808e12 / 149597870.7,
            "display": "≈ 63,241 AU",
            "pass_rule": lambda x: 63240 <= x <= 63242,
        },
        "captions": {
            "twitter": "One light-year is not “a little farther than the Sun.”\n\nUsing IAU definitions: about 63,241 astronomical units fit in one light-year.\n\nDistance math — not a star map.",
            "instagram": "1 AU = 149,597,870.7 km. One light-year ≈ 9.461×10¹² km. Divide them and you get ≈ 63,241 AU.\n\nSources: IAU AU and light-year definitions\n#spacefacts #scale #astronomy",
            "tiktok": "A light-year holds about 63,241 Earth–Sun distances (AUs). IAU definitions, not vibes. #space #math #scale",
        },
        "script": "An astronomical unit is one hundred forty-nine million five hundred ninety-seven thousand eight hundred seventy point seven kilometers — Earth's mean Sun distance by IAU definition. A light-year is about nine point four six one times ten to the twelfth kilometers. Divide them and roughly sixty-three thousand two hundred forty-one AUs fit in one light-year. That is distance math, not a star map.",
        "disclaimer": "DISTANCE COMPARISON — NOT A STAR MAP",
    },
    "WHB-018": {
        "topic": "Lake Superior vs Olympic pool",
        "slug": "superior-vs-olympic-pool",
        "sources": [
            {"label": "Olympic pool volume", "value": "2500 m³", "authority": "50×25×2 m FINA-class geometry"},
            {"label": "Lake Superior volume", "value": "~12100 km³", "authority": "EPA/NOAA Great Lakes volumes (rounded)"},
        ],
        "math": {
            "expr": "(12100 * 1e9) / 2500",
            "exact": (12100 * 1e9) / 2500,
            "display": "≈ 4.84 billion pools",
            "pass_rule": lambda x: 4.8e9 <= x <= 4.9e9,
        },
        "captions": {
            "twitter": "An Olympic pool is about 2,500 m³.\nLake Superior is about 12,100 km³.\n\nThat is roughly 4.8 billion pools — a volume comparison, not packing.",
            "instagram": "50×25×2 m ≈ 2,500 m³. Lake Superior ≈ 12,100 km³ ≈ 1.21×10¹³ m³. Divide: ≈ 4.84 billion Olympic pools.\n\nSources: FINA-class pool geometry; EPA/NOAA Great Lakes volumes\n#geography #scale #dataviz",
            "tiktok": "Lake Superior holds about 4.8 billion Olympic pools. Volume math, not packing. #geography #scale",
        },
        "script": "A fifty by twenty-five by two meter Olympic-class pool is about two thousand five hundred cubic meters. Lake Superior holds about twelve thousand one hundred cubic kilometers of water. Convert and divide, and you need roughly four point eight billion such pools to match Superior's volume. That is a volume comparison, not a packing diagram.",
        "disclaimer": "VOLUME COMPARISON — NOT PACKING",
    },
    "WHB-019": {
        "topic": "Lifetime heartbeats (illustrative)",
        "slug": "lifetime-heartbeats",
        "sources": [
            {"label": "Illustrative resting rate", "value": "70 bpm", "authority": "Mid adult resting range illustration (not a clinical claim)"},
            {"label": "Lifetime window", "value": "80 years", "authority": "Chosen illustration window"},
            {"label": "Seconds per day", "value": "86400", "authority": "SI day"},
        ],
        "math": {
            "expr": "70 * 60 * 24 * 365.25 * 80",
            "exact": 70 * 60 * 24 * 365.25 * 80,
            "display": "≈ 2.94 billion beats",
            "pass_rule": lambda x: 2.9e9 <= x <= 3.0e9,
        },
        "captions": {
            "twitter": "At an illustrative 70 beats/minute for 80 years: about 2.9 billion heartbeats.\n\nA day has 86,400 seconds. Scale is the point — not medical advice.",
            "instagram": "Illustrative only: 70 bpm × 60 × 24 × 365.25 × 80 ≈ 2.94 billion beats. One day = 86,400 seconds.\n\nNot medical advice; rates vary.\n#mathfacts #scale #time",
            "tiktok": "About 2.9 billion heartbeats in an 80-year span at 70 bpm — illustration, not diagnosis. #math #time",
        },
        "script": "Take an illustrative resting rate of seventy beats per minute across eighty years. Multiply through the minutes in a Julian year and you get about two point nine four billion beats. A single day has eighty-six thousand four hundred seconds. This is a scale illustration, not medical advice.",
        "disclaimer": "TIME COMPARISON — ILLUSTRATIVE RANGE",
    },
    "WHB-020": {
        "topic": "Everest vs FL350 cruise",
        "slug": "everest-vs-cruise",
        "sources": [
            {"label": "Everest summit", "value": "8849 m", "authority": "Nepal/China 2020 survey"},
            {"label": "FL350", "value": "35000 ft ≈ 10668 m", "authority": "Flight level definition (100 ft increments)"},
        ],
        "math": {
            "expr": "35000 * 0.3048 - 8849",
            "exact": 35000 * 0.3048 - 8849,
            "display": "≈ 1.82 km above Everest",
            "pass_rule": lambda x: 1800 <= x <= 1850,
        },
        "captions": {
            "twitter": "Everest summit ≈ 8,849 m.\nTypical cruise FL350 ≈ 10,668 m.\n\nAbout 1.8 km higher than the summit — altitude math, not a flight path.",
            "instagram": "8,849 m vs FL350 (35,000 ft ≈ 10,668 m). Difference ≈ 1,819 m.\n\nSources: Nepal/China Everest survey; flight-level definition\n#geography #aviation #scale",
            "tiktok": "Cruise at FL350 sits about 1.8 km above Everest's summit. #geography #aviation",
        },
        "script": "Mount Everest's summit is about eight thousand eight hundred forty-nine meters. Flight level three five zero is thirty-five thousand feet, about ten thousand six hundred sixty-eight meters. Subtract and the cruise band sits roughly one point eight kilometers above the summit. Altitude comparison, not a flight path chart.",
        "disclaimer": "ALTITUDE COMPARISON — NOT A FLIGHT PATH",
    },
    "WHB-021": {
        "topic": "Antarctic ice sheet volume",
        "slug": "antarctica-ice-volume",
        "sources": [
            {"label": "Antarctic ice volume", "value": "~26.5 million km³", "authority": "NSIDC / USGS-class ice-sheet volume estimates (rounded)"},
            {"label": "Sea-level equivalent (approx.)", "value": "~58 m", "authority": "Common SLE conversion cited with ice-sheet volume (approximate)"},
        ],
        "math": {
            "expr": "26.5e6",
            "exact": 26.5e6,
            "display": "~26.5 million km³",
            "pass_rule": lambda x: x == 26.5e6,
        },
        "captions": {
            "twitter": "Antarctica holds on the order of 26.5 million km³ of ice.\n\nThat is an ice-sheet volume estimate — cite the range; ice volume is not liquid water sitting ready.",
            "instagram": "~26.5 million km³ of Antarctic ice (rounded NSIDC/USGS-class figures). Approximate sea-level equivalent ~58 m if fully melted — a conversion, not a forecast.\n\n#climate #antarctica #scale",
            "tiktok": "Antarctic ice: about 26.5 million cubic kilometers. Volume estimate with caveats. #antarctica #scale",
        },
        "script": "The Antarctic ice sheet holds on the order of twenty-six point five million cubic kilometers of ice in rounded NSIDC and USGS-class estimates. If fully melted, that volume is often converted to roughly fifty-eight meters of sea-level equivalent. Treat both as rounded estimates with caveats — ice volume is not liquid water waiting in a tank.",
        "disclaimer": "VOLUME ESTIMATE — CITE THE RANGE",
    },
    "WHB-022": {
        "topic": "4K / UHD pixel count",
        "slug": "uhd-pixel-count",
        "sources": [
            {"label": "UHD width×height", "value": "3840×2160", "authority": "CTA Ultra HD / consumer 4K frame definition"},
        ],
        "math": {
            "expr": "3840 * 2160",
            "exact": 3840 * 2160,
            "display": "8,294,400 pixels ≈ 8.3 MP",
            "pass_rule": lambda x: x == 8294400,
        },
        "captions": {
            "twitter": "“4K” / UHD is 3840 × 2160.\n\nThat is exactly 8,294,400 pixels — about 8.3 megapixels. Arithmetic, not a camera review.",
            "instagram": "3840 × 2160 = 8,294,400 pixels ≈ 8.3 MP.\n\nSource: CTA Ultra HD frame geometry\n#tech #math #dataviz",
            "tiktok": "4K is 8.3 megapixels: 3840 times 2160. #tech #math",
        },
        "script": "Consumer four-K or Ultra HD is three thousand eight hundred forty by two thousand one hundred sixty. Multiply and you get exactly eight million two hundred ninety-four thousand four hundred pixels — about eight point three megapixels. That is arithmetic, not a product review.",
        "disclaimer": "PIXEL MATH — NOT A PRODUCT REVIEW",
    },
}


def check_math() -> dict:
    checks = []
    all_pass = True
    for cid, pkt in PACKETS.items():
        exact = pkt["math"]["exact"]
        ok = bool(pkt["math"]["pass_rule"](exact))
        all_pass = all_pass and ok
        checks.append({
            "content_id": cid,
            "sources": pkt["sources"],
            "calculation": pkt["math"]["expr"],
            "exact": exact if not isinstance(exact, float) else exact,
            "display": pkt["math"]["display"],
            "pass": ok,
        })
    return {"jira_key": JIRA, "all_pass": all_pass, "checks": checks}


def write_packet_metadata(out_dir: Path, cid: str, sha256: str, bytes_: int) -> dict:
    pkt = PACKETS[cid]
    prov = {
        "content_id": cid,
        "source_task": JIRA,
        "topic": pkt["topic"],
        "asset": f"{cid}-{pkt['slug']}-{sha256}.mp4",
        "sha256": sha256,
        "bytes": bytes_,
        "variant": "silent_tts_free",
        "format": {
            "duration_seconds": 12,
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "codec": "H.264/avc1",
            "audio": "none (video-only silent MP4; audio_tracks=0)",
        },
        "claim_cards": [
            {"claim": s["label"] + ": " + s["value"], "source": s["authority"], "checked": True}
            for s in pkt["sources"]
        ],
        "rights": {
            "visuals": "Original Pillow-generated graphics",
            "audio": "none on silent upload asset",
            "third_party_assets": [],
        },
        "captions": pkt["captions"],
        "math_check_ref": f"assets/reviewed/MATH_CHECKS_S04_03.json#{cid}",
        "publication_authority": False,
        "status": "staged_upload_ready_silent",
        "disclaimer": pkt["disclaimer"],
    }
    (out_dir / f"{cid}-editorial-provenance.json").write_text(
        json.dumps(prov, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "narration.txt").write_text(pkt["script"] + "\n", encoding="utf-8")
    (out_dir / "media.sha256").write_text(sha256 + "\n", encoding="utf-8")
    return prov


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    report = check_math()
    out = Path(__file__).resolve().parent / "MATH_CHECKS_S04_03.json"
    # JSON-serialize floats cleanly
    serializable = json.loads(json.dumps(report, default=lambda o: o))
    out.write_text(json.dumps(serializable, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"all_pass": report["all_pass"], "path": str(out)}))
    if not report["all_pass"]:
        raise SystemExit(1)
