# FACEIT CS2 Coaching MCP Server

A remote **MCP server** (Python + [FastMCP](https://github.com/jlowin/fastmcp)) that reads a
player's public **FACEIT CS2** data, diagnoses strengths and weaknesses, and produces a
personalized, data-backed improvement pathway. It's designed to deploy to **Render.com** and be
added as a **remote connector in Claude** on the first try.

The server is **read-only** — it only reads public FACEIT data. There is intentionally **no auth
wall** on the MCP endpoint (see [Enabling real OAuth later](#enabling-real-oauth-later--advanced--needs-persistent-storage));
your FACEIT API key stays server-side and is never exposed to the connected client.

---

## What it exposes

**Tools**

| Tool | What it does |
| --- | --- |
| `search_faceit_player(query)` | Resolve a nickname / profile URL / `player_id` / SteamID64 → identity + level + ELO. Returns a `candidates` list when a nickname is ambiguous. |
| `get_player_overview(query)` | Profile + lifetime stats + active bans. |
| `get_map_performance(query)` | Per-map win rate / K-D, best & worst maps (maps with <10 matches ignored). |
| `get_recent_form(query, limit=10)` | Recent matches + per-match stats, trend, streak, consistency. |
| `benchmark_player(query)` | **Percentile verdicts** for K/D, K/R, HS%, ADR, Win Rate vs same-level, same-region peers (e.g. "bottom-quartile ADR for level 8"). Live peer sampling, with a labelled static-baseline fallback. |
| `get_advanced_stats(query)` | **Leetify** advanced metrics FACEIT doesn't expose — opening-duel %, trade %, utility damage, preaim, reaction time, aim/positioning/utility ratings. Degrades cleanly if the player has no Leetify profile. Carries required attribution. |
| `analyze_player(query)` | **Headline tool** — full diagnostic fusing FACEIT stats + peer benchmarks + Leetify signals: strengths, weaknesses (with severity), map/form insights, overall assessment. Reports progress while it fetches. |
| `get_improvement_plan(query, focus=None)` | Weaknesses → prioritized drills/resources tied to the player's own numbers and percentiles. `focus` can be `aim`, `opening`, `trading`, `utility`, `positioning`, `maps`, `teamplay`, `tilt`. |
| `set_my_profile(query)` / `whoami()` / `clear_my_profile()` | Lightweight per-user identity: say "I'm `<nick>`" once and later tools default to you. Best-effort in-memory; every tool still accepts an explicit `query`. |

**Prompts** — `coach_me(query)`, `pre_match_prep(query, map=None)`, `post_loss_review(query)`, `weekly_review(query)`.

**Resource** — `faceit://player/{query}/analysis` exposes the latest computed analysis (served from
an in-memory TTL cache, recomputed on demand — stateless-safe).

`query` in every tool accepts a **nickname**, a **profile URL** (`faceit.com/en/players/<nick>`), a
raw **`player_id`** (UUID), or a **SteamID64**. An exact-nickname miss falls back to FACEIT search;
an ambiguous nickname resolves to the best match and returns the alternatives so Claude can confirm.

### How the advanced features work

- **Peer benchmarking (no DB).** For the target's region + skill level, the server samples ~40 peers
  from the FACEIT rankings, fetches their lifetime stats concurrently (throttled), and computes the
  target's percentile per metric. The distribution is cached in-memory per `(region, level)` for 6h.
  If sampling is rate-limited or too thin, it falls back to an **approximate static baseline** table
  (in `benchmarks.py`) and clearly labels the output `source: "baseline"`.
- **Leetify bridge.** A FACEIT player's `game_player_id` **is their SteamID64**, and Leetify's public
  profile endpoint (`GET /v3/profile?steam64_id=`) is keyed by SteamID64. So the chain is
  nickname → FACEIT player → Steam64 → Leetify profile. Many players aren't on Leetify; the analysis
  degrades cleanly to FACEIT-only and notes that connecting Leetify would deepen the report.
- **Leetify attribution (required).** Any output containing Leetify-derived data includes
  **"Data Provided by Leetify"** and a **View on Leetify** link
  (`https://leetify.com/app/profile/{steam64}`). This project is not affiliated with or sponsored by
  Leetify.

---

## 1. Get a FACEIT Server-Side API key

1. Go to the **FACEIT Developer Portal / App Studio**: <https://developers.faceit.com/>
2. Create an app and generate a **Server-Side** API key (not a Client-Side key).
3. Keep it secret — it lives only as a server env var (`FACEIT_API_KEY`), never in client-facing code.

The current CS2 game slug is `cs2` (the old `csgo` slug is deprecated and unused here).

---

## 2. Run locally

```bash
python -m venv venv
# Windows: venv\Scripts\activate    macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit .env and set FACEIT_API_KEY=...
python server.py
```

The server starts on `http://0.0.0.0:8000` and the MCP endpoint is at **`http://localhost:8000/mcp`**.

**Test it:**

- Health check: `curl http://localhost:8000/health` → `{"status":"ok"}`
- MCP Inspector: point it at `http://localhost:8000/mcp` (Streamable HTTP transport) and call
  `analyze_player` with a nickname like `donk` or `ZywOo`.

> Note: `PORT` overrides the default 8000 locally if set. On Render it's injected automatically.

---

## 3. Deploy to Render

This repo ships a Render **Blueprint** (`render.yaml`):

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo. Render reads `render.yaml` and creates a free
   web service named `faceit-cs2-mcp`.
3. In the service's **Environment** settings, set **`FACEIT_API_KEY`** to your Server-Side key
   (it's marked `sync: false`, so Render won't read it from the file — set it manually).
   Optionally set **`LEETIFY_API_KEY`** for higher Leetify rate limits (the server works without it).
   `ENABLE_FACEIT_OAUTH` and `ENABLE_ELICITATION` default to `"false"` — leave them.
4. Deploy. Render's health probe hits `/health`; once it's green the service is live at
   `https://<your-service>.onrender.com`.

**How the deploy is wired (and why it connects on the first try):**

- **Streamable HTTP transport** — `mcp.run(transport="http", ...)`, not stdio/SSE.
- **Binds `0.0.0.0`** and reads **`PORT`** from the env var Render injects (falls back to 8000
  locally). No hard-coded port, no `127.0.0.1`.
- **Stateless HTTP** (`stateless_http=True`) so Render's proxy + free-tier cold starts don't break
  in-memory sessions and Claude's connector reconnects cleanly.
- **TLS is terminated by Render.** The app serves plain HTTP on `$PORT`; `.onrender.com` provides
  HTTPS for free. There is deliberately **no cert/SSL code** in Python — that's the classic mistake.
- **`/health`** is a plain `GET` (200 `{"status":"ok"}`) and is Render's `healthCheckPath`; the
  `/mcp/` endpoint is not a normal GET and must not be the health target.
- Every tool **catches its own errors** (404/401/429/timeouts) and returns a structured message
  instead of throwing, so a bad lookup never 500s mid-conversation.

---

## 4. Add it to Claude as a connector

In **Claude → Settings → Connectors → Add custom connector**, use this exact URL:

```
https://<your-service>.onrender.com/mcp
```

(Replace `<your-service>` with your Render service name. The path is `/mcp` — the server also
accepts `/mcp/`.)

> **Render free-tier cold start:** after ~15 min idle the service sleeps and the first request can
> take ~50s to wake. If the first connect attempt times out, just retry — it'll be instant once warm.

---

## Per-user personalization

Two layers, both infra-free:

- **Now (default, no auth, no DB):** `set_my_profile("<nick>")` remembers who you are for the
  conversation, so `analyze_player()` etc. default to you. It's stored in-memory keyed by MCP session
  and is **best-effort** — under `stateless_http=True` there isn't always a stable session id, so
  every tool also accepts an explicit `query`. That explicit argument is the real guarantee: nothing
  breaks across cold starts. FACEIT's public API + Leetify's public API already cover the full
  coaching surface, so this gives a personalized experience with zero auth.

- **Later (real OAuth, disabled):** see the next section.

## Enabling real OAuth later (advanced — needs persistent storage)

A full FACEIT OAuth provider is scaffolded in [`auth_faceit.py`](auth_faceit.py) using FastMCP's
`OAuthProxy` against FACEIT's real OIDC endpoints, gated behind `ENABLE_FACEIT_OAUTH` (default
`false`). When false, the server runs open and connects to Claude first-try. **Do not enable it for a
normal deploy.** Two caveats you must understand first:

1. **Claude.ai DCR issue.** Claude.ai remote connectors have a reported Dynamic Client Registration
   (DCR) `400` failure against FastMCP's `OAuthProxy`. Enabling OAuth may break the connection — test
   in the **MCP Inspector** first, not directly in Claude.
2. **In-memory token storage is not production-usable.** The scaffold leaves `client_storage=None`, so
   OAuth client registrations and tokens live in memory and **do not survive a restart / cold start**.
   Making this production-grade requires a **persistent, encrypted store** (a DB/Redis implementing
   `AsyncKeyValue`), which is deliberately out of scope for this infra-free build. The injection point
   is marked in `auth_faceit.py`.

To experiment: set `ENABLE_FACEIT_OAUTH=true`, `MCP_JWT_SECRET`, `FACEIT_OAUTH_CLIENT_ID`,
`FACEIT_OAUTH_CLIENT_SECRET`, and register `https://<service>.onrender.com/auth/callback` as the
redirect URI in your FACEIT OAuth app.

> Similarly, `ENABLE_ELICITATION` (default `false`) turns on protocol-level `ctx.elicit` prompts.
> It's off because that request/response round-trip is unreliable under `stateless_http` and can hang
> a tool. With it off, ambiguous nicknames resolve to the best match and return a `candidates` list
> for Claude to disambiguate in conversation.

---

## Assumptions made

- FastMCP 3.x standalone package (`from fastmcp import FastMCP`); the MCP endpoint mounts at `/mcp`.
- FACEIT stat fields are treated as strings and parsed defensively (missing fields tolerated).
- Skill-level ELO bands follow FACEIT's published CS2 table; maps need ≥10 matches to count toward
  best/worst; a ≥3-game active loss streak flags tilt risk. **Peer benchmarking** samples ~40 peers
  and caches distributions 6h; the static baseline table is approximate. These thresholds/tables live
  in [`analysis.py`](analysis.py) and [`benchmarks.py`](benchmarks.py) and are easy to tune.
- Leetify field names/paths were confirmed against the live OpenAPI spec at build time.

---

## Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

Unit tests (no network) cover the pure analysis engine (parsing, level bands, fragging/aim/mismatch,
map ranking, form/tilt, end-to-end diagnostic), the **benchmarking percentile math** + static
baseline, and the **Leetify fusion** (weakness detection, attribution, graceful absence).
