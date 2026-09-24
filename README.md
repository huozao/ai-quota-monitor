# AI Quota Monitor

Self-hosted Codex and Claude subscription-quota monitor. It reads the already
authenticated browser pages through a local CDP connection, stores raw page
text plus screenshots as evidence, and exposes a small FastAPI read API.

For code entry points, local tests, and contribution boundaries, see
[AGENTS.md](AGENTS.md). [CLAUDE.md](CLAUDE.md) is a compatibility entry to the same rules.

## Security boundary

Login is manual through the local noVNC endpoint. The monitor does not log in,
send account actions, or use provider API tokens. Keep the browser profile,
SQLite database, screenshots, logs, and notification tokens on the host; they
are intentionally excluded from Git.

## Watch position

Besides the two quota pages the monitor can watch one public X timeline
(`QUOTA_X_ACCOUNT`, empty disables it) where upcoming resets are announced early.
Posts are stored with the same retention as captures; only posts that match
`QUOTA_X_KEYWORDS` and are newer than `QUOTA_X_MAX_AGE_HOURS` raise a notification,
deduplicated by post id so a restart never re-sends one.

## Usage limit resets

For Codex (ChatGPT), the collector parses the "Usage limit resets" section in
addition to short-term (5h) and weekly usage limits. It captures:
- `resets_available`: number of available manual resets (highlighted in Feishu notifications when > 0).
- `resets_expires_at` / `resets_expires_at_iso`: expiration date and normalized UTC timestamp.
- `resets_type`: reset policy string (e.g. `Full reset (Weekly + 5 hr)`).

When new resets are granted or the expiration date changes, a `quota.limit_reset`
event is triggered and deduplicated in SQLite (`quota:limit_reset:<provider>:<count>:<expires_at>`).
In daily report cards, manual resets and credits are rendered as compact notation
footnotes below the metrics grid, keeping provider title headers (`֎ Codex`, `✴️ Claude`, `∩ AGY`)
clean, symmetrical, and uncluttered.

## Multi-account support

To monitor multiple Codex accounts within the same container, configure `QUOTA_CODEX_ACCOUNTS`
in your `.env` (using usernames or email addresses):

```bash
QUOTA_CODEX_ACCOUNTS="codex:9224:alice,codex_2:9225:bob@example.com"
```

Labels in report cards and alerts will display the username or email prefix in brackets
(e.g. `֎ Codex (alice)` and `֎ Codex (bob)`).

- **Session Isolation**: Each account runs in a separate Chrome window with an isolated
  profile directory (`/app/quota_browser_data` for port 9224, `/app/quota_browser_data/account_<port>`
  for additional ports) and a distinct CDP remote debugging port.
- **Manual Login**: In noVNC (`:6082`), windows are laid out side-by-side (`680x768`),
  allowing straightforward manual authentication for each account without session crosstalk.
- **Unified Reporting**: Captures are recorded under their respective provider keys, deduplicated
  independently, and aggregated into a single consolidated daily report card with distinct labels.
- **Health Verification**: `/healthz` monitors all configured CDP endpoints and reports individual
  port health in `browser_cdp_ports`.

## Retention

`QUOTA_RETENTION_DAYS` defaults to 7. After each capture, expired capture rows
and their screenshots are removed. The retention setting limits history data;
the browser profile is separate and is not pruned.

## Runtime health and recovery

The entrypoint removes stale Xvfb `:101` lock/socket files only when their
recorded PID is absent or a zombie, then waits for the newly started Xvfb
process and socket together. This protects the browser profile while allowing
the container to recover after an unclean stop. `/healthz` also checks the
dedicated Chrome CDP endpoint on `9224`; a dead browser returns HTTP `503`
instead of reporting a false healthy container.

The health check does not prove login, page parsing, notification delivery, or
Feishu receipt. Verify those separately with the capture database and
`notify_deliveries` in the private deployment runbook.

### Browser tab invariant

The collector enforces a strict single-tab invariant per target provider.
`_page_matches` matches both platform domains and settings routes (e.g. `codex`
and `chatgpt.com` for Codex; `claude.ai` for Claude; `x.com` for X).

During each collection cycle, if session restore or startup arguments produce
multiple matching tabs for a provider, the collector automatically reuses the
first matching page and closes all redundant duplicates (`await dup.close()`).
This was verified on webdock2 on 2026-09-24: multiple duplicate tabs on port 9225
were automatically cleaned up to a single active page without disrupting the user session.

### Notification constraints

The internal notification service enforces a strict schema limit of at most 3
items in `tags`. In multi-account setups, `quota_monitor/app.py` automatically
caps `tags` to the first 3 items (`tags[:3]`) to ensure daily report delivery
is never rejected with HTTP 422.

## Run locally

```bash
cp .env.example .env
docker compose up -d --build
```

Complete provider login in noVNC, then create `ATTACH_ENABLED` inside the data
directory. Put any reverse proxy, SSO, and notification routing in a private
deployment repository.

This repository contains code and deployment examples only. Never commit real
credentials, browser data, screenshots, SQLite files, or production config.
