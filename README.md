# MiniApps Agent API

A small FastAPI service that turns a signed-in **miniapps.ai** browser session into a
clean, key-authenticated REST API — plus an MCP server so agents can drive it directly.

It started life as this one-off script:

```python
requests.put(
    "https://api.miniapps.ai/conversations/<uuid>",
    cookies={"jwt": ..., "__Host-miniapps.x-csrf-token": ...},
    headers={"x-csrf-token": ...},
    json={"toolSettings": {"webSearch": True, "codeInterpreter": True, ...}},
)
```

...now with auth, multi-user sessions, partial updates, credential reseeding, Docker,
and a Railway deploy config.

---

## Features

- **Tool-settings API** — read and update the six conversation toggles
  (`webSearch`, `codeInterpreter`, `canvas`, `flightSearch`, `locationSearch`,
  `conversationLookup`).
- **True partial updates** — the upstream `PUT` replaces the whole `toolSettings`
  object, so the service reads current state and merges. `?merge=false` reproduces the
  raw all-or-nothing browser behaviour.
- **Multi-user** — one API key per miniapps account, resolved with a constant-time
  comparison. Keys map to isolated sessions.
- **Credential reseeding** — paste a fresh DevTools capture into `POST /auth/reseed`
  and keep serving without a redeploy.
- **MCP built in** — SSE at `/mcp/sse`, or stdio via `mcp_stdio.py`.
- **Raw passthrough** — `/v1/raw/{path}` reaches any endpoint not modelled here.
- **OpenAI-style errors** — `{"error": {"message", "type", "code"}}`.
- **Deploy-ready** — Dockerfile + `railway.json` with a `/health` healthcheck.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # set MINIAPPS_API_KEY

# Option A: paste a DevTools capture and let the app parse it
$EDITOR capture.txt           # see capture.txt.example
python reseed.py capture.txt  # verify: shows the account, issue + expiry dates

# Option B: set MINIAPPS_JWT / MINIAPPS_CSRF_TOKEN / MINIAPPS_CSRF_COOKIE in .env

uvicorn app:app --host 0.0.0.0 --port 5000
# or: python app.py
```

Interactive docs: <http://localhost:5000/docs>

---

## Getting credentials

miniapps.ai authenticates browser sessions with a cookie/header trio:

| Value | Where it lives | Notes |
| --- | --- | --- |
| `jwt` | cookie | HS256 session token, ~15-day lifetime |
| `__Host-miniapps.x-csrf-token` | cookie | CSRF pair, cookie half (short) |
| `x-csrf-token` | request header | CSRF pair, header half (long) |
| `__cf_bm` | cookie | Cloudflare bot cookie, ~30 min, optional |

DevTools → Network → any `api.miniapps.ai` request → **Copy as cURL** (or *Copy as
Python requests*) → paste into `capture.txt`. `reseed.py` matches by name and by JWT
claims, so field order, quoting style, and extra analytics cookies are all fine.

> **The two CSRF values are a pair.** Mixing the cookie from one session with the
> header from another fails with a 403. Always copy both from the same capture.

> **`__cf_bm` is best left blank.** It expires in ~30 minutes; the proxy keeps
> whatever Cloudflare returns on the first call, which is fresher than anything you
> can paste.

---

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness + session expiry per user (**public**) |
| `GET` | `/v1/conversations/{id}` | Full conversation object |
| `GET` | `/v1/conversations/{id}/tool-settings` | Current toggles |
| `PUT` `PATCH` | `/v1/conversations/{id}/tool-settings` | Update toggles (`?merge=true` default) |
| `GET` | `/auth/status` | Account, JWT issue/expiry, time remaining |
| `POST` | `/auth/cookies` | Hot-swap individual credentials |
| `POST` | `/auth/reseed` | Reseed from a pasted capture |
| `GET` | `/auth/env` | Current credentials as `MINIAPPS_*` vars |
| `ANY` | `/v1/raw/{path}` | Passthrough to `api.miniapps.ai` |
| `GET` | `/mcp/sse` | MCP server (SSE transport) |

Every route except `/health` requires `Authorization: Bearer <key>` or `X-API-Key: <key>`.

### Examples

```bash
BASE=http://localhost:5000
KEY=sk-change-me
CID=634764a1-25c9-4143-94cb-0c7e5929f2e6

# Read current toggles
curl -s $BASE/v1/conversations/$CID/tool-settings -H "Authorization: Bearer $KEY"

# Flip two flags, leave the rest untouched
curl -s -X PATCH "$BASE/v1/conversations/$CID/tool-settings" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"webSearch": true, "codeInterpreter": true}'

# Reproduce the original script exactly (unlisted flags -> false)
curl -s -X PUT "$BASE/v1/conversations/$CID/tool-settings?merge=false" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"webSearch": true, "codeInterpreter": true}'

# How long is this session good for?
curl -s $BASE/auth/status -H "Authorization: Bearer $KEY"
```

---

## When the session expires

miniapps.ai exposes **no token refresh endpoint**, so the service cannot rotate the
`jwt` cookie for you. Instead it fails loudly with a `401` that says exactly what to
do, and gives you two ways to fix it without a redeploy:

```bash
# Push a fresh capture straight into the running service
python reseed.py capture.txt --push https://your-app.up.railway.app --key sk-change-me

# Or update individual values
curl -X POST $BASE/auth/cookies -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"jwt": "<new jwt>"}'
```

To make it permanent, write the values back to your env file and redeploy:

```bash
python reseed.py capture.txt --write-env .env
```

`GET /health` surfaces `jwt_expires_at` and `jwt_expired` per user, so a monitor can
warn you a few days ahead.

---

## Multi-user

Credentials are merged from four sources, later ones winning by `api_key`:

1. `users.json` (see `users.json.example`)
2. `MINIAPPS_USERS` — the same JSON array, inline
3. `capture.txt` / `MINIAPPS_CAPTURE` — a captured request
4. Legacy flat vars — `MINIAPPS_JWT`, `MINIAPPS_CSRF_TOKEN`, `MINIAPPS_CSRF_COOKIE`

Set `REQUIRE_API_KEY=false` for local single-user work; every request then resolves to
the first configured user.

---

## Using it from agents

**MCP over SSE** — point any MCP client at `https://your-app.up.railway.app/mcp/sse`.
Tools: `get_tool_settings`, `set_tool_settings`, `get_conversation`, `raw_request`,
`session_status`.

**MCP over stdio** — for editors that spawn a process:

```json
{
  "command": "python",
  "args": ["/path/to/mcp_stdio.py"],
  "env": { "REQUIRE_API_KEY": "false", "MINIAPPS_JWT": "...",
            "MINIAPPS_CSRF_TOKEN": "...", "MINIAPPS_CSRF_COOKIE": "..." }
}
```

MCP transports carry no API key, so MCP tools always act as the **first** configured
user. Keep the MCP endpoint private or run a single-user instance.

---

## Deploy to Railway

```bash
railway init
railway up
```

`railway.json` selects the Dockerfile builder, healthchecks `/health` with a 30s
timeout, and restarts on failure (max 10 retries). Set these variables in the Railway
dashboard — **not** in a committed file:

```
MINIAPPS_API_KEY, MINIAPPS_JWT, MINIAPPS_CSRF_TOKEN, MINIAPPS_CSRF_COOKIE
```

Railway injects `PORT`; the app reads it automatically. The same image runs anywhere
else that takes a Dockerfile (Fly, Render, Cloud Run).

---

## Security

- `.env`, `users.json`, and `capture.txt` are gitignored and excluded from the Docker
  image. Never commit a real capture — it is a full session token.
- A `jwt` cookie is bearer-equivalent: whoever holds it *is* the account until it
  expires. There is no server-side revocation here.
- Any credential pasted into a chat, issue, or screenshot should be considered burned.
  Sign out and back in on miniapps.ai to invalidate it, then reseed.
- This project automates *your own* account. Respect the miniapps.ai terms of service
  and keep the deployment private.

---

## Layout

```
app.py                 FastAPI app: auth, routes, MCP SSE mount
proxy.py               MiniAppsProxy: session state, headers/cookies, upstream calls
config.py              Settings + multi-user credential loading
reseed.py              Capture parser and CLI (--json / --write-env / --push)
mcp_tools.py           Shared MCP tool schemas + dispatch
mcp_stdio.py           stdio MCP entrypoint
capture.txt.example    Where to paste a DevTools capture
users.json.example     Multi-user template
Dockerfile             python:3.12-slim, CMD python app.py
railway.json           Dockerfile builder + /health healthcheck
```
