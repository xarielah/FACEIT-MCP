"""Async client for the Leetify public CS API (Phase 4).

Provides advanced metrics FACEIT's aggregates don't expose — opening-duel win
rate, trade %, utility damage, preaim/crosshair placement, reaction time, and
Leetify sub-ratings (aim/utility/positioning/clutch/opening). No demo parsing.

The bridge from FACEIT: a FACEIT player's `game_player_id` IS their SteamID64,
and Leetify's public profile endpoint is keyed by SteamID64.

Spec: https://api-public-docs.cs-prod.leetify.com/  (base host below).
An API key is optional (higher rate limits); read from LEETIFY_API_KEY.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

LEETIFY_BASE_URL = "https://api-public.cs-prod.leetify.com"
PROFILE_WEB_URL = "https://leetify.com/app/profile/{steam64}"
ATTRIBUTION_TEXT = "Data Provided by Leetify"
REQUEST_TIMEOUT = 12.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 300  # 5 min


class LeetifyError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class _TTLCache:
    def __init__(self, ttl: float = CACHE_TTL_SECONDS):
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)


class LeetifyClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("LEETIFY_API_KEY", "")
        self._cache = _TTLCache()
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Accept": "application/json"}
            if self.api_key:
                # Optional key for higher rate limits. Header name per Leetify dev docs.
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=LEETIFY_BASE_URL, timeout=REQUEST_TIMEOUT, headers=headers
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        cache_key = f"{path}?{params}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached if cached != "__404__" else None

        client = self._http()
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.get(path, params=params)
            except httpx.TimeoutException:
                await asyncio.sleep(2**attempt)
                continue
            except httpx.HTTPError as exc:
                raise LeetifyError(f"Network error contacting Leetify: {exc}") from exc

            if resp.status_code == 200:
                data = resp.json()
                self._cache.set(cache_key, data)
                return data
            if resp.status_code == 404:
                # No Leetify profile for this Steam64 — cache the miss briefly.
                self._cache.set(cache_key, "__404__")
                return None
            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                await asyncio.sleep(float(retry_after) if retry_after else 2**attempt)
                continue
            raise LeetifyError(
                f"Leetify returned status {resp.status_code}.", status_code=resp.status_code
            )
        raise LeetifyError("Leetify request failed after retries.")

    async def get_profile(self, steam64_id: str) -> dict[str, Any] | None:
        """Return the Leetify profile for a SteamID64, or None if not present (404)."""
        if not steam64_id:
            return None
        return await self._get("/v3/profile", params={"steam64_id": steam64_id})


_default_client: LeetifyClient | None = None


def get_leetify_client() -> LeetifyClient:
    global _default_client
    if _default_client is None:
        _default_client = LeetifyClient()
    return _default_client


def profile_web_url(steam64_id: str) -> str:
    return PROFILE_WEB_URL.format(steam64=steam64_id)
