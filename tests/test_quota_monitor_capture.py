"""截图失败不得改写解析状态（2026-09-08 生产故障的回归）。"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from quota_monitor import app as quota_app


CODEX_TEXT = (
    "5 hour usage limit\n\n0%\nremaining\nResets 4:08 PM\n\n"
    "Weekly usage limit\n\n67%\nremaining\nResets Sep 15, 2026 5:18 AM\n\n"
    "Credits remaining\n\n0\n"
)


class _Locator:
    def __init__(self, text: str) -> None:
        self._text = text

    async def inner_text(self, timeout: int = 0) -> str:
        return self._text


class _Mouse:
    async def move(self, x: int, y: int) -> None:
        return None


class FakePage:
    """只实现采集用到的那几个调用；``shots`` 记录每次截图的成败。"""

    url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

    def __init__(self, text: str, *, screenshot_fails: int) -> None:
        self._text = text
        self._screenshot_fails = screenshot_fails
        self.shots = 0
        self.repaints = 0
        self.mouse = _Mouse()

    async def reload(self, **kwargs: object) -> None:
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    def locator(self, selector: str) -> _Locator:
        return _Locator(self._text)

    async def bring_to_front(self) -> None:
        self.repaints += 1

    async def evaluate(self, script: str) -> None:
        return None

    async def screenshot(self, path: str, **kwargs: object) -> None:
        self.shots += 1
        if self.shots <= self._screenshot_fails:
            raise TimeoutError("Page.screenshot: Timeout 15000ms exceeded.")
        Path(path).write_bytes(b"png")


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    monkeypatch.setattr(quota_app, "DATA_DIR", directory)
    monkeypatch.setattr(quota_app, "SCREENSHOT_DIR", directory / "screenshots")
    monkeypatch.setattr(quota_app, "DB_PATH", directory / "quota.sqlite3")
    return directory


def _row(conn):
    return conn.execute(
        "SELECT status, confidence, fields_json, screenshot_path, error FROM captures ORDER BY id DESC LIMIT 1"
    ).fetchone()


def test_screenshot_timeout_keeps_parsed_status_healthy(data_dir):
    page = FakePage(CODEX_TEXT, screenshot_fails=2)
    result = asyncio.run(quota_app._capture(page, "codex"))

    assert result["status"] == "healthy"
    assert result["screenshot_path"] is None
    assert page.shots == 2 and page.repaints == 2  # 重试前每次都先强制产帧

    conn = quota_app._db()
    row = _row(conn)
    conn.close()
    assert row["status"] == "healthy"
    assert row["screenshot_path"] is None  # 不留指向缺失文件的路径，看板不会出碎图
    assert "screenshot: TimeoutError" in row["error"]
    assert json.loads(row["fields_json"])["weekly_remaining"] == "67%"


def test_screenshot_retry_succeeds_after_forced_repaint(data_dir):
    page = FakePage(CODEX_TEXT, screenshot_fails=1)
    result = asyncio.run(quota_app._capture(page, "codex"))

    assert result["status"] == "healthy"
    assert result["screenshot_path"] is not None and result["screenshot_path"].is_file()

    conn = quota_app._db()
    row = _row(conn)
    conn.close()
    assert row["error"] == ""


def test_x_screenshot_height_stops_after_last_rendered_post():
    assert quota_app._x_screenshot_height(900, 2240) == 2280


def test_x_screenshot_height_caps_pathological_document():
    assert quota_app._x_screenshot_height(900, 7000) == quota_app.X_SCREENSHOT_MAX_HEIGHT_PX


def test_x_screenshot_clip_uses_rendered_post_boundary(tmp_path, monkeypatch):
    class FakeXPage:
        async def evaluate(self, script: str):
            return {"viewport_width": 1350, "viewport_height": 900, "article_bottom": 2240}

    monkeypatch.setattr(quota_app, "SCREENSHOT_DIR", tmp_path)
    clip = asyncio.run(quota_app._x_screenshot_clip(FakeXPage()))

    assert clip == {"x": 0, "y": 0, "width": 1350, "height": 2280}


def test_x_screenshot_uses_emulated_viewport_and_writes_png(tmp_path, monkeypatch):
    class _Mouse:
        async def move(self, x: int, y: int) -> None:
            return None

    class FakeCdpSession:
        def __init__(self):
            self.calls = []
            self.detached = False

        async def send(self, method: str, params: dict[str, object]):
            self.calls.append((method, params))
            if method == "Page.captureScreenshot":
                return {"data": base64.b64encode(b"png-from-cdp").decode("ascii")}
            return {}

        async def detach(self):
            self.detached = True

    class FakeContext:
        def __init__(self, session):
            self.session = session

        async def new_cdp_session(self, target):
            return self.session

    class FakeXPage:
        mouse = _Mouse()

        def __init__(self, session):
            self.context = FakeContext(session)

        async def bring_to_front(self) -> None:
            return None

        async def evaluate(self, script: str):
            if "querySelectorAll" in script:
                return {"viewport_width": 1350, "viewport_height": 900, "article_bottom": 2240}
            return None

        async def wait_for_timeout(self, ms: int) -> None:
            return None

    monkeypatch.setattr(quota_app, "SCREENSHOT_DIR", tmp_path)
    session = FakeCdpSession()
    page = FakeXPage(session)
    result = asyncio.run(quota_app._screenshot(page, quota_app.X_PROVIDER))

    assert result[0] is not None
    assert result[0].read_bytes() == b"png-from-cdp"
    assert session.calls == [
        (
            "Emulation.setDeviceMetricsOverride",
            {"width": 1350, "height": 2280, "deviceScaleFactor": 1, "mobile": False},
        ),
        (
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "clip": {"x": 0, "y": 0, "width": 1350, "height": 2280, "scale": 1},
            },
        ),
        ("Emulation.clearDeviceMetricsOverride", {}),
    ]
    assert session.detached is True


def test_page_failure_still_marks_network_error(data_dir):
    page = FakePage(CODEX_TEXT, screenshot_fails=0)

    async def boom(**kwargs: object) -> None:
        raise RuntimeError("net::ERR_TIMED_OUT")

    page.reload = boom  # type: ignore[assignment]
    result = asyncio.run(quota_app._capture(page, "codex"))

    assert result["status"] == "network_error"
    assert "RuntimeError" in _row_error()


def _row_error() -> str:
    conn = quota_app._db()
    row = _row(conn)
    conn.close()
    return row["error"]


CODEX_WITH_LIMIT_RESETS = (
    "5 hour usage limit\n\n100%\nremaining\n\n"
    "Weekly usage limit\n\n0%\nremaining\nResets Sep 28, 2026 2:24 AM\n\n"
    "Credits remaining\n\n441\n"
    "Usage limit resets\n"
    "Use a reset to restore your 5-hour limit, weekly limit, or both.\n"
    "Available 1\nHistory\n"
    "Full reset (Weekly + 5 hr)\n"
    "Expires Oct 22, 6:31 PM\n"
    "Use reset\n"
)


def test_codex_capture_parses_limit_resets_and_notifies(data_dir, monkeypatch):
    notifications = []

    async def fake_notify(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(quota_app, "_notify", fake_notify)

    # 初始有一条无额度的前序记录
    page_zero = FakePage(CODEX_TEXT, screenshot_fails=0)
    res_zero = asyncio.run(quota_app._capture(page_zero, "codex"))
    assert res_zero["status"] == "healthy"

    # 新采集解析出 1 次重置及到期时间，并触发通知
    page_reset = FakePage(CODEX_WITH_LIMIT_RESETS, screenshot_fails=0)
    res_reset = asyncio.run(quota_app._capture(page_reset, "codex"))

    assert res_reset["status"] == "healthy"
    assert res_reset["limit_reset_detected"] is True
    assert res_reset["fields"]["resets_available"] == 1
    assert res_reset["fields"]["resets_expires_at"] == "Oct 22, 6:31 PM"
    assert res_reset["fields"]["resets_expires_at_iso"] is not None
    assert res_reset["fields"]["resets_type"] == "Full reset (Weekly + 5 hr)"

    # 验证发出了一次 quota.limit_reset 通知
    limit_notifs = [n for n in notifications if n[0][0] == "quota.limit_reset"]
    assert len(limit_notifs) == 1
    assert "重置额度已到账" in limit_notifs[0][1]["segments"][0]["text"]

    # 再次采集相同内容（额度和到期时间不变），不再重复触发
    notifications.clear()
    res_again = asyncio.run(quota_app._capture(page_reset, "codex"))
    assert res_again["limit_reset_detected"] is False
    assert len(notifications) == 0


def test_capture_multi_account_codex_sub(data_dir, monkeypatch):
    monkeypatch.setenv("QUOTA_CODEX_ACCOUNTS", "codex:9224:alice,codex_sub:9225:bob@example.com")
    notifications = []

    async def fake_notify(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(quota_app, "_notify", fake_notify)

    # 采集第一个账号 (alice)
    page_main = FakePage(CODEX_TEXT, screenshot_fails=0)
    res_main = asyncio.run(quota_app._capture(page_main, "codex"))
    assert res_main["status"] == "healthy"
    assert res_main["provider"] == "codex"

    # 采集第二个账号 (bob@example.com -> bob)（初始前序记录）
    page_sub_zero = FakePage(CODEX_TEXT, screenshot_fails=0)
    res_sub_zero = asyncio.run(quota_app._capture(page_sub_zero, "codex_sub"))
    assert res_sub_zero["status"] == "healthy"

    # 账号 bob 获得新重置额度
    page_sub = FakePage(CODEX_WITH_LIMIT_RESETS, screenshot_fails=0)
    res_sub = asyncio.run(quota_app._capture(page_sub, "codex_sub"))
    assert res_sub["status"] == "healthy"
    assert res_sub["provider"] == "codex_sub"
    assert res_sub["limit_reset_detected"] is True
    assert res_sub["fields"]["resets_available"] == 1
    assert res_sub["fields"]["resets_expires_at"] == "Oct 22, 6:31 PM"

    # 验证通知标题带有其专属名称 Codex (bob)
    limit_notifs = [n for n in notifications if n[0][0] == "quota.limit_reset"]
    assert len(limit_notifs) == 1
    assert "Codex (bob) 获得新重置额度" in limit_notifs[0][0][1]

    # 验证 latest API 能同时返回两个独立账号且带用户名标签
    latest_data = quota_app.latest()
    assert "codex" in latest_data["providers"]
    assert "codex_sub" in latest_data["providers"]
    assert latest_data["providers"]["codex"]["label"] == "Codex (alice)"
    assert latest_data["providers"]["codex_sub"]["label"] == "Codex (bob)"
    assert latest_data["providers"]["codex_sub"]["resets_available"] == 1
