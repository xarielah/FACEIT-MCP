import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks import (
    baseline_percentile,
    band_label,
    build_verdict,
    extract_metric,
    percentile_of,
    verdicts_to_weaknesses,
)


def test_percentile_of_basic():
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile_of(3.0, samples) == 50.0
    assert percentile_of(0.5, samples) == 0.0
    assert percentile_of(6.0, samples) == 100.0


def test_percentile_of_empty_returns_median():
    assert percentile_of(1.0, []) == 50.0


def test_baseline_percentile_monotonic():
    # For level 10 K/D, higher value => higher percentile.
    low = baseline_percentile("kd", 0.90, 10)
    mid = baseline_percentile("kd", 1.10, 10)
    high = baseline_percentile("kd", 1.30, 10)
    assert low < mid < high
    assert 0 <= low <= 100 and 0 <= high <= 100


def test_baseline_percentile_at_median_is_50():
    # median K/D for level 5 is 0.98 in the table
    assert abs(baseline_percentile("kd", 0.98, 5) - 50.0) < 1.0


def test_band_label_tiers():
    assert "top-decile" in band_label(95, 8)
    assert "bottom-decile" in band_label(5, 8)
    assert "above-median" in band_label(60, 8)


def test_extract_metric_parses_fields():
    lifetime = {"Average K/D Ratio": "1.15", "Average Headshots %": "47", "Win Rate %": "52"}
    assert extract_metric(lifetime, "kd") == 1.15
    assert extract_metric(lifetime, "hs_pct") == 47.0
    assert extract_metric(lifetime, "win_rate") == 52.0
    assert extract_metric(lifetime, "adr") is None  # absent


def test_build_verdict_sampled_vs_baseline():
    v_sampled = build_verdict("adr", 60.0, 8, [70.0, 75.0, 80.0, 85.0], "sampled")
    assert v_sampled["source"] == "sampled"
    assert v_sampled["percentile"] == 0.0  # below all peers

    v_baseline = build_verdict("adr", 60.0, 8, None, "sampled")  # no samples -> baseline
    assert v_baseline["source"] == "baseline"
    assert v_baseline["percentile"] is not None


def test_build_verdict_missing_value():
    v = build_verdict("adr", None, 8, None, "sampled")
    assert v["value"] is None
    assert v["percentile"] is None
    assert v["band"] == "not available"


def test_verdicts_to_weaknesses_flags_low_percentiles():
    verdicts = [
        build_verdict("kd", 0.5, 8, [1.0, 1.1, 1.2, 1.3], "sampled"),   # ~0th pct -> high
        build_verdict("adr", 72.0, 8, [70.0, 71.0, 90.0, 95.0], "sampled"),  # ~37th -> not a weakness
    ]
    weaknesses = verdicts_to_weaknesses(verdicts)
    codes = {w["code"] for w in weaknesses}
    assert "PEER_LOW_KD" in codes
    # 37th percentile is above the 25 cutoff -> not flagged
    assert "PEER_LOW_ADR" not in codes
    # bottom-decile should be high severity
    kd_w = next(w for w in weaknesses if w["code"] == "PEER_LOW_KD")
    assert kd_w["severity"] == "high"
