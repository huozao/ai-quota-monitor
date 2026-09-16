# AI Quota Monitor

Public, self-hosted quota collector for already authenticated Codex and Claude
pages, with local evidence storage and a FastAPI read API. This file also applies
to standalone clones; a private parent workspace is not required for local work.

## Entry points

- [README.md](README.md): purpose, security boundary, retention, and local setup.
- `quota_monitor/core.py`: reset parsing, normalization, event deduplication, and watch filters.
- `quota_monitor/app.py`: API, collection lifecycle, capture evidence, storage, and retention.
- `tests/`: parser, capture, retention, and watch regression tests.
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
