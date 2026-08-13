# Cloudflare tunnel: "Bad gateway" (error 502) — root cause and fix

## The bug

`app/main.py` started the named tunnel with only:

    cloudflared tunnel --no-autoupdate run --token <token>

With no `--url`, the local origin came entirely from the dashboard tunnel
config, which is:

    {"ingress":[{"hostname":"elcoraldb.alz.name.ng","service":"http://localhost:8000"}, ...]}

On Linux, `localhost` resolves to `::1` (IPv6 loopback) **first**, but the API
is launched with `--host 0.0.0.0`, which listens on **IPv4 only**. So
cloudflared dialled `[::1]:8000`, got connection-refused, and Cloudflare
returned its 502 "Bad gateway" page — while the API itself was perfectly
healthy on `127.0.0.1`. That mismatch is what made this look inexplicable.

Proven in the sandbox with the real token:

    curl http://127.0.0.1:8000/health   -> 200
    curl http://[::1]:8000/health       -> 000 (connection refused)
    https://elcoraldb.alz.name.ng/health -> 502   (tunnel connected, 0 requests proxied)

Note the quick-tunnel branch had the same `http://localhost:{port}` bug.

## The fix (app/main.py, scripts/tunnel.sh)

* Always pass `--url http://127.0.0.1:<port>` to cloudflared — for the named
  tunnel **and** the quick tunnel. This overrides the dashboard's
  `http://localhost:8000` service, pinning the origin to IPv4 loopback on the
  port this process actually serves. It also makes the tunnel immune to a stale
  port in the dashboard config (panel-assigned `SERVER_PORT` != 8000).
* The tunnel thread now waits for the local API socket to accept connections
  before connecting cloudflared, so the hostname can't 502 during the first
  seconds of boot.
* `scripts/tunnel.sh` got the same treatment, so both entrypoints behave
  identically.
* `PUBLIC_API_URL` now accepts a bare hostname (`elcoraldb.alz.name.ng`) as
  well as a full URL; `/health` reports it normalised with `https://`.
* New self-check: ~12s after boot the app fetches `PUBLIC_API_URL/health` and
  logs either `Public hostname verified: ... -> this process`, or, on
  502/503/504, the exact dashboard/DNS steps to fix — instead of failing
  silently like before.

Nothing else changed: routes, auth, JWT, SQLite/Postgres selection, LLM tiers,
model download and the quick-tunnel fallback all behave as before.

## Verified end to end over the real tunnel

Boot log after the fix:

    INFO myra: Starting named Cloudflare tunnel (stable hostname), origin http://127.0.0.1:8000
    INFO myra: Local API is accepting connections on http://127.0.0.1:8000
    INFO myra: Public hostname verified: https://elcoraldb.alz.name.ng -> this process

Live requests through `https://elcoraldb.alz.name.ng` (was 502 before the fix,
200 after; cloudflared metrics confirm the requests now reach the connector,
`cloudflared_tunnel_total_requests` climbing, `request_errors 0`):

    GET  /health                 200  {"status":"ok","database":"sqlite",...}
    GET  /                       200
    GET  /docs                   200  Swagger UI renders in a real Chromium browser
    GET  /model                  200  {"backend":"mock","loaded":true,...}
    POST /auth/register          201  token + user
    POST /auth/login             200  token
    GET  /auth/me                200  user
    POST /sessions               201  session id
    POST /sessions/{id}/chat     200  assistant reply

Tested with `MYRA_LLM_BACKEND=mock` so no multi-GB GGUF download was needed;
the HTTP/tunnel path is identical for the real `llama_cpp` backend.

## If you ever see 502 again

1. Confirm the API answers locally: `curl http://127.0.0.1:$PORT/health`.
2. Confirm the boot log prints `Public hostname verified`.
3. If not, check the tunnel's Public Hostname in Zero Trust → Networks →
   Tunnels points at `http://127.0.0.1:<your port>`, and that
   `elcoraldb.alz.name.ng` is the CNAME to
   `<tunnel-id>.cfargotunnel.com` that Cloudflare creates for you (a plain
   proxied A record on that hostname cannot route to a tunnel).
4. Never use `localhost` as the tunnel origin — always `127.0.0.1`.
