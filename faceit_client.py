"""Async client for the FACEIT Data API (CS2).

Handles auth, retries with backoff on 429/503, and a small in-memory TTL
cache to stay comfortably under FACEIT's rate limits.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import httpx

FACEIT_BASE_URL = "https://open.faceit.com/data/v4"
GAME = "cs2"
CACHE_TTL_SECONDS = 60
REQUEST_TIMEOUT = 10.0
MAX_RETRIES = 3

PROFILE_URL_RE = re.compile(r"faceit\.com/[a-z-]+/players/([^/?#]+)", re.IGNORECASE)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
STEAM64_RE = re.compile(r"^7656119\d{10}$")


class FaceitAPIError(Exception):
    """Raised for FACEIT API errors that callers should handle gracefully."""

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


class FaceitClient:
    """Thin async wrapper around the FACEIT Data API v4."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("FACEIT_API_KEY", "")
        self._cache = _TTLCache()
        self._client: httpx.AsyncClient | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=FACEIT_BASE_URL,
                timeout=REQUEST_TIMEOUT,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise FaceitAPIError(
                "FACEIT_API_KEY is not configured on the server. "
                "Set it as an environment variable (a Server-Side app key from the "
                "FACEIT Developer Portal).",
                status_code=401,
            )

        cache_key = f"{path}?{params}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        client = self._get_http_client()
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(path, params=params)
            except httpx.TimeoutException as exc:
                last_error = exc
                await asyncio.sleep(2**attempt)
                continue
            except httpx.HTTPError as exc:
                raise FaceitAPIError(f"Network error contacting FACEIT API: {exc}") from exc

            if response.status_code == 200:
                data = response.json()
                self._cache.set(cache_key, data)
                return data

            if response.status_code == 404:
                raise FaceitAPIError("Not found on FACEIT.", status_code=404)

            if response.status_code == 401:
                raise FaceitAPIError(
                    "FACEIT API rejected the key (401). Check FACEIT_API_KEY is a valid "
                    "Server-Side app key.",
                    status_code=401,
                )

            if response.status_code in (429, 503):
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2**attempt
                await asyncio.sleep(delay)
                last_error = FaceitAPIError(
                    f"FACEIT API returned {response.status_code}.", status_code=response.status_code
                )
                continue

            raise FaceitAPIError(
                f"FACEIT API returned unexpected status {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )

        raise last_error or FaceitAPIError("FACEIT API request failed after retries.")

    # ------------------------------------------------------------------
    # Input resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_nickname_from_url(text: str) -> str | None:
        match = PROFILE_URL_RE.search(text)
        return match.group(1) if match else None

    async def resolve_player_id(self, query: str) -> dict[str, Any]:
        """Resolve a nickname, profile URL, player_id, or SteamID64 to a player object."""
        query = query.strip()

        url_nickname = self._extract_nickname_from_url(query)
        if url_nickname:
            return await self.get_player_by_nickname(url_nickname)

        if UUID_RE.match(query):
            return await self.get_player_by_id(query)

        if STEAM64_RE.match(query):
            return await self._get("/players", params={"game": GAME, "game_player_id": query})

        # Try exact-nickname lookup first; if that 404s, fall back to the
        # search endpoint and resolve the closest match by full player object.
        try:
            return await self.get_player_by_nickname(query)
        except FaceitAPIError as exc:
            if exc.status_code != 404:
                raise
            search = await self.search_players(query, limit=1)
            items = search.get("items") or []
            if not items:
                raise FaceitAPIError(
                    f"No FACEIT CS2 player found matching '{query}'.", status_code=404
                ) from exc
            best_id = items[0].get("player_id")
            if not best_id:
                raise FaceitAPIError(
                    f"No FACEIT CS2 player found matching '{query}'.", status_code=404
                ) from exc
            return await self.get_player_by_id(best_id)

    # ------------------------------------------------------------------
    # Player endpoints
    # ------------------------------------------------------------------

    async def search_players(self, nickname: str, limit: int = 5) -> dict[str, Any]:
        return await self._get(
            "/search/players", params={"nickname": nickname, "game": GAME, "offset": 0, "limit": limit}
        )

    async def get_player_by_nickname(self, nickname: str) -> dict[str, Any]:
        return await self._get("/players", params={"nickname": nickname})

    async def get_player_by_id(self, player_id: str) -> dict[str, Any]:
        return await self._get(f"/players/{player_id}")

    async def get_player_stats(self, player_id: str) -> dict[str, Any]:
        return await self._get(f"/players/{player_id}/stats/{GAME}")

    async def get_player_history(self, player_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return await self._get(
            f"/players/{player_id}/history",
            params={"game": GAME, "offset": offset, "limit": limit},
        )

    async def get_player_bans(self, player_id: str) -> dict[str, Any]:
        try:
            return await self._get(f"/players/{player_id}/bans")
        except FaceitAPIError as exc:
            if exc.status_code == 404:
                return {"items": []}
            raise

    async def get_match_stats(self, match_id: str) -> dict[str, Any]:
        return await self._get(f"/matches/{match_id}/stats")

    async def get_rankings_around_player(
        self, region: str, player_id: str, limit: int = 40
    ) -> dict[str, Any]:
        """Rankings centered on a player — returns same-ELO-neighborhood peers."""
        return await self._get(
            f"/rankings/games/{GAME}/regions/{region}/players/{player_id}",
            params={"limit": limit},
        )

    async def get_region_rankings(self, region: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        return await self._get(
            f"/rankings/games/{GAME}/regions/{region}", params={"offset": offset, "limit": limit}
        )


_default_client: FaceitClient | None = None


def get_client() -> FaceitClient:
    global _default_client
    if _default_client is None:
        _default_client = FaceitClient()
    return _default_client
