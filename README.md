# AI Quota Monitor

Self-hosted Codex and Claude subscription-quota monitor. It reads the already
authenticated browser pages through a local CDP connection, stores raw page
text plus screenshots as evidence, and exposes a small FastAPI read API.

## Security boundary

Login is manual through the local noVNC endpoint. The monitor does not log in,
send account actions, or use provider API tokens. Keep the browser profile,
SQLite database, screenshots, logs, and notification tokens on the host; they
are intentionally excluded from Git.

## Retention

`QUOTA_RETENTION_DAYS` defaults to 7. After each capture, expired capture rows
and their screenshots are removed. The retention setting limits history data;
the browser profile is separate and is not pruned.

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
