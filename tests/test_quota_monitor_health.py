"""Runtime health checks must detect a dead quota Chrome, not only a live API."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from quota_monitor import app as quota_app


def test_healthz_reports_cdp_ready(monkeypatch):
    monkeypatch.setattr(quota_app, "_cdp_ready", lambda: True)

    result = quota_app.healthz()

    assert result["ok"] is True
    assert result["browser_cdp"] is True
    assert "db" in result


def test_healthz_fails_when_cdp_is_unavailable(monkeypatch):
    monkeypatch.setattr(quota_app, "_cdp_ready", lambda: False)

    with pytest.raises(HTTPException) as caught:
        quota_app.healthz()

    assert caught.value.status_code == 503
    assert caught.value.detail["ok"] is False
    assert caught.value.detail["browser_cdp"] is False
