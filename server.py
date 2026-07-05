"""FACEIT CS2 Coaching MCP server.

Exposes read-only FACEIT Data API access plus a rules-based analysis and
coaching engine as MCP tools, served over Streamable HTTP for remote
connectors (e.g. Claude custom connectors) and deployable as-is to Render.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from fastmcp import FastMCP

from faceit_client import FaceitAPIError, get_client
from analysis import build_diagnostic
from coaching_data import build_improvement_plan

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Auth stub (disabled by default — see README "Adding auth later")
# ---------------------------------------------------------------------------
# This server is intentionally read-only and unauthenticated at the MCP
# transport layer. A broken bearer/OAuth handshake is the most common reason
# a remote MCP connector silently fails to attach in Claude, so auth is left
# off for the first deploy. To add it later:
#
#   from fastmcp.server.auth import TokenVerifier
#   auth_provider = TokenVerifier(...)
#   mcp = FastMCP("faceit-cs2-coach", auth=auth_provider)
#
# and require a bearer token via Render env vars / your identity provider.
# Do NOT enable this until you've confirmed the connector works without auth.

mcp = FastMCP(name="faceit-cs2-coach")


def _error_payload(exc: Exception, query: str | None = None) -> dict[str, Any]:
    status_code = getattr(exc, "status_code", None)
    return {
        "error": True,
        "status_code": status_code,
        "message": str(exc),
        "query": query,
    }


async def _gather_overview(query: str) -> dict[str, Any]:
    client = get_client()
    profile = await client.resolve_player_id(query)
    player_id = profile.get("player_id")
    if not player_id:
        raise FaceitAPIError(f"Could not resolve a FACEIT player_id for '{query}'.", status_code=404)

    stats, bans = await asyncio.gather(
        client.get_player_stats(player_id),
        client.get_player_bans(player_id),
    )
    return {"profile": profile, "stats": stats, "bans": bans.get("items", [])}


async def _gather_full_diagnostic_inputs(query: str, history_limit: int = 15) -> dict[str, Any]:
    client = get_client()
    profile = await client.resolve_player_id(query)
    player_id = profile.get("player_id")
    if not player_id:
        raise FaceitAPIError(f"Could not resolve a FACEIT player_id for '{query}'.", status_code=404)

    stats, bans, history = await asyncio.gather(
        client.get_player_stats(player_id),
        client.get_player_bans(player_id),
        client.get_player_history(player_id, limit=history_limit),
    )
    history_items = history.get("items", [])

    match_ids = [item.get("match_id") for item in history_items if item.get("match_id")]
    match_stats_results = await asyncio.gather(
        *(client.get_match_stats(mid) for mid in match_ids[:10]), return_exceptions=True
    )
    recent_match_stats = [m for m in match_stats_results if isinstance(m, dict)]

    return {
        "profile": profile,
        "stats": stats,
        "bans": bans.get("items", []),
        "history_items": history_items,
        "recent_match_stats": recent_match_stats,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def search_faceit_player(query: str) -> dict[str, Any]:
    """Resolve a FACEIT CS2 player from a nickname, profile URL, player_id (UUID), or SteamID64.

    Returns basic identity, skill level, and ELO. Use this first when you're
    unsure a nickname is correct, or to disambiguate before calling other tools.
    """
    try:
        client = get_client()
        profile = await client.resolve_player_id(query)
        games = profile.get("games") or {}
        cs2 = games.get("cs2") or {}
        return {
            "player_id": profile.get("player_id"),
            "nickname": profile.get("nickname"),
            "country": profile.get("country"),
            "cs2_skill_level": cs2.get("skill_level"),
            "cs2_faceit_elo": cs2.get("faceit_elo"),
            "cs2_region": cs2.get("region"),
        }
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001 - tools must never throw
        return _error_payload(exc, query)


@mcp.tool
async def get_player_overview(query: str) -> dict[str, Any]:
    """Get a FACEIT CS2 player's profile, lifetime stats, and active bans.

    `query` accepts a nickname, profile URL, player_id, or SteamID64. Good for
    a quick snapshot before running the full analysis.
    """
    try:
        data = await _gather_overview(query)
        profile = data["profile"]
        games = profile.get("games") or {}
        cs2 = games.get("cs2") or {}
        lifetime = data["stats"].get("lifetime") or {}
        return {
            "identity": {
                "player_id": profile.get("player_id"),
                "nickname": profile.get("nickname"),
                "country": profile.get("country"),
                "skill_level": cs2.get("skill_level"),
                "faceit_elo": cs2.get("faceit_elo"),
                "region": cs2.get("region"),
            },
            "lifetime_stats": lifetime,
            "active_bans": data["bans"],
        }
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def get_map_performance(query: str) -> dict[str, Any]:
    """Get per-map CS2 win rate / K-D breakdown for a FACEIT player, with best/worst maps flagged.

    Maps with fewer than 10 recorded matches are excluded as too small a sample.
    """
    try:
        client = get_client()
        profile = await client.resolve_player_id(query)
        player_id = profile.get("player_id")
        if not player_id:
            raise FaceitAPIError(f"Could not resolve a FACEIT player_id for '{query}'.", status_code=404)
        stats = await client.get_player_stats(player_id)
        from analysis import analyze_maps

        return analyze_maps(stats.get("segments") or [])
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def get_recent_form(query: str, limit: int = 10) -> dict[str, Any]:
    """Get a FACEIT CS2 player's recent match history with per-match stats, trend, streak, and consistency.

    `limit` controls how many recent matches to sample (default 10, max 20).
    """
    try:
        limit = max(1, min(limit, 20))
        data = await _gather_full_diagnostic_inputs(query, history_limit=limit)
        from analysis import analyze_form

        lifetime = data["stats"].get("lifetime") or {}
        player_id = data["profile"].get("player_id")
        form = analyze_form(data["history_items"], data["recent_match_stats"], player_id, lifetime)
        return {
            "player_id": player_id,
            "matches_requested": limit,
            **form,
        }
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def analyze_player(query: str) -> dict[str, Any]:
    """Run the full FACEIT CS2 diagnostic: fetches profile, stats, map breakdown, recent form, and bans,
    then computes strengths, weaknesses (with severity), map insights, form trend, and an overall assessment.

    This is the headline tool — use it whenever the user wants to understand
    their game, get coached, or find out what to improve.
    """
    try:
        data = await _gather_full_diagnostic_inputs(query)
        diagnostic = build_diagnostic(
            profile=data["profile"],
            stats=data["stats"],
            history_items=data["history_items"],
            recent_match_stats=data["recent_match_stats"],
            bans=data["bans"],
        )
        return diagnostic
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def get_improvement_plan(query: str, focus: str | None = None) -> dict[str, Any]:
    """Generate a prioritized, data-backed CS2 improvement plan for a FACEIT player.

    Runs the full diagnostic and maps each detected weakness to concrete drills,
    cadence, and resources, tied to the player's own numbers. Optionally pass
    `focus` (e.g. "aim", "maps", "teamplay", "tilt") to target one area.
    """
    try:
        data = await _gather_full_diagnostic_inputs(query)
        diagnostic = build_diagnostic(
            profile=data["profile"],
            stats=data["stats"],
            history_items=data["history_items"],
            recent_match_stats=data["recent_match_stats"],
            bans=data["bans"],
        )
        plan = build_improvement_plan(diagnostic, focus=focus)
        return {
            "player": diagnostic["profile_summary"],
            "overall_assessment": diagnostic["overall_assessment"],
            **plan,
        }
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


@mcp.prompt
def coach_me(query: str) -> str:
    """Prime a full CS2 coaching conversation for a FACEIT player."""
    return (
        f"Analyze the FACEIT CS2 profile for '{query}' using analyze_player. "
        "Explain their strengths and weaknesses in plain, encouraging terms tied to their actual numbers "
        "(not generic advice). Then call get_improvement_plan for the same player and turn it into a "
        "concrete, prioritized training plan they could start this week. If anything in the data looks "
        "like a small sample size, say so."
    )


# ---------------------------------------------------------------------------
# Health check (plain HTTP GET, for Render's health probe)
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port,
        stateless_http=True,
    )
