import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import (
    analyze_aim,
    analyze_form,
    analyze_fragging,
    analyze_maps,
    analyze_mismatch,
    build_context,
    build_diagnostic,
    safe_float,
)


def test_safe_float_handles_percent_and_none():
    assert safe_float("45%") == 45.0
    assert safe_float(None, default=1.0) == 1.0
    assert safe_float("") == 0.0
    assert safe_float("1.23") == 1.23
    assert safe_float("garbage", default=2.0) == 2.0


def test_build_context_level_band_and_next_level():
    profile = {
        "nickname": "s1mple",
        "player_id": "abc-123",
        "country": "ua",
        "games": {"cs2": {"skill_level": 8, "faceit_elo": 1600, "region": "EU"}},
    }
    ctx = build_context(profile)
    assert ctx["skill_level"] == 8
    assert ctx["faceit_elo"] == 1600
    assert "Level 8" in ctx["band_label"]
    assert ctx["elo_to_next_level"] == 151  # 1750 + 1 - 1600


def test_analyze_fragging_flags_low_impact():
    lifetime = {"Average K/D Ratio": "1.2", "Average K/R Ratio": "0.55"}
    result = analyze_fragging(lifetime)
    assert result["kd"] == 1.2
    assert result["low_impact_despite_kd"] is True


def test_analyze_aim_reads_low_hs():
    result = analyze_aim({"Average Headshots %": "25"})
    assert result["hs_pct"] == 25.0
    assert "spray" in result["hs_read"]


def test_analyze_mismatch_detects_kd_winrate_gap():
    result = analyze_mismatch({"Win Rate %": "40"}, kd=1.3)
    assert result["kd_winrate_mismatch"] is True


def test_analyze_maps_filters_small_samples_and_ranks():
    segments = [
        {"type": "Map", "label": "de_mirage", "stats": {"Matches": "50", "Win Rate %": "60", "Average K/D Ratio": "1.1"}},
        {"type": "Map", "label": "de_inferno", "stats": {"Matches": "40", "Win Rate %": "30", "Average K/D Ratio": "0.9"}},
        {"type": "Map", "label": "de_train", "stats": {"Matches": "3", "Win Rate %": "100", "Average K/D Ratio": "2.0"}},
    ]
    result = analyze_maps(segments)
    labels = [m["map"] for m in result["eligible_maps"]]
    assert "de_train" not in labels  # below MIN_MAP_SAMPLE
    assert result["best_maps"][0]["map"] == "de_mirage"
    assert result["worst_maps"][0]["map"] == "de_inferno"


def test_analyze_form_detects_loss_streak_and_tilt():
    lifetime = {"Recent Results": ["0", "0", "0", "1", "1"]}
    result = analyze_form(history_items=[], recent_match_stats=[], player_id="p1", lifetime=lifetime)
    assert result["current_streak_type"] == "loss"
    assert result["current_streak_length"] == 3
    assert result["likely_tilt"] is True


def test_analyze_form_trend_from_match_stats():
    def match(player_id, kd):
        return {
            "rounds": [
                {
                    "teams": [
                        {"players": [{"player_id": player_id, "player_stats": {"K/D Ratio": str(kd)}}]}
                    ]
                }
            ]
        }

    # kds listed most-recent-first, improving trend expected
    recent_stats = [match("p1", 1.5), match("p1", 1.4), match("p1", 0.8), match("p1", 0.7)]
    result = analyze_form(history_items=[], recent_match_stats=recent_stats, player_id="p1", lifetime={})
    assert result["trend"] == "improving"
    assert result["matches_sampled"] == 4


def test_build_diagnostic_end_to_end_mismatch_case():
    profile = {
        "nickname": "tester",
        "player_id": "p1",
        "country": "us",
        "games": {"cs2": {"skill_level": 7, "faceit_elo": 1400, "region": "NA"}},
    }
    stats = {
        "lifetime": {
            "Average K/D Ratio": "1.25",
            "Average K/R Ratio": "0.6",
            "Average Headshots %": "30",
            "Win Rate %": "42",
            "Recent Results": ["0", "0", "0"],
        },
        "segments": [
            {"type": "Map", "label": "de_mirage", "stats": {"Matches": "20", "Win Rate %": "25", "Average K/D Ratio": "1.0"}},
        ],
    }
    diagnostic = build_diagnostic(profile, stats, history_items=[], recent_match_stats=[], bans=[])

    codes = {w["code"] for w in diagnostic["weaknesses"]}
    assert "WINRATE_KD_MISMATCH" in codes
    assert "LOW_HS_PERCENT" in codes
    assert "TILT_LOSS_STREAK" in codes
    assert diagnostic["profile_summary"]["nickname"] == "tester"
