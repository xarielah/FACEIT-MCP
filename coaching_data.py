"""Maps diagnostic weakness codes to concrete, data-backed CS2 training plans.

Pure data + one assembly function — no I/O.
"""

from __future__ import annotations

from typing import Any

# Each entry: drills to run for a given weakness code. `why` is a template
# filled in with the specific weakness's `metric` string from the diagnostic,
# so the rationale is tied to the player's own numbers.
DRILL_LIBRARY: dict[str, list[dict[str, str]]] = {
    "LOW_HS_PERCENT": [
        {
            "drill": "Crosshair placement + spray control in aim_botz",
            "cadence": "15 min before every session",
            "why": "Your {metric} points to crosshair resting below head level or over-relying on spray. "
            "Aim_botz with a focus on pre-aiming head-height corners rebuilds the habit.",
            "resource": "aim_botz (Steam Workshop), Refrag.gg aim routines",
        },
        {
            "drill": "Yprac prefire maps for your most-played maps",
            "cadence": "10-15 min, 3x/week",
            "why": "Prefire drilling trains snapping to head level at common angles, which raises HS% "
            "faster than raw deathmatch volume.",
            "resource": "Yprac Practice Config (Steam Workshop)",
        },
    ],
    "LOW_KD": [
        {
            "drill": "Structured deathmatch warmup (headshot-only servers)",
            "cadence": "20 min before competitive queue",
            "why": "A sub-1.0 K/D usually reflects cold or unstructured warmup, not a ceiling on ability. "
            "Headshot-only DM rebuilds tracking/flicking fundamentals fast.",
            "resource": "Community HS-only deathmatch servers, Aim Lab 'CS2 Benchmark' routine",
        },
    ],
    "LOW_KR_IMPACT": [
        {
            "drill": "Opening-duel and trade-fragging repetitions",
            "cadence": "1 focused session/week reviewing your own opening duels",
            "why": "Your {metric} shows kills are landing but not converting into round-swinging picks. "
            "Practicing entry timing and immediately re-peeking to trade teammates raises impact per round.",
            "resource": "Yprac 'Duels' maps, VOD review of your own opening picks",
        },
        {
            "drill": "Utility-lineup practice for flashes/smokes on entry maps",
            "cadence": "15 min, 2x/week",
            "why": "Better utility on entry converts even-numbered duels into your favor, directly lifting K/R.",
            "resource": "Yprac utility maps, csgonades-style lineup resources",
        },
    ],
    "WINRATE_KD_MISMATCH": [
        {
            "drill": "Full-match VOD review focused on rounds you topfragged but lost",
            "cadence": "1-2 matches/week",
            "why": "Your {metric} is the strongest signal in this whole profile: mechanics aren't the bottleneck, "
            "decision-making and team coordination are. Review comms, utility timing, and clutch decisions specifically.",
            "resource": "Leetify (round-by-round win probability), in-game demo review",
        },
        {
            "drill": "Deliberate trade-and-utility practice in scrims/DM with a plan",
            "cadence": "ongoing, every competitive session",
            "why": "Converting individual frags into round wins requires trading discipline and utility support, "
            "not more raw aim training.",
            "resource": "5v5 scrims with shot-calling focus",
        },
    ],
    "WEAK_MAP": [
        {
            "drill": "Yprac map-specific practice + common-position VOD review for {map}",
            "cadence": "20 min, 2x/week until win rate stabilizes",
            "why": "{metric} — enough matches to be a real weak spot, not variance. Either drill it up or bookmark it "
            "as a ban candidate in the meantime.",
            "resource": "Yprac map packs, pro VOD reviews for the map's common executes/retakes",
        },
    ],
    "TILT_LOSS_STREAK": [
        {
            "drill": "Hard session cap + queue break after 2 consecutive losses",
            "cadence": "every session",
            "why": "{metric} — continuing to queue on tilt compounds losses. A short break resets decision quality "
            "more reliably than trying to 'fix it' mid-session.",
            "resource": "Simple personal rule, no tool needed",
        },
    ],
    "DECLINING_FORM": [
        {
            "drill": "Reset with a warmup routine before ranked queue and shorten session length",
            "cadence": "every session this week",
            "why": "{metric}. Short-term dips are usually warmup/fatigue related before they're mechanical.",
            "resource": "Aim Lab / DM warmup routine, session-length discipline",
        },
    ],
    "INCONSISTENT_FORM": [
        {
            "drill": "Standardize pre-match warmup and stick to a fixed queue schedule",
            "cadence": "ongoing",
            "why": "{metric} — high variance often comes from inconsistent warmup/sleep/session timing rather than "
            "an aim ceiling issue.",
            "resource": "Personal routine + Leetify session tracking",
        },
    ],
    # --- Leetify-derived (advanced) weaknesses ---
    "LEETIFY_LOW_OPENING": [
        {
            "drill": "Entry / opening-duel practice: prefire common angles + first-contact aim duels",
            "cadence": "15 min, 3x/week",
            "why": "{metric}. Winning the first duel swings round economy and numbers; prefire + duel maps train "
            "exactly the angles you're losing on.",
            "resource": "Yprac prefire & duel maps, aim_botz first-shot practice",
        },
    ],
    "LEETIFY_LOW_TRADING": [
        {
            "drill": "Trade-refrag discipline: stick to trade distance and re-peek instantly when a teammate dies",
            "cadence": "every competitive session + 1 VOD review/week",
            "why": "{metric}. Converting a teammate's death into a trade is a positioning/timing habit, not aim — "
            "review rounds where a teammate died untraded.",
            "resource": "Leetify round review, in-game demo review",
        },
    ],
    "LEETIFY_POOR_PREAIM": [
        {
            "drill": "Crosshair-placement reps: walk maps keeping the dot at head level on every angle",
            "cadence": "10 min before every session",
            "why": "{metric}. Lowering preaim degrees means less distance to move on contact — directly cuts reaction "
            "time and raises HS%.",
            "resource": "Yprac crosshair-placement maps, aim_botz",
        },
    ],
    "LEETIFY_LOW_UTILITY": [
        {
            "drill": "Learn a compact flash/smoke/HE lineup pack for your 3 most-played maps",
            "cadence": "15 min, 2x/week",
            "why": "{metric}. Utility is the cheapest rating gain — a few reliable lineups per map lift round impact "
            "without touching your aim.",
            "resource": "Yprac utility maps, csgonades-style lineup resources",
        },
    ],
    "LEETIFY_LOW_POSITIONING": [
        {
            "drill": "Death review: tag every death as over-peek / bad crossfire / caught-out and fix the top pattern",
            "cadence": "1-2 matches/week",
            "why": "{metric}. Positioning is learned by reviewing where you die, not by aiming more — cut the most "
            "frequent avoidable death.",
            "resource": "Leetify positioning breakdown, demo review",
        },
    ],
    # --- Peer-relative (percentile) weaknesses ---
    "PEER_LOW_KD": [
        {
            "drill": "Structured aim warmup + review of rounds where you traded poorly",
            "cadence": "20 min warmup every session",
            "why": "{metric}. You're low relative to your own level's peers — closing that gap is about consistent "
            "warmup and cleaner duels, not a mechanics overhaul.",
            "resource": "aim_botz, HS-only DM, Leetify session tracking",
        },
    ],
    "PEER_LOW_KR": [
        {
            "drill": "Impact reps: opening duels, multi-kill setups, and utility to force favorable fights",
            "cadence": "15 min, 3x/week",
            "why": "{metric}. Peers at your level get more kills per round — chase impact frags (entries/trades), "
            "not just end-of-round pickups.",
            "resource": "Yprac duel maps, prefire configs",
        },
    ],
    "PEER_LOW_HS_PCT": [
        {
            "drill": "Head-level crosshair placement + headshot-only deathmatch",
            "cadence": "15 min before every session",
            "why": "{metric}. Your headshot rate trails peers at your level — head-height crosshair discipline is the "
            "fastest fix.",
            "resource": "aim_botz, HS-only DM servers, Yprac prefire",
        },
    ],
    "PEER_LOW_ADR": [
        {
            "drill": "Damage-focused play: trade damage before dying, use HE/utility for chip damage",
            "cadence": "ongoing",
            "why": "{metric}. Low damage-per-round vs peers points to dying without contributing — prioritise trading "
            "damage and utility on every round.",
            "resource": "Leetify ADR/utility breakdown, demo review",
        },
    ],
    "PEER_LOW_WIN_RATE": [
        {
            "drill": "Win-condition focus: comms, mid-round calling, and reviewing losses for decision errors",
            "cadence": "1-2 loss reviews/week",
            "why": "{metric}. Below-peer win rate despite playing the same pool means round decisions/teamplay are the "
            "lever — review losses, not just deaths.",
            "resource": "Leetify round-by-round, VOD review with a focus on calls",
        },
    ],
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def build_improvement_plan(diagnostic: dict[str, Any], focus: str | None = None) -> dict[str, Any]:
    """Build a prioritized plan from a diagnostic's weaknesses.

    `focus` may be a weakness code (e.g. "LOW_HS_PERCENT") or a loose keyword
    (e.g. "aim", "maps", "tilt") to filter to one area.
    """
    weaknesses = list(diagnostic.get("weaknesses") or [])
    weaknesses.sort(key=lambda w: SEVERITY_ORDER.get(w.get("severity", "low"), 3))

    if focus:
        focus_norm = focus.strip().lower()
        keyword_map = {
            "aim": {"LOW_HS_PERCENT", "LOW_KD", "LEETIFY_POOR_PREAIM", "PEER_LOW_HS_PCT", "PEER_LOW_KD"},
            "hs": {"LOW_HS_PERCENT", "LEETIFY_POOR_PREAIM", "PEER_LOW_HS_PCT"},
            "impact": {"LOW_KR_IMPACT", "PEER_LOW_KR", "PEER_LOW_ADR"},
            "opening": {"LEETIFY_LOW_OPENING"},
            "entry": {"LEETIFY_LOW_OPENING"},
            "trading": {"LEETIFY_LOW_TRADING"},
            "trade": {"LEETIFY_LOW_TRADING"},
            "utility": {"LEETIFY_LOW_UTILITY"},
            "positioning": {"LEETIFY_LOW_POSITIONING"},
            "map": {"WEAK_MAP"},
            "maps": {"WEAK_MAP"},
            "teamplay": {"WINRATE_KD_MISMATCH", "LEETIFY_LOW_TRADING", "PEER_LOW_WIN_RATE"},
            "team": {"WINRATE_KD_MISMATCH", "LEETIFY_LOW_TRADING", "PEER_LOW_WIN_RATE"},
            "tilt": {"TILT_LOSS_STREAK", "DECLINING_FORM", "INCONSISTENT_FORM"},
            "form": {"DECLINING_FORM", "INCONSISTENT_FORM"},
        }
        allowed_codes = keyword_map.get(focus_norm, {focus_norm.upper()})
        weaknesses = [w for w in weaknesses if w.get("code") in allowed_codes]

    plan_items = []
    for weakness in weaknesses:
        drills = DRILL_LIBRARY.get(weakness["code"], [])
        formatted_drills = []
        for drill in drills:
            formatted_drills.append(
                {
                    "drill": drill["drill"],
                    "cadence": drill["cadence"],
                    "resource": drill["resource"],
                    "why": drill["why"].format(metric=weakness.get("metric", ""), map=weakness.get("map", "")),
                }
            )
        plan_items.append(
            {
                "targets": weakness["code"],
                "severity": weakness.get("severity", "low"),
                "weakness_summary": weakness.get("summary"),
                "drills": formatted_drills,
            }
        )

    return {
        "focus": focus,
        "plan": plan_items,
        "note": None if plan_items else "No weaknesses matched this focus, or none were detected in the diagnostic.",
    }
