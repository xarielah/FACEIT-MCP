"""Pure, unit-testable diagnostic engine.

Takes raw FACEIT API payloads (already fetched) and turns them into a
structured diagnostic: strengths, weaknesses (with severity + code), map
insights, recent-form trend, and an overall assessment. No I/O happens here.
"""

from __future__ import annotations

import statistics
from typing import Any

# CS2 FACEIT ELO bands per skill level (approximate, per FACEIT's published table).
SKILL_LEVEL_BANDS = {
    1: (100, 500),
    2: (501, 750),
    3: (751, 900),
    4: (901, 1050),
    5: (1051, 1200),
    6: (1201, 1350),
    7: (1351, 1530),
    8: (1531, 1750),
    9: (1751, 2000),
    10: (2001, None),
}

MIN_MAP_SAMPLE = 10  # ignore maps with fewer matches than this


def safe_float(value: Any, default: float = 0.0) -> float:
    """Parse FACEIT's stringly-typed stats into floats, tolerating '%', '', None."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "")
    if text == "" or text.lower() == "nan":
        return default
    try:
        return float(text)
    except ValueError:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    return int(safe_float(value, default))


# ----------------------------------------------------------------------
# Context: skill level / ELO
# ----------------------------------------------------------------------


def build_context(profile: dict[str, Any]) -> dict[str, Any]:
    games = profile.get("games") or {}
    cs2 = games.get("cs2") or {}
    skill_level = safe_int(cs2.get("skill_level"), 0)
    elo = safe_int(cs2.get("faceit_elo"), 0)
    region = cs2.get("region", "unknown")

    band = SKILL_LEVEL_BANDS.get(skill_level)
    elo_to_next = None
    band_label = "unranked"
    if band:
        low, high = band
        if high is None:
            band_label = f"Level {skill_level} ({low}+ ELO)"
        else:
            band_label = f"Level {skill_level} ({low}-{high} ELO)"
            elo_to_next = max(high + 1 - elo, 0)

    return {
        "nickname": profile.get("nickname"),
        "player_id": profile.get("player_id"),
        "country": profile.get("country"),
        "region": region,
        "skill_level": skill_level,
        "faceit_elo": elo,
        "band_label": band_label,
        "elo_to_next_level": elo_to_next,
    }


# ----------------------------------------------------------------------
# Fragging / aim / mismatch
# ----------------------------------------------------------------------


def analyze_fragging(lifetime: dict[str, Any]) -> dict[str, Any]:
    kd = safe_float(lifetime.get("Average K/D Ratio"))
    kr = safe_float(lifetime.get("Average K/R Ratio") or lifetime.get("K/R Ratio"))

    if kd >= 1.15:
        kd_read = "strong, consistently net-positive fragger"
    elif kd >= 0.95:
        kd_read = "roughly break-even in raw frags"
    else:
        kd_read = "net-negative in raw frags"

    low_impact = kd >= 1.0 and 0 < kr < 0.68
    return {
        "kd": kd,
        "kr": kr,
        "kd_read": kd_read,
        "low_impact_despite_kd": low_impact,
    }


def analyze_aim(lifetime: dict[str, Any]) -> dict[str, Any]:
    hs_pct = safe_float(lifetime.get("Average Headshots %"))
    adr = safe_float(lifetime.get("ADR") or lifetime.get("Average Damage per Round"))

    if hs_pct >= 50:
        hs_read = "disciplined crosshair placement at head level, rifles cleanly"
    elif hs_pct >= 35:
        hs_read = "moderate headshot rate, mixes spray-down with head-level aim"
    else:
        hs_read = "spray-reliant, crosshair likely resting below head level"

    return {"hs_pct": hs_pct, "adr": adr if adr > 0 else None, "hs_read": hs_read}


def analyze_mismatch(lifetime: dict[str, Any], kd: float) -> dict[str, Any]:
    win_rate = safe_float(lifetime.get("Win Rate %"))
    mismatch = kd >= 1.05 and win_rate < 48
    return {"win_rate": win_rate, "kd_winrate_mismatch": mismatch}


# ----------------------------------------------------------------------
# Map pool
# ----------------------------------------------------------------------


def analyze_maps(segments: list[dict[str, Any]]) -> dict[str, Any]:
    maps = []
    for seg in segments or []:
        if seg.get("type") != "Map":
            continue
        stats = seg.get("stats") or {}
        matches = safe_int(stats.get("Matches"))
        if matches < MIN_MAP_SAMPLE:
            continue
        maps.append(
            {
                "map": seg.get("label"),
                "matches": matches,
                "win_rate": safe_float(stats.get("Win Rate %")),
                "kd": safe_float(stats.get("Average K/D Ratio")),
            }
        )

    ranked = sorted(maps, key=lambda m: m["win_rate"], reverse=True)
    best = ranked[:3]
    worst = list(reversed(ranked[-3:])) if len(ranked) > 3 else list(reversed(ranked))
    # avoid overlap when there are 3 or fewer maps
    if best and worst and len(ranked) <= 3:
        worst = [m for m in worst if m not in best] or worst

    return {
        "eligible_maps": ranked,
        "best_maps": best,
        "worst_maps": worst,
        "insufficient_sample": len(maps) == 0,
    }


# ----------------------------------------------------------------------
# Recent form / tilt
# ----------------------------------------------------------------------


def _extract_match_kd(match_stats: dict[str, Any], player_id: str) -> float | None:
    for round_ in match_stats.get("rounds", []) or []:
        for team in round_.get("teams", []) or []:
            for player in team.get("players", []) or []:
                if player.get("player_id") == player_id:
                    pstats = player.get("player_stats") or {}
                    return safe_float(pstats.get("K/D Ratio"))
    return None


def analyze_form(
    history_items: list[dict[str, Any]],
    recent_match_stats: list[dict[str, Any]],
    player_id: str,
    lifetime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recent_results = []
    lifetime = lifetime or {}
    raw_recent = lifetime.get("Recent Results")
    if isinstance(raw_recent, list):
        recent_results = [safe_int(r) for r in raw_recent]

    current_streak_type = None
    current_streak_len = 0
    if recent_results:
        first = recent_results[0]
        current_streak_type = "win" if first == 1 else "loss"
        for r in recent_results:
            if r == first:
                current_streak_len += 1
            else:
                break

    kds = [kd for kd in (_extract_match_kd(m, player_id) for m in recent_match_stats) if kd is not None]

    trend = "insufficient data"
    consistency = None
    if len(kds) >= 4:
        half = len(kds) // 2
        recent_half_avg = sum(kds[:half]) / half
        older_half_avg = sum(kds[half:]) / (len(kds) - half)
        if recent_half_avg - older_half_avg > 0.1:
            trend = "improving"
        elif older_half_avg - recent_half_avg > 0.1:
            trend = "declining"
        else:
            trend = "stable"
        consistency = round(statistics.pstdev(kds), 2)

    likely_tilt = current_streak_type == "loss" and current_streak_len >= 3

    return {
        "recent_results": recent_results,
        "current_streak_type": current_streak_type,
        "current_streak_length": current_streak_len,
        "trend": trend,
        "consistency_stdev": consistency,
        "matches_sampled": len(kds),
        "likely_tilt": likely_tilt,
    }


def infer_role(hs_pct: float, kr: float, lifetime: dict[str, Any]) -> str:
    triple = safe_int(lifetime.get("Triple Kills"))
    matches = max(safe_int(lifetime.get("Matches")), 1)
    multi_kill_rate = triple / matches

    if kr >= 0.75 and (hs_pct >= 45 or multi_kill_rate > 0.15):
        return "entry / fragger lean"
    if kr < 0.65 and hs_pct < 40:
        return "support / utility lean"
    return "balanced / flexible role"


# ----------------------------------------------------------------------
# Leetify fusion (advanced metrics, Phase 4)
# ----------------------------------------------------------------------

LEETIFY_ATTRIBUTION = "Data Provided by Leetify"


def analyze_leetify(leetify_profile: dict[str, Any] | None, steam64: str | None = None) -> dict[str, Any]:
    """Interpret a Leetify profile into advanced signals + strengths/weaknesses.

    Returns {"available": False, ...} when no profile is present so callers can
    degrade cleanly to FACEIT-only analysis. Any available output carries the
    required Leetify attribution + profile link.
    """
    profile_url = f"https://leetify.com/app/profile/{steam64}" if steam64 else None

    if not leetify_profile:
        return {
            "available": False,
            "steam64": steam64,
            "profile_url": profile_url,
            "note": "No Leetify profile found for this player (they may never have uploaded). "
            "Connecting Leetify would add opening-duel, trade, utility, preaim and positioning insight.",
            "strengths": [],
            "weaknesses": [],
        }

    ranks = leetify_profile.get("ranks") or {}
    rating = leetify_profile.get("rating") or {}
    stats = leetify_profile.get("stats") or {}

    ct_open = safe_float(stats.get("ct_opening_duel_success_percentage"))
    t_open = safe_float(stats.get("t_opening_duel_success_percentage"))
    open_vals = [v for v in (ct_open, t_open) if v > 0]
    opening_pct = sum(open_vals) / len(open_vals) if open_vals else 0.0

    trade_kill_pct = safe_float(stats.get("trade_kills_success_percentage"))
    traded_death_pct = safe_float(stats.get("traded_deaths_success_percentage"))
    preaim = safe_float(stats.get("preaim"))
    reaction_ms = safe_float(stats.get("reaction_time_ms"))
    he_dmg = safe_float(stats.get("he_foes_damage_avg"))
    util_on_death = safe_float(stats.get("utility_on_death_avg"))
    spray_acc = safe_float(stats.get("spray_accuracy"))

    aim_rating = safe_float(rating.get("aim"))
    positioning_rating = safe_float(rating.get("positioning"))
    utility_rating = safe_float(rating.get("utility"))

    key_stats = {
        "leetify_rating": ranks.get("leetify"),
        "opening_duel_success_pct": round(opening_pct, 1) if opening_pct else None,
        "trade_kills_success_pct": round(trade_kill_pct, 1) if trade_kill_pct else None,
        "traded_deaths_success_pct": round(traded_death_pct, 1) if traded_death_pct else None,
        "preaim_degrees": round(preaim, 2) if preaim else None,
        "reaction_time_ms": round(reaction_ms, 0) if reaction_ms else None,
        "he_foes_damage_avg": round(he_dmg, 1) if he_dmg else None,
        "spray_accuracy": round(spray_acc, 3) if spray_acc else None,
    }
    ratings = {
        "aim": round(aim_rating, 1) if aim_rating else None,
        "positioning": round(positioning_rating, 1) if positioning_rating else None,
        "utility": round(utility_rating, 1) if utility_rating else None,
    }

    strengths: list[dict[str, Any]] = []
    weaknesses: list[dict[str, Any]] = []

    # Opening duels
    if opening_pct and opening_pct < 45:
        weaknesses.append(
            {
                "code": "LEETIFY_LOW_OPENING",
                "severity": "medium",
                "metric": f"Opening-duel success {opening_pct:.0f}% (Leetify)",
                "summary": "Losing more opening duels than winning — entry timing and aim-duel prep are costing early-round advantage.",
            }
        )
    elif opening_pct >= 55:
        strengths.append({"code": "LEETIFY_STRONG_OPENING", "summary": f"Wins opening duels ({opening_pct:.0f}%, Leetify)."})

    # Trading
    if trade_kill_pct and trade_kill_pct < 45:
        weaknesses.append(
            {
                "code": "LEETIFY_LOW_TRADING",
                "severity": "medium",
                "metric": f"Trade-kill success {trade_kill_pct:.0f}% (Leetify)",
                "summary": "Low trade-kill conversion — not punishing when a teammate dies. A trading/refrag discipline issue, not aim.",
            }
        )

    # Preaim / crosshair placement (lower degrees = better)
    if preaim and preaim > 16:
        weaknesses.append(
            {
                "code": "LEETIFY_POOR_PREAIM",
                "severity": "medium",
                "metric": f"Preaim {preaim:.1f}° off target (Leetify)",
                "summary": "Crosshair is far from head level on average — crosshair-placement drilling would cut reaction time and raise HS%.",
            }
        )
    elif preaim and preaim <= 11:
        strengths.append({"code": "LEETIFY_GOOD_PREAIM", "summary": f"Tight crosshair placement ({preaim:.1f}°, Leetify)."})

    # Utility
    if utility_rating and utility_rating < 45:
        weaknesses.append(
            {
                "code": "LEETIFY_LOW_UTILITY",
                "severity": "low",
                "metric": f"Utility rating {utility_rating:.0f} (Leetify)",
                "summary": "Below-par utility impact — flash/smoke/HE lineups and timing are an easy point gain.",
            }
        )
    elif utility_rating and utility_rating >= 60:
        strengths.append({"code": "LEETIFY_STRONG_UTILITY", "summary": f"Strong utility usage (rating {utility_rating:.0f}, Leetify)."})

    # Positioning
    if positioning_rating and positioning_rating < 45:
        weaknesses.append(
            {
                "code": "LEETIFY_LOW_POSITIONING",
                "severity": "medium",
                "metric": f"Positioning rating {positioning_rating:.0f} (Leetify)",
                "summary": "Weak positioning rating — dying in avoidable spots. Review deaths for over-peeks and bad crossfires.",
            }
        )
    elif positioning_rating and positioning_rating >= 60:
        strengths.append({"code": "LEETIFY_STRONG_POSITIONING", "summary": f"Good positioning (rating {positioning_rating:.0f}, Leetify)."})

    if aim_rating and aim_rating >= 65:
        strengths.append({"code": "LEETIFY_STRONG_AIM", "summary": f"Strong aim rating ({aim_rating:.0f}, Leetify)."})

    return {
        "available": True,
        "steam64": steam64 or leetify_profile.get("steam64_id"),
        "profile_url": profile_url,
        "attribution": LEETIFY_ATTRIBUTION,
        "ratings": ratings,
        "key_stats": key_stats,
        "strengths": strengths,
        "weaknesses": weaknesses,
    }


# ----------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------


def build_diagnostic(
    profile: dict[str, Any],
    stats: dict[str, Any],
    history_items: list[dict[str, Any]],
    recent_match_stats: list[dict[str, Any]],
    bans: list[dict[str, Any]],
    leetify_profile: dict[str, Any] | None = None,
    benchmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = build_context(profile)
    lifetime = stats.get("lifetime") or {}
    segments = stats.get("segments") or []

    fragging = analyze_fragging(lifetime)
    aim = analyze_aim(lifetime)
    mismatch = analyze_mismatch(lifetime, fragging["kd"])
    maps = analyze_maps(segments)
    form = analyze_form(history_items, recent_match_stats, context.get("player_id") or "", lifetime)
    role = infer_role(aim["hs_pct"], fragging["kr"], lifetime)

    cs2 = (profile.get("games") or {}).get("cs2") or {}
    steam64 = cs2.get("game_player_id")
    leetify = analyze_leetify(leetify_profile, steam64)

    strengths: list[dict[str, Any]] = []
    weaknesses: list[dict[str, Any]] = []

    if fragging["kd"] >= 1.15:
        strengths.append(
            {
                "code": "STRONG_KD",
                "summary": f"Strong fragging output (K/D {fragging['kd']:.2f}).",
            }
        )
    elif fragging["kd"] < 0.95 and fragging["kd"] > 0:
        weaknesses.append(
            {
                "code": "LOW_KD",
                "severity": "medium",
                "metric": f"Average K/D Ratio = {fragging['kd']:.2f}",
                "summary": "Net-negative fragging overall.",
            }
        )

    if aim["hs_pct"] >= 50:
        strengths.append(
            {"code": "HIGH_HS_PERCENT", "summary": f"High headshot rate ({aim['hs_pct']:.0f}%)."}
        )
    elif aim["hs_pct"] < 35 and aim["hs_pct"] > 0:
        weaknesses.append(
            {
                "code": "LOW_HS_PERCENT",
                "severity": "medium",
                "metric": f"Average Headshots % = {aim['hs_pct']:.0f}%",
                "summary": "Low headshot rate suggests spray-reliant aim or low crosshair placement.",
            }
        )

    if fragging["low_impact_despite_kd"]:
        weaknesses.append(
            {
                "code": "LOW_KR_IMPACT",
                "severity": "medium",
                "metric": f"K/R Ratio = {fragging['kr']:.2f}",
                "summary": "Decent K/D but low kills-per-round — likely picking up low-impact frags rather than opening/trading duels.",
            }
        )

    if mismatch["kd_winrate_mismatch"]:
        weaknesses.append(
            {
                "code": "WINRATE_KD_MISMATCH",
                "severity": "high",
                "metric": f"K/D {fragging['kd']:.2f} vs Win Rate {mismatch['win_rate']:.0f}%",
                "summary": "Individual fragging outpaces win rate — points to weak team play: trading, utility usage, or clutch/round-closing decisions rather than aim.",
            }
        )
    elif mismatch["win_rate"] >= 55:
        strengths.append(
            {"code": "GOOD_WINRATE", "summary": f"Strong win rate ({mismatch['win_rate']:.0f}%)."}
        )

    for m in maps["worst_maps"]:
        weaknesses.append(
            {
                "code": "WEAK_MAP",
                "severity": "low",
                "metric": f"{m['map']}: {m['win_rate']:.0f}% win rate over {m['matches']} matches",
                "summary": f"Weak map: {m['map']}.",
                "map": m["map"],
            }
        )

    for m in maps["best_maps"]:
        if m["win_rate"] >= 55:
            strengths.append(
                {
                    "code": "STRONG_MAP",
                    "summary": f"Strong map: {m['map']} ({m['win_rate']:.0f}% win rate over {m['matches']} matches).",
                }
            )

    if form["likely_tilt"]:
        weaknesses.append(
            {
                "code": "TILT_LOSS_STREAK",
                "severity": "high",
                "metric": f"Current streak: {form['current_streak_length']} losses",
                "summary": "On an active loss streak long enough that tilt is a real risk — session/queue management matters as much as mechanics right now.",
            }
        )

    if form["trend"] == "declining":
        weaknesses.append(
            {
                "code": "DECLINING_FORM",
                "severity": "medium",
                "metric": "Recent K/D trending down vs earlier sampled matches",
                "summary": "Recent form is declining relative to earlier matches in the sample.",
            }
        )
    elif form["trend"] == "improving":
        strengths.append({"code": "IMPROVING_FORM", "summary": "Recent form is trending upward."})

    if form["consistency_stdev"] is not None and form["consistency_stdev"] >= 0.5:
        weaknesses.append(
            {
                "code": "INCONSISTENT_FORM",
                "severity": "low",
                "metric": f"K/D standard deviation = {form['consistency_stdev']}",
                "summary": "High match-to-match variance — inconsistent performance rather than a steady baseline.",
            }
        )

    # Fold in Leetify-derived strengths/weaknesses (Phase 4).
    strengths.extend(leetify.get("strengths", []))
    weaknesses.extend(leetify.get("weaknesses", []))

    # Fold in relative (peer-percentile) weaknesses (Phase 1).
    if benchmark:
        for rel in benchmark.get("relative_weaknesses", []):
            weaknesses.append(rel)

    active_bans = [b for b in (bans or []) if str(b.get("status", "")).lower() not in ("expired", "revoked")]

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    weaknesses.sort(key=lambda w: severity_rank.get(w.get("severity", "low"), 3))

    overall_assessment = _build_overall_assessment(context, fragging, aim, mismatch, form)

    result = {
        "profile_summary": context,
        "role_lean": role,
        "fragging": fragging,
        "aim": aim,
        "map_insights": maps,
        "form": form,
        "bans": active_bans,
        "benchmark": benchmark,
        "advanced_stats_leetify": leetify,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "overall_assessment": overall_assessment,
    }
    if leetify.get("available"):
        result["attribution"] = LEETIFY_ATTRIBUTION
        result["leetify_profile_url"] = leetify.get("profile_url")
    return result


def _build_overall_assessment(context, fragging, aim, mismatch, form) -> str:
    parts = [
        f"{context.get('nickname', 'Player')} is {context.get('band_label', 'unranked')}.",
        f"K/D {fragging['kd']:.2f}, HS% {aim['hs_pct']:.0f}, win rate {mismatch['win_rate']:.0f}%.",
    ]
    if mismatch["kd_winrate_mismatch"]:
        parts.append("Individual output exceeds team results — team-play factors are the top lever.")
    if form["likely_tilt"]:
        parts.append(f"Currently on a {form['current_streak_length']}-loss streak; manage tilt before grinding more queue.")
    return " ".join(parts)
