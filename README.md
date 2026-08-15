# MiniApps Agent API

A REST + MCP wrapper around the private [miniapps.ai](https://miniapps.ai) conversation API.
It turns a copied browser request into a deployable service that can read and
change a conversation's tool settings, proxy any upstream endpoint, and expose the
same operations to agents as MCP tools.

Built from a single captured `PUT /conversations/{id}` request, generalised into
something you can host.

## Why a wrapper

miniapps.ai has no public API and no token refresh endpoint. Auth is a browser
session:

| Credential | Sent as | Notes |
| --- | --- | --- |
| `jwt` | cookie | HS256 session token, ~15 day lifetime |
| `__Host-miniapps.x-csrf-token` | cookie | short CSRF value |
| `x-csrf-token` | header | long CSRF value, **paired** with the cookie above |
| `__cf_bm` | cookie | Cloudflare bot cookie, ~30 min, optional |

So this server holds the session, keeps the CSRF pair together, absorbs whatever
`__cf_bm` Cloudflare last issued, reports how long the token has left, and gives
you a one-call path to swap in a fresh capture when it expires.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Public healthcheck + session expiry summary |
| `GET` | `/v1/conversations/{id}` | Fetch a conversation |
| `GET` | `/v1/conversations/{id}/tool-settings` | Read the six tool toggles |
| `PUT` `PATCH` | `/v1/conversations/{id}/tool-settings` | Change toggles (`?merge=true` by default) |
| `*` | `/v1/raw/{path}` | Pass anything through to `api.miniapps.ai` |
| `GET` | `/auth/status` | User, issued/expiry times, seconds remaining |
| `POST` | `/auth/cookies` | Hot-swap individual credentials |
| `POST` | `/auth/reseed` | Reseed from a pasted DevTools capture |
| `GET` | `/auth/env` | Current credentials as `MINIAPPS_*` lines |
| `GET` | `/mcp/sse` | MCP server (SSE transport) |

Interactive docs: `/docs`.

### The merge flag matters

The upstream `PUT` replaces the **whole** `toolSettings` object, so sending one
flag from the browser silently switches the other five off. This server reads
current state first and merges:

```bash
# turn on canvas, leave everything else as it is
curl -X PATCH "$BASE/v1/conversations/$CID/tool-settings" \
  -H "Authorization: Bearer $MINIAPPS_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"canvas": true}'

# reproduce the raw browser behaviour: unlisted flags go to false
curl -X PUT "$BASE/v1/conversations/$CID/tool-settings?merge=false" \
  -H "Authorization: Bearer $MINIAPPS_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"webSearch": true, "codeInterpreter": true}'
```

All six flags are always sent upstream: `webSearch`, `codeInterpreter`,
`canvas`, `flightSearch`, `locationSearch`, `conversationLookup`.

## Quick start

```bash
git clone https://github.com/BYOKopencode/miniapps.git
cd miniapps
pip install -r requirements.txt
cp .env.example .env      # then fill in the three required values
python app.py             # http://localhost:5000/docs
```

Don't want to copy values by hand? Save the copied request as `capture.txt` and
let the parser do it:

```bash
python reseed.py capture.txt --write-env .env
```

```text
jwt          eyJhbGciOiJI... (232 chars)
csrf_token   5beae7549a06... (128 chars)
csrf_cookie  037f93e07bd9... (64 chars)
cf_bm        MsqfN4AXBRdW... (139 chars)
session user you@example.com (b6017a15-...)
issued at    2026-08-15T07:57:15+00:00
expires at   2026-08-30T07:57:15+00:00
```

It reads cURL, `fetch`, and Python `requests` captures, and never confuses the
CSRF header with the `__Host-` cookie.

## Docker

```bash
docker build -t miniapps-api .
docker run -p 5000:5000 --env-file .env miniapps-api
```

## Deploy to Railway

`railway.json` is ready: Dockerfile builder, `/health` healthcheck, restart on
failure.

1. Push this repo to GitHub and create a Railway project from it.
2. Add the variables from `.env.example` (at minimum `MINIAPPS_API_KEY`,
   `MINIAPPS_JWT`, `MINIAPPS_CSRF_TOKEN`, `MINIAPPS_CSRF_COOKIE`).
3. Deploy. Railway injects `PORT` automatically.

Works the same on Render, Fly.io, or any Docker host.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MINIAPPS_API_KEY` | generated | Key clients must send to this API |
| `REQUIRE_API_KEY` | `true` | Set `false` for local/stdio use |
| `MINIAPPS_JWT` | - | `jwt` cookie (required) |
| `MINIAPPS_CSRF_TOKEN` | - | `x-csrf-token` header (required) |
| `MINIAPPS_CSRF_COOKIE` | - | `__Host-miniapps.x-csrf-token` cookie (required) |
| `MINIAPPS_CF_BM` | - | Optional Cloudflare cookie |
| `MINIAPPS_CAPTURE` / `MINIAPPS_CAPTURE_FILE` | - | Seed from a pasted request instead |
| `MINIAPPS_USERS` / `MINIAPPS_USERS_FILE` | - | Multi-session mode, one API key each |
| `MINIAPPS_API_BASE` | `https://api.miniapps.ai` | Upstream base |
| `MINIAPPS_FRONTEND_BASE` | `https://miniapps.ai` | Origin/referer sent upstream |
| `REQUEST_TIMEOUT` | `30` | Upstream timeout, seconds |
| `HOST` / `PORT` / `LOG_LEVEL` | `0.0.0.0` / `5000` / `info` | Server |
| `MCP_SERVER_NAME` / `MCP_SERVER_VERSION` | `miniapps` / `1.0.0` | MCP identity |

Multi-session mode: copy `users.json.example` to `users.json`, set
`MINIAPPS_USERS_FILE=users.json`, and each caller's API key selects its own
miniapps session.

## MCP

Five tools, shared by both transports: `get_tool_settings`, `set_tool_settings`,
`get_conversation`, `raw_request`, `session_status`.

Hosted (SSE):

```json
{
  "mcpServers": {
    "miniapps": { "url": "https://your-app.up.railway.app/mcp/sse" }
  }
}
```

Local (stdio):

```json
{
  "mcpServers": {
    "miniapps": {
      "command": "python",
      "args": ["mcp_stdio.py"],
      "env": {
        "MINIAPPS_JWT": "...",
        "MINIAPPS_CSRF_TOKEN": "...",
        "MINIAPPS_CSRF_COOKIE": "...",
        "REQUIRE_API_KEY": "false"
      }
    }
  }
}
```

MCP has no per-request API key, so MCP tools always act as the first configured
session.

## When the session expires

There is no refresh endpoint, so the fix is a fresh capture. Sign in again, copy
any authenticated `api.miniapps.ai` request, then either:

```bash
# push it into a running instance, no redeploy
python reseed.py capture.txt --push "$BASE" --key "$MINIAPPS_API_KEY"

# or POST it yourself
curl -X POST "$BASE/auth/reseed" \
  -H "Authorization: Bearer $MINIAPPS_API_KEY" \
  -H 'content-type: application/json' \
  -d "{\"raw\": $(jq -Rs . < capture.txt)}"
```

Before expiry, requests fail with `401` and a message pointing at
`/auth/reseed`. `GET /auth/env` then hands back the new `MINIAPPS_*` lines to
paste into your deploy config so a restart keeps working.

## Layout

```text
app.py         FastAPI routes, API-key auth, error mapping, MCP over SSE
proxy.py       Session state, cookie/header building, conversation calls
config.py      Env + users + capture loading, placeholder rejection
reseed.py      Capture parser, JWT decode, CLI (--json/--write-env/--push)
mcp_tools.py   Tool schemas + dispatch, shared by SSE and stdio
mcp_stdio.py   stdio MCP entrypoint
```

## Notes

- Unofficial and unaffiliated with miniapps.ai. Use your own account, and expect
  the private API to change without warning.
- Never commit `.env`, `users.json`, or `capture.txt` - all three are gitignored.
  A `jwt` cookie is a full account credential.
- API keys are compared with `secrets.compare_digest`, and startup fails if the
  example placeholder values are still in place.
