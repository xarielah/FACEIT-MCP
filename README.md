# FACEIT CS2 Coaching MCP Server

A remote **MCP server** (Python + [FastMCP](https://github.com/jlowin/fastmcp)) that reads a
player's public **FACEIT CS2** data, diagnoses strengths and weaknesses, and produces a
personalized, data-backed improvement pathway. It's designed to deploy to **Render.com** and be
added as a **remote connector in Claude** on the first try.

The server is **read-only** — it only reads public FACEIT data. There is intentionally **no auth
wall** on the MCP endpoint (see [Adding auth later](#adding-auth-later)); your FACEIT API key stays
server-side and is never exposed to the connected client.

---

## What it exposes

**Tools**

| Tool | What it does |
| --- | --- |
| `search_faceit_player(query)` | Resolve a nickname / profile URL / `player_id` / SteamID64 → identity + level + ELO. |
| `get_player_overview(query)` | Profile + lifetime stats + active bans. |
| `get_map_performance(query)` | Per-map win rate / K-D, best & worst maps (maps with <10 matches ignored). |
| `get_recent_form(query, limit=10)` | Recent matches + per-match stats, trend, streak, consistency. |
| `analyze_player(query)` | **Headline tool** — full diagnostic: strengths, weaknesses (with severity), map insights, form, overall assessment. |
| `get_improvement_plan(query, focus=None)` | Weaknesses → prioritized drills/resources tied to the player's own numbers. `focus` can be `aim`, `maps`, `teamplay`, `tilt`, etc. |

**Prompt**

- `coach_me(query)` — primes a full coaching conversation (analyze → explain weak spots → training plan).

`query` in every tool accepts a **nickname**, a **profile URL** (`faceit.com/en/players/<nick>`), a
raw **`player_id`** (UUID), or a **SteamID64**. An exact-nickname miss falls back to FACEIT search
and resolves the closest match.

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

## Adding auth later

Auth is intentionally **off** for the first deploy — a broken bearer/OAuth handshake is the #1
reason a remote MCP connector silently fails to attach. The server is read-only, so the endpoint is
left open. When you're ready to lock it down, there's a commented `TokenVerifier` stub at the top of
[`server.py`](server.py):

```python
from fastmcp.server.auth import TokenVerifier
auth_provider = TokenVerifier(...)
mcp = FastMCP("faceit-cs2-coach", auth=auth_provider)
```

Wire the token/issuer through Render env vars and only enable it **after** confirming the connector
works without auth.

---

## Assumptions made

- FastMCP 3.x standalone package (`from fastmcp import FastMCP`); the MCP endpoint mounts at `/mcp`.
- FACEIT stat fields are treated as strings and parsed defensively (missing fields tolerated).
- Skill-level ELO bands follow FACEIT's published CS2 table; maps need ≥10 matches to count toward
  best/worst; a ≥3-game active loss streak flags tilt risk. These thresholds live in
  [`analysis.py`](analysis.py) and are easy to tune.

---

## Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

Unit tests cover the pure analysis engine (parsing, level bands, fragging/aim/mismatch detection,
map ranking, form/tilt, and an end-to-end diagnostic) — no network needed.
