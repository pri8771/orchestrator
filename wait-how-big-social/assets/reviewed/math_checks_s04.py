#!/usr/bin/env python3
"""BOTS-122: deterministic math unit checks for WHB-013..016."""
import json
import sys
from decimal import Decimal, getcontext

getcontext().prec = 28


def check_whb013():
    mean_distance_km = Decimal("384400")
    earth_diameter_km = Decimal("12756")
    ratio = mean_distance_km / earth_diameter_km
    rounded = ratio.quantize(Decimal("0.1"))
    assert rounded == Decimal("30.1"), f"expected 30.1, got {rounded}"
    return {
        "content_id": "WHB-013",
        "sources": [
            {"label": "mean Earth-Moon distance", "value_km": "384400", "authority": "NASA Moon Fact Sheet"},
            {"label": "Earth equatorial diameter", "value_km": "12756", "authority": "NASA Earth Fact Sheet"},
        ],
        "calculation": "384400 / 12756",
        "exact_ratio": str(ratio),
        "rounded_earths_in_gap": str(rounded),
        "display": "~30 Earths in the gap",
        "pass": True,
    }


def check_whb014():
    sun_diameter_km = Decimal("1392700")
    earth_diameter_km = Decimal("12742")
    ratio_linear = sun_diameter_km / earth_diameter_km
    volume_ratio = ratio_linear ** 3
    million = volume_ratio / Decimal("1000000")
    rounded_million = million.quantize(Decimal("0.01"))
    assert Decimal("1.30") <= rounded_million <= Decimal("1.32"), f"unexpected {rounded_million}"
    return {
        "content_id": "WHB-014",
        "sources": [
            {"label": "Sun diameter", "value_km": "1392700", "authority": "NASA Sun Fact Sheet"},
            {"label": "Earth diameter", "value_km": "12742", "authority": "NASA Earth Fact Sheet"},
        ],
        "calculation": "(1392700 / 12742)^3",
        "exact_volume_ratio": str(volume_ratio),
        "million_earth_volumes": str(million),
        "rounded_million_earth_volumes": str(rounded_million),
        "display": "~1.3 million Earth volumes",
        "label": "volume comparison, not physical packing",
        "pass": True,
    }


def check_whb015():
    challenger_m = Decimal("10935")
    everest_m = Decimal("8849")
    remaining = challenger_m - everest_m
    rounded_km = (remaining / Decimal("1000")).quantize(Decimal("0.1"))
    assert rounded_km == Decimal("2.1"), f"expected 2.1 km, got {rounded_km}"
    return {
        "content_id": "WHB-015",
        "sources": [
            {"label": "Challenger Deep depth", "value_m": "10935", "authority": "NOAA Ocean Exploration"},
            {"label": "Mount Everest summit elevation", "value_m": "8849", "authority": "Nepal/China 2020 survey (NOAA/NASA cited)"},
        ],
        "calculation": "10935 - 8849",
        "exact_remaining_m": str(remaining),
        "rounded_remaining_km": str(rounded_km),
        "display": "summit still ~2.1 km underwater",
        "pass": True,
    }


def check_whb016():
    hours_per_year = Decimal("24") * Decimal("365.25")
    million_hours = Decimal("1000000")
    billion_hours = Decimal("1000000000")
    million_years = million_hours / hours_per_year
    billion_years = billion_hours / hours_per_year
    rounded_million = million_years.quantize(Decimal("1"))
    rounded_billion = (billion_years / Decimal("1000")).quantize(Decimal("1")) * Decimal("1000")
    assert rounded_million == Decimal("114"), f"expected 114 years, got {rounded_million}"
    assert rounded_billion == Decimal("114000"), f"expected 114000 years, got {rounded_billion}"
    return {
        "content_id": "WHB-016",
        "sources": [
            {"label": "hours per Julian year", "value": "8766", "authority": "24 × 365.25"},
        ],
        "calculation_million": "1000000 / (24 * 365.25)",
        "calculation_billion": "1000000000 / (24 * 365.25)",
        "exact_million_years": str(million_years),
        "exact_billion_years": str(billion_years),
        "rounded_million_years": str(rounded_million),
        "rounded_billion_years": str(rounded_billion),
        "ratio": "1000:1",
        "display": "114 years vs 114,000 years",
        "pass": True,
    }


CHECKS = {
    "WHB-013": check_whb013,
    "WHB-014": check_whb014,
    "WHB-015": check_whb015,
    "WHB-016": check_whb016,
}


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else None
    results = [fn() for fn in CHECKS.values()]
    payload = {"jira_key": "BOTS-122", "all_pass": all(r["pass"] for r in results), "checks": results}
    text = json.dumps(payload, indent=2) + "\n"
    if out_path:
        Path = __import__("pathlib").Path
        Path(out_path).write_text(text, encoding="utf-8")
    print(text)
    if not payload["all_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
