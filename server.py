"""FACEIT CS2 Coaching MCP server.

Read-only FACEIT Data API access + a rules-based analysis/coaching engine,
now with peer-percentile benchmarking (Phase 1), MCP-native features
(elicitation / progress / resources / prompts, Phase 2), Leetify advanced-metric
fusion (Phase 4), and lightweight per-user identity (Phase 5). Served over
Streamable HTTP for remote connectors and deployable to Render.

Infra-free: all caching and transient state is in-memory (dicts with TTL).
No database, no Redis, no external store.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from fastmcp import Context, FastMCP

from faceit_client import FaceitAPIError, get_client
from leetify_client import get_leetify_client
from analysis import build_diagnostic
from coaching_data import build_improvement_plan
from benchmarking import benchmark_player as compute_benchmark
import identity
import auth_faceit

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Auth: OFF by default. The MCP endpoint stays open (read-only public data) so
# the connector never breaks on an auth handshake. A full FACEIT OAuth provider
# is scaffolded in auth_faceit.py and only attached when ENABLE_FACEIT_OAUTH=true
# (see that file for the two caveats: Claude DCR issue + in-memory token storage).
# ---------------------------------------------------------------------------
_auth_provider = None
if auth_faceit.oauth_enabled():
    try:
        _auth_provider = auth_faceit.build_faceit_oauth_provider()
    except Exception as exc:  # noqa: BLE001 - never let auth setup crash startup
        print(f"[warn] ENABLE_FACEIT_OAUTH set but provider build failed: {exc}. Running open.")
        _auth_provider = None

mcp = FastMCP(name="faceit-cs2-coach", auth=_auth_provider)

# In-memory cache of the latest computed diagnostic, keyed by player_id, for the
# analysis resource to read back without recomputing. TTL keeps it stateless-safe.
_ANALYSIS_TTL = 10 * 60
_analysis_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_analysis(player_id: str, diagnostic: dict[str, Any]) -> None:
    if player_id:
        _analysis_cache[player_id] = (time.monotonic() + _ANALYSIS_TTL, diagnostic)


def _get_cached_analysis(player_id: str) -> dict[str, Any] | None:
    entry = _analysis_cache.get(player_id)
    if entry is None:
        return None
    expires_at, diag = entry
    if time.monotonic() > expires_at:
        _analysis_cache.pop(player_id, None)
        return None
    return diag


def _error_payload(exc: Exception, query: str | None = None) -> dict[str, Any]:
    return {
        "error": True,
        "status_code": getattr(exc, "status_code", None),
        "message": str(exc),
        "query": query,
    }


def _session_id(ctx: Context | None) -> str | None:
    if ctx is None:
        return None
    try:
        return ctx.session_id
    except Exception:  # noqa: BLE001
        return None


# Protocol-level elicitation (ctx.elicit) needs a stable session to carry the
# server->client request/response round-trip. We run stateless_http=True (a §0
# requirement so Claude reconnects cleanly across cold starts), where that
# round-trip is unreliable and can hang the tool waiting for a reply that can't
# be routed back. So elicitation is OFF by default and only enabled for stateful
# deployments via ENABLE_ELICITATION=true. When off, ambiguity is handled by
# auto-resolving to the best candidate and surfacing the candidate list so the
# model can ask the user in plain conversation (which works over any transport).
def _elicitation_enabled() -> bool:
    return os.environ.get("ENABLE_ELICITATION", "false").strip().lower() in ("1", "true", "yes")


async def _safe_elicit(ctx: Context | None, message: str, options: list[str]) -> str | None:
    """Elicit a choice, but never hang: disabled unless ENABLE_ELICITATION=true,
    and bounded by a timeout so a non-responding client falls back gracefully."""
    if ctx is None or not _elicitation_enabled():
        return None
    try:
        result = await asyncio.wait_for(
            ctx.elicit(message=message, response_type=options), timeout=90
        )
        if getattr(result, "action", None) == "accept" and getattr(result, "data", None):
            return result.data
    except Exception:  # noqa: BLE001 - timeout/decline/cancel/unsupported all fall back
        return None
    return None


def _pick_best_candidate(query: str, items: list[dict[str, Any]]) -> str:
    """Choose the best player_id from search candidates: an exact case-insensitive
    nickname match wins; otherwise the highest skill level; otherwise the top result."""
    ql = query.strip().lower()
    exact = [it for it in items if str(it.get("nickname", "")).lower() == ql]
    if exact:
        return exact[0]["player_id"]

    def level_of(it: dict[str, Any]) -> int:
        games = it.get("games")
        if isinstance(games, dict):
            return int((games.get("cs2") or {}).get("skill_level") or 0)
        return 0

    return sorted(items, key=level_of, reverse=True)[0]["player_id"]


def _candidate_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        games = it.get("games")
        lvl = (games.get("cs2") or {}).get("skill_level") if isinstance(games, dict) else None
        out.append(
            {
                "nickname": it.get("nickname"),
                "player_id": it.get("player_id"),
                "country": it.get("country"),
                "skill_level": lvl,
            }
        )
    return out


async def _resolve_query(explicit: str | None, ctx: Context | None) -> str:
    """Resolve the effective query, defaulting to remembered identity, and error
    clearly if neither is available."""
    q = identity.resolve_query(explicit, _session_id(ctx))
    if not q:
        raise FaceitAPIError(
            "No player specified and no saved identity for this session. "
            "Pass a nickname/URL/id, or call set_my_profile first.",
            status_code=400,
        )
    return q


async def _resolve_profile(
    query: str, ctx: Context | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve a query to a FACEIT player. Returns (profile, ambiguous_candidates).

    On an ambiguous nickname: optionally elicit a pick (only when
    ENABLE_ELICITATION=true), otherwise auto-resolve to the best candidate and
    return the candidate list so the caller can surface it for the model to
    disambiguate in conversation. Never hangs.
    """
    client = get_client()
    q = query.strip()

    # URL / UUID / steam64 resolve unambiguously — hand off to the client.
    from faceit_client import PROFILE_URL_RE, UUID_RE, STEAM64_RE

    if PROFILE_URL_RE.search(q) or UUID_RE.match(q) or STEAM64_RE.match(q):
        return await client.resolve_player_id(q), []

    # Nickname: exact match first.
    try:
        return await client.get_player_by_nickname(q), []
    except FaceitAPIError as exc:
        if exc.status_code != 404:
            raise

    # No exact match: search for candidates.
    search = await client.search_players(q, limit=5)
    items = [it for it in (search.get("items") or []) if it.get("player_id")]
    if not items:
        raise FaceitAPIError(f"No FACEIT CS2 player found matching '{q}'.", status_code=404)
    if len(items) == 1:
        return await client.get_player_by_id(items[0]["player_id"]), []

    # Multiple candidates. Try elicitation (only if explicitly enabled); else
    # auto-pick the best match and report the alternatives.
    labels = [f"{c['nickname']} ({c.get('country','?')}, lvl {c.get('skill_level','?')})" for c in _candidate_summary(items)]
    label_to_id = {labels[i]: items[i]["player_id"] for i in range(len(items))}
    picked = await _safe_elicit(ctx, f"Multiple FACEIT players match '{q}'. Which one?", labels)
    if picked and picked in label_to_id:
        return await client.get_player_by_id(label_to_id[picked]), []

    best_id = _pick_best_candidate(q, items)
    candidates = _candidate_summary(items)
    return await client.get_player_by_id(best_id), candidates


# ---------------------------------------------------------------------------
# Core orchestration (shared by tools + the analysis resource)
# ---------------------------------------------------------------------------


async def _run_full_analysis(
    query: str,
    ctx: Context | None,
    with_benchmark: bool = True,
    with_leetify: bool = True,
) -> dict[str, Any]:
    client = get_client()

    async def progress(done: float, total: float, msg: str) -> None:
        if ctx is not None:
            try:
                await ctx.report_progress(progress=done, total=total, message=msg)
            except Exception:  # noqa: BLE001
                pass

    async def log(msg: str) -> None:
        if ctx is not None:
            try:
                await ctx.info(msg)
            except Exception:  # noqa: BLE001
                pass

    await progress(1, 10, "Resolving player")
    profile, ambiguous = await _resolve_profile(query, ctx)
    player_id = profile.get("player_id")
    if not player_id:
        raise FaceitAPIError(f"Could not resolve a FACEIT player_id for '{query}'.", status_code=404)
    await log(f"Resolved {profile.get('nickname')} ({player_id})")

    await progress(2, 10, "Fetching stats, history, bans")
    stats, bans, history = await asyncio.gather(
        client.get_player_stats(player_id),
        client.get_player_bans(player_id),
        client.get_player_history(player_id, limit=15),
    )
    history_items = history.get("items", [])

    await progress(4, 10, "Fetching recent match detail")
    match_ids = [it.get("match_id") for it in history_items if it.get("match_id")]
    match_results = await asyncio.gather(
        *(client.get_match_stats(mid) for mid in match_ids[:10]), return_exceptions=True
    )
    recent_match_stats = [m for m in match_results if isinstance(m, dict)]

    # Leetify (Phase 4) — via the FACEIT game_player_id == SteamID64 bridge.
    leetify_profile = None
    if with_leetify:
        steam64 = (profile.get("games", {}).get("cs2", {}) or {}).get("game_player_id")
        if steam64:
            await progress(6, 10, "Fetching Leetify advanced stats")
            try:
                leetify_profile = await get_leetify_client().get_profile(steam64)
            except Exception as exc:  # noqa: BLE001 - Leetify optional
                await log(f"Leetify unavailable: {exc}")
                leetify_profile = None

    # Benchmark (Phase 1) — peer percentile sampling with progress.
    benchmark = None
    if with_benchmark:
        await progress(7, 10, "Benchmarking against peers")
        try:
            benchmark = await compute_benchmark(client, profile, stats, progress=progress)
        except Exception as exc:  # noqa: BLE001 - benchmarking optional
            await log(f"Benchmarking failed, continuing without: {exc}")
            benchmark = None

    await progress(9, 10, "Computing diagnostic")
    diagnostic = build_diagnostic(
        profile=profile,
        stats=stats,
        history_items=history_items,
        recent_match_stats=recent_match_stats,
        bans=bans.get("items", []),
        leetify_profile=leetify_profile,
        benchmark=benchmark,
    )
    if ambiguous:
        diagnostic["ambiguous_query"] = {
            "note": f"'{query}' matched multiple players; analyzed the best match "
            f"({profile.get('nickname')}). If that's not who you meant, pick another below or pass a player_id.",
            "candidates": ambiguous,
        }
    _cache_analysis(player_id, diagnostic)
    await progress(10, 10, "Done")
    return diagnostic


# ---------------------------------------------------------------------------
# Identity tools (Phase 5A)
# ---------------------------------------------------------------------------


@mcp.tool
async def set_my_profile(query: str, ctx: Context) -> dict[str, Any]:
    """Remember who the current user is (their FACEIT/Steam identity) for this
    conversation, so later tools default to them. `query` = nickname/URL/id/steam64.

    Identity is best-effort in-memory (per MCP session); every other tool still
    accepts an explicit `query`, so nothing breaks if it's forgotten.
    """
    try:
        profile, ambiguous = await _resolve_profile(query, ctx)
        cs2 = (profile.get("games") or {}).get("cs2") or {}
        record = {
            "player_id": profile.get("player_id"),
            "nickname": profile.get("nickname"),
            "country": profile.get("country"),
            "skill_level": cs2.get("skill_level"),
            "faceit_elo": cs2.get("faceit_elo"),
            "steam64": cs2.get("game_player_id"),
        }
        identity.set_identity(_session_id(ctx), record)
        out = {"saved": True, "identity": record, "note": "Future tools will default to this player when no query is given."}
        if ambiguous:
            out["ambiguous_candidates"] = ambiguous
            out["note"] += f" Note: '{query}' was ambiguous; saved the best match — confirm it's correct."
        return out
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def whoami(ctx: Context) -> dict[str, Any]:
    """Show the identity currently remembered for this session (if any)."""
    record = identity.get_identity(_session_id(ctx))
    if not record:
        return {"identity": None, "note": "No saved identity. Use set_my_profile to set one."}
    return {"identity": record}


@mcp.tool
async def clear_my_profile(ctx: Context) -> dict[str, Any]:
    """Forget the remembered identity for this session."""
    cleared = identity.clear_identity(_session_id(ctx))
    return {"cleared": cleared}


# ---------------------------------------------------------------------------
# Data tools
# ---------------------------------------------------------------------------


@mcp.tool
async def search_faceit_player(query: str, ctx: Context) -> dict[str, Any]:
    """Resolve a FACEIT CS2 player from a nickname, profile URL, player_id (UUID), or SteamID64.

    Returns basic identity, skill level, and ELO. If a nickname matches multiple
    players, returns the best match plus a `candidates` list so you can confirm
    or pick a different one.
    """
    try:
        q = await _resolve_query(query, ctx)
        profile, candidates = await _resolve_profile(q, ctx)
        cs2 = (profile.get("games") or {}).get("cs2") or {}
        out = {
            "player_id": profile.get("player_id"),
            "nickname": profile.get("nickname"),
            "country": profile.get("country"),
            "cs2_skill_level": cs2.get("skill_level"),
            "cs2_faceit_elo": cs2.get("faceit_elo"),
            "cs2_region": cs2.get("region"),
            "steam64": cs2.get("game_player_id"),
        }
        if candidates:
            out["ambiguous"] = True
            out["candidates"] = candidates
            out["note"] = f"'{q}' matched multiple players; returned the best match. Other candidates listed."
        return out
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def get_player_overview(query: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """Get a FACEIT CS2 player's profile, lifetime stats, and active bans.

    `query` accepts a nickname, profile URL, player_id, or SteamID64. If omitted,
    defaults to your saved identity (set_my_profile).
    """
    try:
        q = await _resolve_query(query, ctx)
        client = get_client()
        profile, _cands = await _resolve_profile(q, ctx)
        player_id = profile.get("player_id")
        stats, bans = await asyncio.gather(
            client.get_player_stats(player_id), client.get_player_bans(player_id)
        )
        cs2 = (profile.get("games") or {}).get("cs2") or {}
        return {
            "identity": {
                "player_id": player_id,
                "nickname": profile.get("nickname"),
                "country": profile.get("country"),
                "skill_level": cs2.get("skill_level"),
                "faceit_elo": cs2.get("faceit_elo"),
                "region": cs2.get("region"),
            },
            "lifetime_stats": stats.get("lifetime") or {},
            "active_bans": bans.get("items", []),
        }
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def get_map_performance(query: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """Per-map CS2 win rate / K-D breakdown, with best/worst maps flagged.
    Maps with fewer than 10 recorded matches are excluded. Defaults to saved identity."""
    try:
        q = await _resolve_query(query, ctx)
        client = get_client()
        profile, _cands = await _resolve_profile(q, ctx)
        stats = await client.get_player_stats(profile.get("player_id"))
        from analysis import analyze_maps

        return analyze_maps(stats.get("segments") or [])
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def get_recent_form(query: str | None = None, limit: int = 10, ctx: Context = None) -> dict[str, Any]:
    """Recent match history with per-match stats, trend, streak, and consistency.
    `limit` = matches to sample (default 10, max 20). Defaults to saved identity."""
    try:
        q = await _resolve_query(query, ctx)
        limit = max(1, min(limit, 20))
        client = get_client()
        profile, _cands = await _resolve_profile(q, ctx)
        player_id = profile.get("player_id")
        stats, history = await asyncio.gather(
            client.get_player_stats(player_id),
            client.get_player_history(player_id, limit=limit),
        )
        history_items = history.get("items", [])
        match_ids = [it.get("match_id") for it in history_items if it.get("match_id")]
        match_results = await asyncio.gather(
            *(client.get_match_stats(mid) for mid in match_ids[:10]), return_exceptions=True
        )
        recent_match_stats = [m for m in match_results if isinstance(m, dict)]
        from analysis import analyze_form

        form = analyze_form(history_items, recent_match_stats, player_id, stats.get("lifetime") or {})
        return {"player_id": player_id, "matches_requested": limit, **form}
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def benchmark_player(query: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """Benchmark a player's key stats (K/D, K/R, HS%, ADR, Win Rate) as PERCENTILES
    versus same-level, same-region peers — turning each number into a verdict
    (e.g. "bottom-quartile ADR for level 8"). Uses live peer sampling, falling back
    to an approximate static baseline (clearly labelled) when sampling is unavailable.
    Defaults to saved identity."""
    try:
        q = await _resolve_query(query, ctx)
        client = get_client()
        profile, _cands = await _resolve_profile(q, ctx)
        stats = await client.get_player_stats(profile.get("player_id"))

        async def progress(done, total, msg):
            if ctx is not None:
                try:
                    await ctx.report_progress(progress=done, total=total, message=msg)
                except Exception:  # noqa: BLE001
                    pass

        result = await compute_benchmark(client, profile, stats, progress=progress)
        result["nickname"] = profile.get("nickname")
        return result
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def get_advanced_stats(query: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """Advanced CS2 metrics from Leetify that FACEIT doesn't expose — opening-duel
    win rate, trade %, utility damage, preaim/crosshair placement, reaction time,
    and aim/positioning/utility sub-ratings. Degrades cleanly if the player has no
    Leetify profile. Any Leetify data includes required attribution + profile link.
    Defaults to saved identity."""
    try:
        q = await _resolve_query(query, ctx)
        client = get_client()
        profile, _cands = await _resolve_profile(q, ctx)
        cs2 = (profile.get("games") or {}).get("cs2") or {}
        steam64 = cs2.get("game_player_id")
        from analysis import analyze_leetify

        leetify_profile = None
        if steam64:
            try:
                leetify_profile = await get_leetify_client().get_profile(steam64)
            except Exception as exc:  # noqa: BLE001
                return {
                    "available": False,
                    "steam64": steam64,
                    "note": f"Leetify lookup failed: {exc}. FACEIT-only analysis still works.",
                }
        result = analyze_leetify(leetify_profile, steam64)
        result["nickname"] = profile.get("nickname")
        return result
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def analyze_player(query: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """Full FACEIT CS2 diagnostic — the headline tool. Pulls profile, stats, maps,
    recent form, bans, Leetify advanced metrics, and peer-percentile benchmarks,
    then returns strengths, weaknesses (with severity), map/form insights, and an
    overall assessment. Reports progress as it fetches. Defaults to saved identity.
    Includes "Data Provided by Leetify" attribution whenever Leetify data is used."""
    try:
        q = await _resolve_query(query, ctx)
        return await _run_full_analysis(q, ctx)
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


@mcp.tool
async def get_improvement_plan(query: str | None = None, focus: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """Prioritized, data-backed CS2 improvement plan. Runs the full diagnostic
    (FACEIT + Leetify + benchmarks) and maps each weakness to concrete drills,
    cadence, and resources tied to the player's own numbers. If `focus` is omitted,
    it will ask what to focus on. Defaults to saved identity."""
    try:
        q = await _resolve_query(query, ctx)

        # Elicit a focus area when none was given (Phase 2 elicitation).
        if not focus:
            options = ["Everything (full plan)", "aim", "opening/entry", "trading", "utility", "positioning", "maps", "tilt/consistency"]
            picked = await _safe_elicit(ctx, "What do you want the training plan to focus on?", options)
            if picked and not picked.startswith("Everything"):
                focus = picked.split("/")[0]

        diagnostic = await _run_full_analysis(q, ctx)
        if diagnostic.get("error"):
            return diagnostic
        plan = build_improvement_plan(diagnostic, focus=focus)
        out = {
            "player": diagnostic["profile_summary"],
            "overall_assessment": diagnostic["overall_assessment"],
            **plan,
        }
        if diagnostic.get("attribution"):
            out["attribution"] = diagnostic["attribution"]
            out["leetify_profile_url"] = diagnostic.get("leetify_profile_url")
        return out
    except FaceitAPIError as exc:
        return _error_payload(exc, query)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


# ---------------------------------------------------------------------------
# Resource (Phase 2) — latest computed analysis, recomputed on demand
# ---------------------------------------------------------------------------


@mcp.resource("faceit://player/{query}/analysis")
async def player_analysis_resource(query: str) -> dict[str, Any]:
    """The player's latest computed analysis. Served from the in-memory TTL cache
    when fresh, otherwise recomputed (stateless-safe)."""
    try:
        client = get_client()
        profile = await client.resolve_player_id(query)
        cached = _get_cached_analysis(profile.get("player_id") or "")
        if cached is not None:
            return cached
        return await _run_full_analysis(query, ctx=None)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc, query)


# ---------------------------------------------------------------------------
# Prompt library (Phase 2)
# ---------------------------------------------------------------------------


@mcp.prompt
def coach_me(query: str) -> str:
    """Prime a full CS2 coaching conversation for a FACEIT player."""
    return (
        f"Analyze the FACEIT CS2 profile for '{query}' using analyze_player. Explain their strengths "
        "and weaknesses in plain, encouraging terms tied to their actual numbers and peer percentiles "
        "(not generic advice). If Leetify advanced data is present, use it and surface the "
        "'Data Provided by Leetify' attribution and profile link. Then call get_improvement_plan and "
        "turn it into a concrete, prioritized training plan for this week. Note any small-sample caveats."
    )


@mcp.prompt
def pre_match_prep(query: str, map: str | None = None) -> str:
    """Prime a pre-match preparation conversation."""
    map_line = f" They are about to play {map}." if map else ""
    return (
        f"'{query}' is about to queue a FACEIT CS2 match.{map_line} Call analyze_player and "
        "get_map_performance. Give a tight pre-match brief: which maps to pick/ban based on their win "
        "rates, one or two concrete things to focus on this session (from their weaknesses and Leetify "
        "opening/trade/utility signals), and a 10-minute warmup routine. Keep it short and actionable."
    )


@mcp.prompt
def post_loss_review(query: str) -> str:
    """Prime a calm, constructive post-loss review."""
    return (
        f"'{query}' just lost and may be tilted. Call get_recent_form and analyze_player. First check for "
        "a loss streak / declining form and address tilt and session management directly but kindly. Then "
        "pick the single highest-leverage weakness (prefer team-play / trading / decision signals over raw "
        "aim if the data points there) and give one focused thing to work on. Avoid a laundry list."
    )


@mcp.prompt
def weekly_review(query: str) -> str:
    """Prime a weekly progress review + plan."""
    return (
        f"Run a weekly CS2 review for '{query}'. Call analyze_player (for benchmarks + Leetify), "
        "get_recent_form, and get_improvement_plan. Summarize where they stand versus peers at their level, "
        "what trended up or down this week, and set 2-3 concrete goals for next week with drills and cadence. "
        "Include Leetify attribution if Leetify data is used."
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
