"""Peer-percentile benchmarking — pure math + a static baseline table.

No I/O here (that lives in benchmarking.py). This module owns:
  * the approximate per-skill-level reference table used as a fallback, and
  * the percentile/verdict math used for both live samples and the baseline.
"""

from __future__ import annotations

from typing import Any

from analysis import safe_float

# Metrics we benchmark. `higher_is_better=False` would flip verdicts, but all
# of these read better when larger.
BENCHMARK_METRICS = ["kd", "kr", "hs_pct", "adr", "win_rate"]

# How each metric is pulled out of a FACEIT lifetime stats dict.
LIFETIME_FIELD_MAP = {
    "kd": ["Average K/D Ratio"],
    "kr": ["Average K/R Ratio", "K/R Ratio"],
    "hs_pct": ["Average Headshots %"],
    "adr": ["ADR", "Average Damage per Round"],
    "win_rate": ["Win Rate %"],
}

# Approximate reference distributions (q1 / median / q3) per CS2 skill level.
# These are deliberately rough — they exist only as a fallback when live peer
# sampling is unavailable, and any output derived from them is labelled
# "baseline (approximate)" so the model can caveat it. Win rate stays ~50% at
# every level (matchmaking equalises); K/D, HS% and ADR drift up with level.
STATIC_BASELINE: dict[int, dict[str, tuple[float, float, float]]] = {
    1: {"kd": (0.75, 0.90, 1.05), "kr": (0.50, 0.58, 0.66), "hs_pct": (28, 34, 41), "adr": (55, 62, 70), "win_rate": (42, 50, 58)},
    2: {"kd": (0.78, 0.92, 1.06), "kr": (0.52, 0.59, 0.67), "hs_pct": (30, 36, 43), "adr": (57, 64, 72), "win_rate": (43, 50, 57)},
    3: {"kd": (0.80, 0.94, 1.08), "kr": (0.53, 0.60, 0.68), "hs_pct": (32, 38, 44), "adr": (59, 66, 73), "win_rate": (44, 50, 57)},
    4: {"kd": (0.82, 0.96, 1.10), "kr": (0.55, 0.62, 0.69), "hs_pct": (34, 40, 46), "adr": (61, 68, 75), "win_rate": (44, 50, 56)},
    5: {"kd": (0.84, 0.98, 1.12), "kr": (0.56, 0.63, 0.70), "hs_pct": (35, 41, 47), "adr": (63, 70, 77), "win_rate": (45, 50, 56)},
    6: {"kd": (0.86, 1.00, 1.14), "kr": (0.58, 0.65, 0.72), "hs_pct": (37, 43, 49), "adr": (65, 72, 79), "win_rate": (45, 50, 55)},
    7: {"kd": (0.88, 1.02, 1.16), "kr": (0.59, 0.66, 0.73), "hs_pct": (38, 44, 50), "adr": (67, 74, 81), "win_rate": (45, 50, 55)},
    8: {"kd": (0.90, 1.04, 1.18), "kr": (0.61, 0.68, 0.75), "hs_pct": (39, 45, 51), "adr": (69, 76, 83), "win_rate": (46, 50, 55)},
    9: {"kd": (0.92, 1.06, 1.20), "kr": (0.62, 0.69, 0.76), "hs_pct": (40, 46, 52), "adr": (71, 78, 85), "win_rate": (46, 50, 54)},
    10: {"kd": (0.95, 1.10, 1.28), "kr": (0.64, 0.72, 0.80), "hs_pct": (41, 48, 54), "adr": (73, 81, 90), "win_rate": (46, 50, 54)},
}

METRIC_LABELS = {
    "kd": "K/D",
    "kr": "K/R",
    "hs_pct": "Headshot %",
    "adr": "ADR",
    "win_rate": "Win Rate %",
}


def extract_metric(lifetime: dict[str, Any], metric: str) -> float | None:
    for field in LIFETIME_FIELD_MAP.get(metric, []):
        if field in lifetime and str(lifetime.get(field)).strip() not in ("", "None"):
            val = safe_float(lifetime.get(field))
            if val > 0 or metric == "win_rate":
                return val
    return None


def band_label(percentile: float, level: int) -> str:
    if percentile >= 90:
        tier = "top-decile"
    elif percentile >= 75:
        tier = "top-quartile"
    elif percentile >= 50:
        tier = "above-median"
    elif percentile >= 25:
        tier = "below-median"
    elif percentile >= 10:
        tier = "bottom-quartile"
    else:
        tier = "bottom-decile"
    return f"{tier} for level {level}"


def percentile_of(value: float, samples: list[float]) -> float:
    """Percentile of `value` within `samples` (0-100), using midpoint ranking."""
    if not samples:
        return 50.0
    below = sum(1 for s in samples if s < value)
    equal = sum(1 for s in samples if s == value)
    pct = (below + 0.5 * equal) / len(samples) * 100
    return round(max(0.0, min(100.0, pct)), 1)


def baseline_percentile(metric: str, value: float, level: int) -> float:
    """Estimate a percentile from the static q1/median/q3 baseline via piecewise
    linear interpolation. Approximate by construction."""
    table = STATIC_BASELINE.get(level) or STATIC_BASELINE[max(1, min(level, 10))]
    q1, med, q3 = table[metric]
    if value <= q1:
        # below q1: scale linearly from 0th pct (at value=0) up to 25th (at q1)
        if q1 <= 0:
            return 0.0
        return round(max(0.0, 25 * value / q1), 1)
    if value <= med:
        return round(25 + 25 * (value - q1) / (med - q1 or 1), 1)
    if value <= q3:
        return round(50 + 25 * (value - med) / (q3 - med or 1), 1)
    # above q3: approach 100 asymptotically
    over = (value - q3) / (q3 - med or 1)
    return round(min(99.0, 75 + 20 * min(over, 1.0)), 1)


def build_verdict(
    metric: str,
    value: float | None,
    level: int,
    peer_samples: list[float] | None,
    source: str,
) -> dict[str, Any]:
    """Build a single metric verdict dict."""
    if value is None:
        return {
            "metric": metric,
            "label": METRIC_LABELS.get(metric, metric),
            "value": None,
            "percentile": None,
            "band": "not available",
            "source": source,
        }
    if source == "sampled" and peer_samples:
        pct = percentile_of(value, peer_samples)
    else:
        pct = baseline_percentile(metric, value, level)
        source = "baseline"
    return {
        "metric": metric,
        "label": METRIC_LABELS.get(metric, metric),
        "value": round(value, 2),
        "percentile": pct,
        "band": band_label(pct, level),
        "source": source,
    }


def verdicts_to_weaknesses(verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn relative standing into weaknesses — bottom-quartile among peers is
    actionable even if the raw number looks average."""
    weaknesses = []
    for v in verdicts:
        pct = v.get("percentile")
        if pct is None:
            continue
        if pct < 25:
            severity = "high" if pct < 10 else "medium"
            weaknesses.append(
                {
                    "code": f"PEER_LOW_{v['metric'].upper()}",
                    "severity": severity,
                    "metric": f"{v['label']} = {v['value']} ({v['band']}, {v['percentile']}th pct, {v['source']})",
                    "summary": f"{v['label']} is {v['band']} — {v['percentile']}th percentile among peers at your level.",
                    "benchmark_metric": v["metric"],
                }
            )
    return weaknesses
