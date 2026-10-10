"""截图失败不得改写解析状态（2026-09-08 生产故障的回归）。"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
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


def test_page_matches_and_find_page():
    class DummyPage:
        def __init__(self, url):
            self.url = url

    p_chatgpt = DummyPage("https://chatgpt.com/")
    p_codex = DummyPage("https://chatgpt.com/codex/cloud/settings/usage")
    p_claude = DummyPage("https://claude.ai/settings/usage")
    p_x = DummyPage("https://x.com/thsottiaux")

    # Codex target matches both chatgpt.com and codex URL
    assert quota_app._page_matches(p_chatgpt, {"id": "codex", "kind": "codex"}) is True
    assert quota_app._page_matches(p_codex, {"id": "codex", "kind": "codex"}) is True
    assert quota_app._page_matches(p_claude, {"id": "codex", "kind": "codex"}) is False

    # Claude target
    assert quota_app._page_matches(p_claude, {"id": "claude", "kind": "claude"}) is True
    assert quota_app._page_matches(p_chatgpt, {"id": "claude", "kind": "claude"}) is False

    # X target
    assert quota_app._page_matches(p_x, {"id": "x-thsottiaux", "kind": "x"}) is True

    # Find page
    pages = [p_claude, p_chatgpt, p_x]
    assert quota_app._find_page(pages, {"id": "codex", "kind": "codex"}) is p_chatgpt
    assert quota_app._find_page(pages, {"id": "claude", "kind": "claude"}) is p_claude
    assert quota_app._find_page(pages, {"id": "x-thsottiaux", "kind": "x"}) is p_x

    # Hibernated pages match by target id anchor in about:blank
    p_hib_codex = DummyPage("about:blank#quota-target=codex")
    p_hib_codex_2 = DummyPage("about:blank#quota-target=codex_2")
    p_hib_claude = DummyPage("about:blank#quota-target=claude")

    assert quota_app._page_matches(p_hib_codex, {"id": "codex", "kind": "codex"}) is True
    assert quota_app._page_matches(p_hib_codex, {"id": "codex_2", "kind": "codex"}) is False
    assert quota_app._page_matches(p_hib_codex_2, {"id": "codex_2", "kind": "codex"}) is True
    assert quota_app._page_matches(p_hib_claude, {"id": "claude", "kind": "claude"}) is True
    assert quota_app._page_matches(p_hib_claude, {"id": "codex", "kind": "codex"}) is False

    hib_pages = [p_hib_claude, p_hib_codex, p_hib_codex_2]
    assert quota_app._find_page(hib_pages, {"id": "codex", "kind": "codex"}) is p_hib_codex
    assert quota_app._find_page(hib_pages, {"id": "codex_2", "kind": "codex"}) is p_hib_codex_2
    assert quota_app._find_page(hib_pages, {"id": "claude", "kind": "claude"}) is p_hib_claude


def test_build_quota_card_and_history_card_model(data_dir):
    # 构建测试用的额度 item
    item = {
        "provider": "codex",
        "status": "healthy",
        "captured_at": "2026-09-27T10:00:00+08:00",
        "confidence": 0.95,
        "screenshot_url": "/v1/quota/captures/1/screenshot",
        "fields": {
            "remaining": "100%",
            "weekly_remaining": "0%",
            "weekly_reset_at": "Sep 28, 2026 2:24 AM",
            "weekly_reset_at_iso": "2026-09-28T02:24:00+08:00",
            "resets_available": 1,
            "resets_expires_at": "Oct 22, 6:31 PM",
            "resets_expires_at_iso": "2026-10-22T18:31:00+08:00",
            "credits_remaining": 441,
        },
    }
    card = quota_app.build_quota_card(item)

    assert card["provider"] == "codex"
    assert card["kind"] == "quota"
    assert card["icon"] == "֎"
    assert card["title"] == "Codex"
    assert card["status"] == "healthy"
    assert card["status_label"] == "正常"
    assert card["status_color"] == "ok"

    # 指标：5h 与 周额度
    metrics = {m["key"]: m for m in card["metrics"]}
    assert metrics["5h"]["value"] == "100%"
    assert metrics["5h"]["color"] == "green"
    assert metrics["5h"]["note"] == "等待周额度重置"  # 周限额为 0% 时

    assert metrics["weekly"]["value"] == "0%"
    assert metrics["weekly"]["color"] == "red"
    assert "重置" in metrics["weekly"]["note"]

    # 附加信息：重置额度 与 Credits
    extra_keys = [e["key"] for e in card["extra_notes"]]
    assert "resets" in extra_keys
    assert "credits" in extra_keys
    resets_note = next(e for e in card["extra_notes"] if e["key"] == "resets")
    assert "💡 重置额度 1 次" in resets_note["text"]
    assert resets_note["color"] == "green"

    # 验证 history() API 会包含 card 与 provider_order
    page = FakePage(CODEX_WITH_LIMIT_RESETS, screenshot_fails=0)
    asyncio.run(quota_app._capture(page, "codex"))

    history_data = quota_app.history(limit=10)
    assert "provider_order" in history_data
    assert "codex" in history_data["providers"]
    first = history_data["providers"]["codex"][0]
    assert "card" in first
    assert first["card"]["title"] == "Codex"
    assert len(first["card"]["metrics"]) == 2
    assert "trend_url" in first["card"]
    assert first["card"]["trend_url"].endswith("/providers/codex/trend.png")


def test_weekly_trend_chart_and_endpoint(tmp_path: Path, monkeypatch: Any) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(quota_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(quota_app, "DB_PATH", data_dir / "quota.sqlite3")
    monkeypatch.setattr(quota_app, "SCREENSHOT_DIR", data_dir / "screenshots")

    from quota_monitor.chart import extract_weekly_points, generate_weekly_trend_chart

    conn = quota_app._db()
    # 1. 空数据测试
    points_empty = extract_weekly_points(conn, "codex", days=7)
    assert points_empty == []
    empty_png = generate_weekly_trend_chart("codex", conn, title_label="Codex", days=7)
    assert empty_png.startswith(b"\x89PNG\r\n\x1a\n")

    # 2. 插入模拟周限额下降与跃升数据点
    now_utc = datetime.now(timezone.utc)
    mock_records = [
        (now_utc - timedelta(days=5), {"weekly_remaining": "100%"}),
        (now_utc - timedelta(days=4), {"weekly_remaining": "85%"}),
        (now_utc - timedelta(days=3), {"weekly_remaining": "60%"}),
        (now_utc - timedelta(days=2), {"weekly_remaining": "15%"}),
        (now_utc - timedelta(days=1), {"weekly_remaining": "100%"}),  # 周重置跃升
        (now_utc - timedelta(hours=6), {"weekly_remaining": "78%"}),
    ]
    for dt, flds in mock_records:
        conn.execute(
            """
            INSERT INTO captures(provider, captured_at, url, text, fields_json, status, confidence)
            VALUES (?, ?, 'https://test', 'text', ?, 'healthy', 1.0)
            """,
            ("codex", dt.isoformat(), json.dumps(flds)),
        )
    conn.commit()

    points = extract_weekly_points(conn, "codex", days=7)
    assert len(points) == 6
    assert points[0][1] == 100.0
    assert points[-1][1] == 78.0

    chart_png = generate_weekly_trend_chart("codex", conn, title_label="Codex", days=7)
    assert chart_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(chart_png) > 5000

    # 3. 验证 API 端点
    resp = quota_app.provider_trend("codex", days=7)
    assert resp.media_type == "image/png"
    assert resp.body.startswith(b"\x89PNG\r\n\x1a\n")
    conn.close()


def test_purge_page_memory_with_cdp_session() -> None:
    sent_commands: list[str] = []
    detached = False

    class _CDPSession:
        async def send(self, command: str) -> None:
            sent_commands.append(command)

        async def detach(self) -> None:
            nonlocal detached
            detached = True

    class _Context:
        async def new_cdp_session(self, page: object) -> _CDPSession:
            return _CDPSession()

    class _PageWithContext:
        def __init__(self) -> None:
            self.context = _Context()

    page = _PageWithContext()
    asyncio.run(quota_app._purge_page_memory(page))
    assert "HeapProfiler.collectGarbage" in sent_commands
    assert "Memory.forciblyPurgeJavaScriptMemory" in sent_commands
    assert detached is True


def test_purge_page_memory_handles_missing_context() -> None:
    # 模拟 FakePage 等无 context 属性对象，确保不抛出异常
    class _PageNoContext:
        pass

    asyncio.run(quota_app._purge_page_memory(_PageNoContext()))


def test_setup_subreaper_safe() -> None:
    quota_app._setup_subreaper()


def test_codex_loading_state_prevents_false_limit_reset(data_dir, monkeypatch):
    notifications = []

    async def fake_notify(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(quota_app, "_notify", fake_notify)

    # 1. 正常采集：已有 1 次重置额度
    page_normal = FakePage(CODEX_WITH_LIMIT_RESETS, screenshot_fails=0)
    res1 = asyncio.run(quota_app._capture(page_normal, "codex"))
    assert res1["status"] == "healthy"
    assert res1["fields"]["resets_available"] == 1
    notifications.clear()

    # 2. 模拟偶发残缺：页面处于 "Loading usage data"，仅渲染了静态 Credits: 0
    loading_text = "Codex and Work Analytics\n\nLoading usage data\n\nCredits remaining\n\n0\n"
    page_loading = FakePage(loading_text, screenshot_fails=0)
    res_loading = asyncio.run(quota_app._capture(page_loading, "codex"))
    assert res_loading["status"] == "loading"
    assert res_loading["limit_reset_detected"] is False

    # 3. 下一轮恢复正常：页面重新解析出 1 次重置额度
    res3 = asyncio.run(quota_app._capture(page_normal, "codex"))
    assert res3["status"] == "healthy"
    # 关键断言：不得误触发获得新重置通知！
    assert res3["limit_reset_detected"] is False
    assert len(notifications) == 0


def test_codex_new_schema_2026_10_capture_and_card(data_dir, monkeypatch):
    notifications = []

    async def fake_notify(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(quota_app, "_notify", fake_notify)

    raw_text_4059 = (
        "5-hour limit\n"
        "Resets in 5h 1m\n"
        "100% left\n"
        "Weekly limit\n"
        "Resets in 3d 20h\n"
        "66% left\n"
        "Credits\n"
        "Buy credits or turn on automatic reload to continue using Work and Codex when you reach usage limits. Learn more\n"
        "748 credits remaining\n"
        "Current balance\n"
        "Add more\n"
        "Automatic reload\n"
        "Usage limit resets\n"
        "Use a reset to restore your 5-hour limit, weekly limit, or both\n"
        "Available\n"
        "2\n"
        "History\n"
        "Full reset (Weekly + 5 hr)\n"
        "Expires October 22\n"
    )

    page = FakePage(raw_text_4059, screenshot_fails=0)
    capture = asyncio.run(quota_app._capture(page, "codex"))
    assert capture["status"] == "healthy"
    fields = capture["fields"]
    assert fields["remaining"] == "100%"
    assert fields["used"] == "0%"
    assert fields["weekly_remaining"] == "66%"
    assert fields["weekly_used_percent"] == "34%"
    assert fields["credits_remaining"] == "748"
    assert fields["reset_at"] == "5h 1m"
    assert fields["weekly_reset_at"] == "3d 20h"
    assert fields["resets_available"] == 2
    assert fields["resets_expires_at"] == "October 22"
    assert fields.get("weekly_reset_at_iso") is not None
    assert fields.get("reset_at_iso") is not None

    card = quota_app.build_quota_card(capture)
    assert card["status"] == "healthy"
    assert card["metrics"][0]["value"] == "100%"
    assert card["metrics"][1]["value"] == "66%"
    assert any("💡 重置额度" in note["text"] for note in card["extra_notes"])
    assert any("Credits 748" in note["text"] for note in card["extra_notes"])

    # 测试无重置次数的辅助账号 (Capture 4062 结构)
    raw_text_4062 = (
        "5-hour limit\n"
        "Resets in 1h 16m\n"
        "100% left\n"
        "Weekly limit\n"
        "Resets in 4d 17h\n"
        "21% left\n"
        "Credits\n"
        "0 credits remaining\n"
        "Usage limit resets\n"
        "Available\n"
        "0\n"
    )
    page_sub = FakePage(raw_text_4062, screenshot_fails=0)
    capture_sub = asyncio.run(quota_app._capture(page_sub, "codex_2"))
    assert capture_sub["status"] == "healthy"
    fields_sub = capture_sub["fields"]
    assert fields_sub["remaining"] == "100%"
    assert fields_sub["weekly_remaining"] == "21%"
    assert fields_sub["credits_remaining"] == "0"
    assert fields_sub["resets_available"] == 0
