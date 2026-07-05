"""Live peer sampling for percentile benchmarking (Phase 1).

Samples same-level peers from the FACEIT rankings, fetches their lifetime stats
concurrently (throttled), and computes the target's percentile per metric. The
computed peer distribution is cached in-memory (long TTL) keyed by
(region, level_band) so we don't resample on every call. Falls back to the
static baseline in benchmarks.py when sampling is unavailable or too thin.

No database — the cache is a plain dict with TTL, same pattern as the API cache.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from faceit_client import FaceitClient, FaceitAPIError
from benchmarks import (
    BENCHMARK_METRICS,
    build_verdict,
    extract_metric,
    verdicts_to_weaknesses,
)

SAMPLE_SIZE = 40           # peers to sample per (region, level) distribution
MIN_VALID_SAMPLES = 8      # below this, fall back to the static baseline
SAMPLE_TTL_SECONDS = 6 * 60 * 60   # cache distributions for 6h
CONCURRENCY = 6            # cap concurrent stat fetches to respect rate limits

# In-memory distribution cache: (region, level) -> (expires_at, {metric: [values]})
_distribution_cache: dict[tuple[str, int], tuple[float, dict[str, list[float]]]] = {}


def _cache_get(region: str, level: int) -> dict[str, list[float]] | None:
    entry = _distribution_cache.get((region, level))
    if entry is None:
        return None
    expires_at, dist = entry
    if time.monotonic() > expires_at:
        _distribution_cache.pop((region, level), None)
        return None
    return dist


def _cache_set(region: str, level: int, dist: dict[str, list[float]]) -> None:
    _distribution_cache[(region, level)] = (time.monotonic() + SAMPLE_TTL_SECONDS, dist)


async def _sample_peer_distribution(
    client: FaceitClient,
    region: str,
    level: int,
    player_id: str,
    progress: Callable[[float, float, str], Awaitable[None]] | None = None,
) -> dict[str, list[float]] | None:
    """Build a {metric: [peer values]} distribution, or None if sampling failed."""
    cached = _cache_get(region, level)
    if cached is not None:
        return cached

    try:
        rankings = await client.get_rankings_around_player(region, player_id, limit=SAMPLE_SIZE)
    except FaceitAPIError:
        return None

    peers = [
        item.get("player_id")
        for item in (rankings.get("items") or [])
        if item.get("player_id") and item.get("player_id") != player_id
        and item.get("game_skill_level") in (level, level - 1, level + 1)
    ]
    if len(peers) < MIN_VALID_SAMPLES:
        # widen: accept any peer in the ranking neighborhood
        peers = [
            item.get("player_id")
            for item in (rankings.get("items") or [])
            if item.get("player_id") and item.get("player_id") != player_id
        ]
    if len(peers) < MIN_VALID_SAMPLES:
        return None

    semaphore = asyncio.Semaphore(CONCURRENCY)
    total = len(peers)
    done = 0

    async def fetch_stats(pid: str) -> dict[str, Any] | None:
        nonlocal done
        async with semaphore:
            try:
                result = await client.get_player_stats(pid)
            except FaceitAPIError:
                result = None
            done += 1
            if progress:
                await progress(done, total, f"Sampling peers ({done}/{total})")
            return result

    results = await asyncio.gather(*(fetch_stats(pid) for pid in peers))

    distribution: dict[str, list[float]] = {m: [] for m in BENCHMARK_METRICS}
    valid = 0
    for stats in results:
        if not stats:
            continue
        lifetime = stats.get("lifetime") or {}
        got_any = False
        for metric in BENCHMARK_METRICS:
            val = extract_metric(lifetime, metric)
            if val is not None:
                distribution[metric].append(val)
                got_any = True
        if got_any:
            valid += 1

    if valid < MIN_VALID_SAMPLES:
        return None

    _cache_set(region, level, distribution)
    return distribution


async def benchmark_player(
    client: FaceitClient,
    profile: dict[str, Any],
    stats: dict[str, Any],
    progress: Callable[[float, float, str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Return percentile verdicts for the target's key metrics vs same-level peers.

    Uses live sampling when possible, otherwise the static baseline. Always
    returns something usable.
    """
    cs2 = (profile.get("games") or {}).get("cs2") or {}
    level = int(cs2.get("skill_level") or 0)
    region = cs2.get("region") or "EU"
    player_id = profile.get("player_id") or ""
    lifetime = stats.get("lifetime") or {}

    distribution = None
    if level and player_id:
        distribution = await _sample_peer_distribution(client, region, level, player_id, progress)

    source = "sampled" if distribution else "baseline"
    peers_used = 0
    if distribution:
        peers_used = max((len(v) for v in distribution.values()), default=0)

    verdicts = []
    for metric in BENCHMARK_METRICS:
        value = extract_metric(lifetime, metric)
        samples = distribution.get(metric) if distribution else None
        verdicts.append(build_verdict(metric, value, level or 1, samples, source))

    weaknesses = verdicts_to_weaknesses(verdicts)

    return {
        "level": level,
        "region": region,
        "source": source,
        "peers_sampled": peers_used,
        "approximate": source == "baseline",
        "note": (
            "Percentiles computed from a live peer sample at this level/region."
            if source == "sampled"
            else "Live peer sampling unavailable — percentiles are approximate, from the static baseline table."
        ),
        "verdicts": verdicts,
        "relative_weaknesses": weaknesses,
    }
