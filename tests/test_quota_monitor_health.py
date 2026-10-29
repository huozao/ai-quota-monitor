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


def test_healthz_checks_all_cdp_ports(monkeypatch):
    monkeypatch.setenv("QUOTA_CODEX_ACCOUNTS", "codex:9224:alice,codex_sub:9225:bob")
    port_status_map = {9224: True, 9225: True}
    monkeypatch.setattr(quota_app, "_cdp_ready", lambda port=9224: port_status_map.get(port, False))

    result = quota_app.healthz()
    assert result["ok"] is True
    assert result["browser_cdp_ports"] == {"9224": True, "9225": True}

    # 某个端口异常时整体判定为 503
    port_status_map[9225] = False
    with pytest.raises(HTTPException) as caught:
        quota_app.healthz()
    assert caught.value.status_code == 503
    assert caught.value.detail["browser_cdp_ports"] == {"9224": True, "9225": False}


def test_healthz_detects_dead_collector_task(monkeypatch, tmp_path):
    monkeypatch.setattr(quota_app, "_cdp_ready", lambda port=9224: True)
    enable_file = tmp_path / "ATTACH_ENABLED"
    enable_file.touch()
    monkeypatch.setattr(quota_app, "ENABLE_FILE", enable_file)

    class _DeadTask:
        def done(self) -> bool:
            return True

        def cancelled(self) -> bool:
            return False

        def exception(self) -> Exception:
            return RuntimeError("driver connection lost")

    monkeypatch.setattr(quota_app, "_task", _DeadTask())

    with pytest.raises(HTTPException) as caught:
        quota_app.healthz()
    assert caught.value.status_code == 503
    assert caught.value.detail["ok"] is False
    assert caught.value.detail["task_alive"] is False
    assert "RuntimeError: driver connection lost" in caught.value.detail["task_error"]


def test_healthz_detects_stalled_collector(monkeypatch, tmp_path):
    import time
    monkeypatch.setattr(quota_app, "_cdp_ready", lambda port=9224: True)
    enable_file = tmp_path / "ATTACH_ENABLED"
    enable_file.touch()
    monkeypatch.setattr(quota_app, "ENABLE_FILE", enable_file)

    class _RunningTask:
        def done(self) -> bool:
            return False

    monkeypatch.setattr(quota_app, "_task", _RunningTask())
    # 模拟超过 POLL_MAX + 10 分钟无心跳
    monkeypatch.setattr(quota_app, "_last_cycle_progress", time.time() - 3600)

    with pytest.raises(HTTPException) as caught:
        quota_app.healthz()
    assert caught.value.status_code == 503
    assert caught.value.detail["ok"] is False
    assert caught.value.detail["task_stalled"] is True
