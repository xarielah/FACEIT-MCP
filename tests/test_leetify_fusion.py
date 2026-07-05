import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import analyze_leetify, build_diagnostic


def _leetify_weak_profile():
    """A Leetify profile with clear weaknesses (low opening/trade/positioning, high preaim)."""
    return {
        "steam64_id": "76561198000000000",
        "ranks": {"leetify": 1.1},
        "rating": {"aim": 40.0, "positioning": 40.0, "utility": 40.0},
        "stats": {
            "ct_opening_duel_success_percentage": 40.0,
            "t_opening_duel_success_percentage": 38.0,
            "trade_kills_success_percentage": 40.0,
            "preaim": 20.0,
            "reaction_time_ms": 700.0,
            "he_foes_damage_avg": 5.0,
            "utility_on_death_avg": 200.0,
            "spray_accuracy": 0.2,
        },
    }


def test_analyze_leetify_absent_degrades():
    r = analyze_leetify(None, "76561198000000000")
    assert r["available"] is False
    assert r["profile_url"].endswith("76561198000000000")
    assert r["strengths"] == [] and r["weaknesses"] == []


def test_analyze_leetify_detects_weaknesses_and_attribution():
    r = analyze_leetify(_leetify_weak_profile(), "76561198000000000")
    assert r["available"] is True
    assert r["attribution"] == "Data Provided by Leetify"
    codes = {w["code"] for w in r["weaknesses"]}
    assert "LEETIFY_LOW_OPENING" in codes
    assert "LEETIFY_LOW_TRADING" in codes
    assert "LEETIFY_POOR_PREAIM" in codes
    assert "LEETIFY_LOW_POSITIONING" in codes


def test_analyze_leetify_detects_strengths():
    strong = {
        "ranks": {"leetify": 2.0},
        "rating": {"aim": 80.0, "positioning": 65.0, "utility": 65.0},
        "stats": {
            "ct_opening_duel_success_percentage": 58.0,
            "t_opening_duel_success_percentage": 57.0,
            "preaim": 9.0,
            "trade_kills_success_percentage": 55.0,
        },
    }
    r = analyze_leetify(strong, "76561198000000000")
    codes = {s["code"] for s in r["strengths"]}
    assert "LEETIFY_STRONG_AIM" in codes
    assert "LEETIFY_GOOD_PREAIM" in codes
    assert "LEETIFY_STRONG_OPENING" in codes


def test_build_diagnostic_fuses_leetify_and_benchmark():
    profile = {
        "nickname": "tester",
        "player_id": "p1",
        "country": "us",
        "games": {"cs2": {"skill_level": 8, "faceit_elo": 1600, "region": "NA", "game_player_id": "76561198000000000"}},
    }
    stats = {"lifetime": {"Average K/D Ratio": "1.0", "Average Headshots %": "45", "Win Rate %": "50"}, "segments": []}
    benchmark = {
        "source": "sampled",
        "verdicts": [],
        "relative_weaknesses": [
            {"code": "PEER_LOW_ADR", "severity": "medium", "metric": "ADR = 60 (bottom-quartile)", "summary": "low adr"}
        ],
    }
    diag = build_diagnostic(
        profile, stats, history_items=[], recent_match_stats=[], bans=[],
        leetify_profile=_leetify_weak_profile(), benchmark=benchmark,
    )
    codes = {w["code"] for w in diag["weaknesses"]}
    # Leetify-derived weakness fused in
    assert "LEETIFY_LOW_OPENING" in codes
    # peer-relative weakness fused in
    assert "PEER_LOW_ADR" in codes
    # attribution surfaced because Leetify data was used
    assert diag["attribution"] == "Data Provided by Leetify"
    assert diag["advanced_stats_leetify"]["available"] is True
    assert diag["benchmark"]["source"] == "sampled"


def test_build_diagnostic_without_leetify_or_benchmark_still_works():
    profile = {"nickname": "t", "player_id": "p", "games": {"cs2": {"skill_level": 5, "faceit_elo": 1100}}}
    stats = {"lifetime": {"Average K/D Ratio": "1.0", "Average Headshots %": "45", "Win Rate %": "50"}, "segments": []}
    diag = build_diagnostic(profile, stats, history_items=[], recent_match_stats=[], bans=[])
    assert diag["advanced_stats_leetify"]["available"] is False
    assert diag["benchmark"] is None
    assert "attribution" not in diag  # no Leetify data => no attribution
