# AI Quota Monitor

Self-hosted Codex and Claude subscription-quota monitor. It reads the already
authenticated browser pages through a local CDP connection, stores raw page
text plus screenshots as evidence, and exposes a small FastAPI read API.

For code entry points, local tests, and contribution boundaries, see
[AGENTS.md](AGENTS.md). [CLAUDE.md](CLAUDE.md) is a compatibility entry to the same rules.
For production host deployment topology, multi-account setup, and recovery runbooks, see
[docs/deployment-webdock2.md](docs/deployment-webdock2.md).

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

### Translation for watch posts

Posts captured from the watch timeline are automatically translated to Chinese using
`quota_monitor/translate.py` (via `translate.googleapis.com` through `CHROME_PROXY_SERVER`,
with an in-memory LRU cache of 256 items and an 8-second timeout).
In both Feishu notification cards and the web console (`https://hydwang.xyz/console/quota/`),
posts display the original English text as the primary content, followed by the Chinese translation
in subtle light grey small text (`<font color='grey'>` in Feishu markdown, `color: var(--muted); font-size: .82rem` in HTML).


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

## Weekly limit trends and notification cards

The monitor tracks remaining quota progression over a 7-day rolling window:
- **Descending Remaining Curve**: Displays weekly limit progression declining from 100% downward, with reset jump markers (`♻ 周重置`) when quotas are replenished back to 100%.
- **Render Engine**: Generated via Pillow (`quota_monitor/chart.py`) using 2x Retina supersampling downsampled with Lanczos filtering, dark theme (`#0b1220`), and CJK font support.
- **Endpoints**: Accessible at `/v1/quota/providers/{provider}/trend.png` and `/console/quota/api/providers/{provider}/trend.png`.
- **Card ViewModel**: `build_quota_card` embeds `trend_url` alongside `screenshot_url`, ensuring the web console and notification channels share a unified model. Redundant "额度充足" labels are omitted when 5h quota is at 100%.
- **Dual Collapsible Panels**: In Feishu daily report cards, visual evidence is organized into two collapsible panels (`collapsible_panel`, default collapsed):
  1. `📈 点击展开周限额趋势图 (N 张)`
  2. `🖥️ 点击展开页面现场截图 (N 张)`
  This keeps message cards compact while allowing one-click expansion and native high-resolution image preview.
- **Consistent Ordering**: Accounts are strictly ordered as Codex primary (`codex`) → Codex secondary (`codex_2`) → Claude (`claude`) → X watch (`x-thsottiaux`) across report text, trend charts, screenshots, and web console tabs.

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

### Memory footprint and process hygiene

Automated scraping of complex single-page applications (SPAs) over prolonged periods
can lead to significant renderer memory bloat and zombie process buildup:

1. **Chrome Resource Constraints**: Browser instances are launched with `--mute-audio`,
   `--disable-audio-output`, `--js-flags=--max-old-space-size=512`,
   `--disable-features=OmniboxPopupAimWebUI,OptimizationHints,MediaRouter,Translate`, and
   `--renderer-process-limit=2`. This disables unneeded background services (e.g. audio mojom,
   internal omnibox WebUIs) and sets a firm V8 heap ceiling.
2. **Active Post-Capture Memory Purge**: After each collection cycle, `_purge_page_memory` sends
   `HeapProfiler.collectGarbage` and `Memory.forciblyPurgeJavaScriptMemory` via CDP to flush
   fragmented heap memory and unused layout resources from long-lived SPA tabs.
3. **Child Subreaper Hygiene**: `app.py` registers the main process as a child subreaper via
   `prctl(PR_SET_CHILD_SUBREAPER)` and handles `SIGCHLD` to automatically reap any orphaned child
   processes (such as Chrome `cat` wrappers) without leaving defunct zombies.
4. **Lightweight Page Hibernation (`QUOTA_PAGE_HIBERNATE`)**: Enabled by default (`true`).
   After each successful capture, tabs navigate to `about:blank#quota-target=<id>` and display
   a minimal dark placeholder card before flushing GC. This unloads heavy React SPAs and background
   WebSockets, shrinking renderer RSS from ~200MB down to ~20MB while preserving cookies, session
   storage, and TLS fingerprints to avoid Cloudflare Turnstile blocks on subsequent wakes.
   If authentication fails (`auth_required` / `blocked`), the page remains intact on the login screen
   to allow manual re-authentication via noVNC.

### Host Topology & Integration

- **Collector & API Host (`webdock2`)**:
  - Runs the `quota-monitor` Docker container (internal port 8001 -> host port 18002).
  - Manages isolated Chrome instances per account (CDP 9224 for primary `codex`, 9225 for `codex_2`).
  - Renders weekly quota remaining trend charts on the fly via Pillow (`quota_monitor/chart.py`) and serves `/v1/quota/providers/{provider}/trend.png`.
  - Dispatches scheduled and on-demand daily reports to the notification hub (`NOTIFY_ENDPOINT`, e.g. `http://host.docker.internal:18020/v1/internal/notify/send`).
- **Edge & Notification Hub (`txecs`)**:
  - Serves the web console at `https://hydwang.xyz/console/quota/` (managed by `infra/roles/server/tencent/files/console-quota.html`), proxying `/console/quota/api/*` to `webdock2:18002` via SSH tunnel.
  - Runs the notify center backend (`business-cn-backend-api-1`), handling Feishu image uploads and rendering the Card 2.0 dual collapsible panels for `quota.daily_report` events.

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
