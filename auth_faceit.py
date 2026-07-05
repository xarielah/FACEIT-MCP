"""FACEIT OAuth scaffold — DISABLED by default (Phase 5B).

This wires a FastMCP `OAuthProxy` against FACEIT's real OIDC endpoints so a
per-user, authenticated experience is *ready*, but it is gated behind the
`ENABLE_FACEIT_OAUTH` env var (default "false"). When false, the server runs
open exactly as today and connects to Claude first-try.

Two caveats you MUST understand before enabling this (documented in the README):

  1. Claude.ai remote connectors have a reported Dynamic Client Registration
     (DCR) 400 failure against FastMCP's OAuthProxy. Enabling this may break the
     connection. Test in the MCP Inspector first, not directly in Claude.

  2. Client/token storage here is IN-MEMORY (`client_storage` left as None →
     FastMCP's default in-memory store). That means registrations and tokens do
     NOT survive a restart / cold start, so this is NOT production-usable as-is.
     A persistent, encrypted store (a DB/Redis implementing AsyncKeyValue) would
     be injected at the marked spot below — that persistence layer is deliberately
     out of scope for this infra-free build.

FACEIT OIDC discovery (https://api.faceit.com/auth/v1/openid_configuration):
  issuer:        https://api.faceit.com/auth
  authorize:     https://accounts.faceit.com
  token:         https://api.faceit.com/auth/v1/oauth/token
  userinfo:      https://api.faceit.com/auth/v1/resources/userinfo
  jwks:          https://api.faceit.com/auth/v1/oauth/certs
  scopes:        openid, email, profile, membership
"""

from __future__ import annotations

import os

# FACEIT OIDC endpoints (verified from their discovery document).
FACEIT_AUTHORIZE_ENDPOINT = "https://accounts.faceit.com"
FACEIT_TOKEN_ENDPOINT = "https://api.faceit.com/auth/v1/oauth/token"
FACEIT_JWKS_URI = "https://api.faceit.com/auth/v1/oauth/certs"
FACEIT_ISSUER = "https://api.faceit.com/auth"
FACEIT_SCOPES = ["openid", "email", "profile"]

# Callback path registered as the redirect URI in your FACEIT OAuth app.
# Full URL must be:  https://<your-service>.onrender.com/auth/callback
FACEIT_REDIRECT_PATH = "/auth/callback"


def oauth_enabled() -> bool:
    return os.environ.get("ENABLE_FACEIT_OAUTH", "false").strip().lower() in ("1", "true", "yes")


def build_faceit_oauth_provider():
    """Construct the FACEIT OAuthProxy. Only called when ENABLE_FACEIT_OAUTH=true.

    Requires env vars (placeholders in render.yaml):
      FACEIT_OAUTH_CLIENT_ID, FACEIT_OAUTH_CLIENT_SECRET  — from the FACEIT OAuth app
      MCP_JWT_SECRET                                       — signs FastMCP-issued JWTs
      RENDER_EXTERNAL_URL or PUBLIC_BASE_URL               — the https base URL
    """
    from fastmcp.server.auth import OAuthProxy
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    client_id = os.environ.get("FACEIT_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("FACEIT_OAUTH_CLIENT_SECRET", "")
    jwt_secret = os.environ.get("MCP_JWT_SECRET", "")
    base_url = (
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or "http://localhost:8000"
    )

    if not (client_id and client_secret and jwt_secret):
        raise RuntimeError(
            "ENABLE_FACEIT_OAUTH=true but FACEIT_OAUTH_CLIENT_ID / "
            "FACEIT_OAUTH_CLIENT_SECRET / MCP_JWT_SECRET are not all set."
        )

    # Verifies FACEIT's upstream access/id tokens via their JWKS.
    token_verifier = JWTVerifier(
        jwks_uri=FACEIT_JWKS_URI,
        issuer=FACEIT_ISSUER,
        algorithm="RS256",
    )

    return OAuthProxy(
        upstream_authorization_endpoint=FACEIT_AUTHORIZE_ENDPOINT,
        upstream_token_endpoint=FACEIT_TOKEN_ENDPOINT,
        upstream_client_id=client_id,
        upstream_client_secret=client_secret,
        token_verifier=token_verifier,
        base_url=base_url,
        redirect_path=FACEIT_REDIRECT_PATH,
        valid_scopes=FACEIT_SCOPES,
        # FastMCP-issued JWTs are signed with this secret so they survive restarts.
        jwt_signing_key=jwt_secret,
        # ---------------------------------------------------------------
        # PERSISTENCE INJECTION POINT (out of scope for this build):
        #   client_storage=YourAsyncKeyValueStore(...)
        # Without a persistent, encrypted store, registrations/tokens are
        # in-memory only and do not survive a restart — not production-usable.
        # ---------------------------------------------------------------
        client_storage=None,
    )
