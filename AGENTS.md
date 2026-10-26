# AI Quota Monitor

Public, self-hosted quota collector for already authenticated Codex and Claude
pages, with local evidence storage and a FastAPI read API. This file also applies
to standalone clones; a private parent workspace is not required for local work.

## Entry points

- [README.md](README.md): purpose, security boundary, retention, and local setup.
- [docs/deployment-webdock2.md](docs/deployment-webdock2.md): webdock2 host topology, multi-account config, and recovery runbook.
- `quota_monitor/core.py`: reset parsing, normalization, event deduplication, and watch filters.
- `quota_monitor/translate.py`: watch post Chinese translation via Google Translate with cache and proxy.
- `quota_monitor/chart.py`: weekly remaining limit trend chart generation (Pillow) with reset jump detection.
- `quota_monitor/app.py`: API, collection lifecycle, capture evidence, storage, and retention.
- `tests/`: parser, capture, retention, watch, and translation regression tests.
- `docker-compose.yml`, `Dockerfile`, `docker/quota-entrypoint.sh`: generic runtime examples.

## Boundaries

- Login remains manual. Do not perform account actions, change authentication,
  trigger real collection/notifications, or start a deployment as part of a local test.
  Such actions require authorization within the user's task.
- Never commit credentials, actual browser profiles, captures, screenshots,
  databases, logs, production configuration, or private host/network details.
  Keep deployment-specific routing and authentication in the operator's private repository.
- Preserve existing capture evidence and retention contracts. Consume normalized
  reset timestamps; do not guess absolute times from display strings downstream.
- Do not commit, push, merge, create PRs, publish images, deploy, or modify remote
  resources without explicit authorization. Preserve unrelated worktree changes.

## Core delivery principle & deployment SSOT

- **GitHub as the sole source of truth (SSOT)**:
  Any modification, parser enhancement, or bugfix must be locally verified via the test suite (`pytest`, `compileall`, `git diff --check`), then committed and pushed to GitHub. Runtime logic must always be executed directly from verified GitHub source code. Live hot-patching or manual file modifications on deployment hosts are strictly prohibited.
- **Containerized deployment (GitOps)**:
  Production environments must strictly run immutable container images built from GitHub source (e.g. `ghcr.io/huozao/ai-quota-monitor:sha-<commit>`). Configuration changes and image version pins are managed exclusively via GitOps (e.g. SOPS-encrypted environments in `infra` repo deployed via `render.sh`), ensuring zero host drift and reliable disaster recovery.
- **The web console is not in this repo**: the dashboard page (`console-quota.html`)
  and the image pin both live in the operator's `infra` repo. A change that is visible
  in the web page is delivered only when it is merged into infra **main**. Changes that
  sit on a branch get reverted by the next apply from main. When the web page and Feishu
  disagree, compare the live page with infra main before suspecting this API.
  See Case 8 in [docs/deployment-webdock2.md](docs/deployment-webdock2.md).

## Local verification

Use a Python environment with `requirements.txt` and `pytest` installed:

```bash
python -m pytest -q
python -m compileall -q quota_monitor
git diff --check
```

Start with the affected tests. For documentation-only changes, check paths, links,
and commands without launching the collector or a browser.

When behavior, API fields, evidence, or verification changes, update the affected
existing documentation and navigation in the same change. Add only the documentation
a new feature needs; do not create records for unaffected areas. Date historical
observations and label unverified outcomes; local tests do not prove live capture
or notification delivery.
