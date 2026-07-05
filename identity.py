"""Lightweight per-user identity — in-memory only, no auth, no DB (Phase 5A).

Lets a user say "I'm <nickname>" once so later tools default to them. Because
the server runs stateless_http=True and free-tier cold starts wipe memory, this
is intentionally best-effort:

  * identity is stored in an in-memory map keyed by MCP session id (when the
    transport provides one), with a TTL, and
  * every tool still accepts an explicit `query`, so identity is never *required*
    to be stateful — nothing breaks across cold starts or missing sessions.
"""

from __future__ import annotations

import time
from typing import Any

_TTL_SECONDS = 60 * 60  # remember for an hour of activity
# session_id -> (expires_at, profile_dict)
_identity: dict[str, tuple[float, dict[str, Any]]] = {}


def set_identity(session_id: str | None, profile: dict[str, Any]) -> None:
    if not session_id:
        return
    _identity[session_id] = (time.monotonic() + _TTL_SECONDS, profile)


def get_identity(session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    entry = _identity.get(session_id)
    if entry is None:
        return None
    expires_at, profile = entry
    if time.monotonic() > expires_at:
        _identity.pop(session_id, None)
        return None
    # refresh TTL on use
    _identity[session_id] = (time.monotonic() + _TTL_SECONDS, profile)
    return profile


def clear_identity(session_id: str | None) -> bool:
    if not session_id:
        return False
    return _identity.pop(session_id, None) is not None


def resolve_query(explicit_query: str | None, session_id: str | None) -> str | None:
    """Pick the query to use: an explicit one always wins; otherwise fall back
    to the remembered identity's nickname/player_id for this session."""
    if explicit_query:
        return explicit_query
    remembered = get_identity(session_id)
    if remembered:
        return remembered.get("player_id") or remembered.get("nickname")
    return None
