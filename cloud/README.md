# Cloud relay (Cloudflare Workers, free plan)

Replaces the Windows port-forward: the local bridge pushes state *out* to the Worker and collects
decisions from it, and the Flipper polls the Worker over the internet. No inbound ports anywhere.

```
hooks -> bridge (WSL) --PUT /state--> Worker (Durable Object) <--GET /state?wait=1500-- Flipper
                       <--GET /decision?take=1--            <--GET /decision?answer=allow--
```

## Deploy (once)
```bash
cd cloud
wrangler login                      # opens a browser
wrangler deploy                     # prints https://cc-flipper-relay.<you>.workers.dev
openssl rand -hex 16                # make a token
echo "<token>" | wrangler secret put CC_TOKEN
```

## Point both ends at it
- Bridge: put `CC_CLOUD_URL=https://cc-flipper-relay.<you>.workers.dev` and `CC_CLOUD_TOKEN=<token>` in `~/.claude/cc-monitor/env` (read by `bridge/start.sh`), then restart the bridge
- Flipper: `apps_data/cc_monitor/bridge.txt` line 1 = the Worker URL, line 2 = the token.

## Budget
Free plan = 100k Worker requests/day and 100k Durable Object requests/day. The app long-polls (1.5 s hold), so an app
left open all day uses ~55k. The bridge only talks to the Worker on state changes and once per second while a decision is pending.

## Privacy
The tool name and a trimmed command line (60 chars) are stored in memory on the Durable Object while the session runs.
Anyone with the token can read them or answer a pending ask, so keep the token private and rotate it with `wrangler secret put`.

## Gotchas
- Cloudflare answers 403 to the default `Python-urllib` user agent; the bridge sends `User-Agent: cc-bridge/1.0`. The ESP32 client's UA is accepted.
- `compatibility_date` is pinned to a date the local `wrangler dev` runtime accepts; bump it when you upgrade wrangler.
- If `wrangler login` complains about an API token, run `env -u CLOUDFLARE_API_TOKEN wrangler login`.
