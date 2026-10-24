# garmin-mcp

A personal MCP server for Garmin Connect data. It pulls your activities and daily
health summaries into a local SQLite database on a schedule, and serves them to
Claude over MCP (streamable HTTP) at `http://<server>:8000/mcp`.

Data comes from the Garmin Connect Personal API (`app/sources.py`, `GarminApiSource`):
activity search, v2 daily summaries (steps, resting HR, calories, stress, Body Battery),
sleep summaries and HRV. The personal API key is exchanged for a 24-hour access token,
renewed automatically. `GARMIN_SOURCE=fake` runs on sample data instead; switching to
`garmin` removes the sample rows on startup and pulls `INITIAL_SYNC_DAYS` of history.

Times and days are local, taken from the server's `/etc/localtime`.

## Run on the server

```bash
cp .env.example .env        # then set MCP_TOKEN
docker compose up -d --build
curl http://localhost:8000/health
```

Data lives in the `garmin-data` Docker volume and survives rebuilds.

## Connect Claude Code

```bash
claude mcp add --transport http --scope user garmin http://192.168.0.71:8000/mcp --header "Authorization: Bearer <MCP_TOKEN>"
```

## Tools

| Tool | What it returns |
| --- | --- |
| `list_activities` | Activities in a date range, newest first (default last 30 days) |
| `get_activity` | Everything stored for one activity |
| `get_health` | Daily summaries: daily, sleep, hrv, body_battery, stress (default last 7 days) |
| `training_totals` | Count, hours, km and calories per activity type |
| `sync_status` | Last sync time and result, and how much is stored |
| `sync_now` | Pulls the last N days from Garmin immediately |

`GET /health` is open (no token) for Uptime Kuma. `/dashboard` uses a browser password
(`DASHBOARD_PASSWORD`, any username). Everything else needs the bearer token.

## Dashboard

`http://192.168.0.71:8000/dashboard` at home, or `http://100.111.96.85:8000/dashboard`
over Tailscale. It shows summary tiles (vs the previous period), training time by sport,
sleep, HRV, resting HR, steps and recent activities for 7, 30 or 90 days, and refreshes
every 15 minutes. Every chart has a table view.

## Tests

```bash
docker exec garmin-mcp python tests/smoke_test.py      # MCP tools + token check
docker exec garmin-mcp python tests/dashboard_test.py  # dashboard password + data
docker exec garmin-mcp python -m tests.garmin_client_test  # Garmin client vs a simulated API
```

## Settings (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_TOKEN` | required | Bearer token, at least 24 characters |
| `DASHBOARD_PASSWORD` | empty | Browser password for `/dashboard`; empty locks it |
| `GARMIN_SOURCE` | `fake` | `fake` sample data, or `garmin` for your real data |
| `GARMIN_API_KEY` | empty | Personal API key from Garmin Connect Web (needed for `garmin`) |
| `SYNC_INTERVAL_MIN` | `60` | Minutes between syncs |
| `INITIAL_SYNC_DAYS` | `180` | Days pulled on the first sync into an empty database |
| `LOOKBACK_DAYS` | `3` | Days re-pulled on each later sync, to catch late uploads |
