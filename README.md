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

With the default configuration, the dedicated Chrome should have three page
tabs: Codex, Claude, and the optional X watch page. The collector reuses the
first page whose URL matches a provider and creates a page only when no match
exists; it does not close additional matching pages. The entrypoint currently
passes Codex and Claude URLs on every Chrome start, while the browser profile
is persistent. After a browser/container restart, session restore plus those
startup URLs can therefore leave a duplicate Codex tab. This was confirmed on
webdock2 on 2026-09-19: CDP showed two identical Codex pages, while the
capture database still had only one Codex capture per cycle. One duplicate was
closed through the local CDP endpoint; no container restart or profile/data
deletion was performed.

Prevention is not yet deployed. A future change must choose one owner for
startup page creation (entrypoint or collector), and add an explicit duplicate
policy before changing the persistent profile. Until then, verify page count
through CDP after a browser/container restart; do not delete the browser
profile as a cleanup shortcut.

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
