"""X 观察位：帖子提取、告警闸门与去重。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from quota_monitor import app as quota_app
from quota_monitor.core import alertable_posts, is_reset_post, post_id, post_is_fresh

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def _post(pid: str, text: str, hours_ago: float) -> dict[str, str]:
    return {
        "url": f"https://x.com/thsottiaux/status/{pid}",
        "posted_at": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
        "text": text,
        "author": "@thsottiaux",
    }


def test_post_id_ignores_link_suffixes():
    assert post_id("/thsottiaux/status/2097174560412246215") == "2097174560412246215"
    assert post_id("https://x.com/thsottiaux/status/2097174560412246215/photo/1") == "2097174560412246215"
    assert post_id("/thsottiaux") == ""


def test_reset_keyword_match_is_case_insensitive():
    keywords = ("reset", "limit")
    assert is_reset_post("All reset for everyone. Enjoy the week.", keywords)
    assert is_reset_post("Your LIMITS are back", keywords)
    assert not is_reset_post("we are so back", keywords)


def test_stale_and_unparsable_timestamps_are_not_fresh():
    assert post_is_fresh((NOW - timedelta(hours=3)).isoformat(), NOW, 24.0)
    assert not post_is_fresh((NOW - timedelta(hours=30)).isoformat(), NOW, 24.0)
    assert not post_is_fresh("", NOW, 24.0)
    assert not post_is_fresh("just now", NOW, 24.0)


def test_alertable_posts_filters_and_orders_oldest_first():
    posts = [
        _post("3", "Global reset incoming", 1),
        _post("2", "we are so back", 2),           # 不命中关键词
        _post("1", "All reset for everyone", 5),
        _post("0", "reset last week", 200),        # 太旧
        {"url": "/thsottiaux", "posted_at": NOW.isoformat(), "text": "reset"},  # 没有 id
    ]
    picked = alertable_posts(posts, NOW, keywords=("reset",), max_age_hours=24.0)
    assert [item["url"].rsplit("/", 1)[-1] for item in picked] == ["1", "3"]


class _First:
    """locator(...).first.wait_for()：没有帖子时模拟等待超时。"""

    def __init__(self, items: list[dict[str, str]]) -> None:
        self._items = items

    async def wait_for(self, timeout: int = 0) -> None:
        if not self._items:
            raise TimeoutError("locator.wait_for: Timeout exceeded")


class _PostLocator:
    def __init__(self, items: list[dict[str, str]]) -> None:
        self._items = items

    @property
    def first(self) -> _First:
        return _First(self._items)

    async def evaluate_all(self, script: str) -> list[dict[str, str]]:
        return self._items

    async def inner_text(self, timeout: int = 0) -> str:
        return "Tibo\n@thsottiaux\nposts"


class _Mouse:
    async def move(self, x: int, y: int) -> None:
        return None


class FakeXPage:
    url = "https://x.com/thsottiaux"

    def __init__(self, items: list[dict[str, str]]) -> None:
        self._items = items
        self.mouse = _Mouse()

    async def reload(self, **kwargs: object) -> None:
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    def locator(self, selector: str) -> _PostLocator:
        return _PostLocator(self._items)

    async def bring_to_front(self) -> None:
        return None

    async def evaluate(self, script: str) -> None:
        return None

    async def screenshot(self, path: str, **kwargs: object) -> None:
        from pathlib import Path

        Path(path).write_bytes(b"png")


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    monkeypatch.setattr(quota_app, "DATA_DIR", directory)
    monkeypatch.setattr(quota_app, "SCREENSHOT_DIR", directory / "screenshots")
    monkeypatch.setattr(quota_app, "DB_PATH", directory / "quota.sqlite3")
    return directory


@pytest.fixture()
def sent(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake_notify(item, screenshot_path):
        calls.append(item)

    monkeypatch.setattr(quota_app, "_notify_post", fake_notify)
    return calls


def _fresh(text: str, pid: str) -> dict[str, str]:
    return _post(pid, text, 0.5) | {
        "posted_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "url": f"/thsottiaux/status/{pid}",
    }


def test_capture_stores_posts_and_notifies_reset_hits_once(data_dir, sent):
    page = FakeXPage([_fresh("All reset for everyone. Enjoy the week.", "111"),
                      _fresh("I think I can officially say: we are so back", "222")])

    first = asyncio.run(quota_app._capture(page, quota_app.X_PROVIDER))
    assert first["status"] == "healthy"
    assert [item["id"] for item in first["new_posts"]] == ["111"]  # 玩梗那条不推送
    assert len(sent) == 1

    second = asyncio.run(quota_app._capture(page, quota_app.X_PROVIDER))
    assert second["new_posts"] == []  # 同一条帖子只推一次
    assert len(sent) == 1

    conn = quota_app._db()
    row = conn.execute(
        "SELECT fields_json FROM captures WHERE provider=? ORDER BY id DESC LIMIT 1",
        (quota_app.X_PROVIDER,),
    ).fetchone()
    conn.close()
    posts = json.loads(row["fields_json"])["posts"]
    assert [item["is_reset"] for item in posts] == [True, False]  # 两条都入库，只是标记不同


def test_capture_marks_schema_change_when_no_posts_found(data_dir, sent):
    result = asyncio.run(quota_app._capture(FakeXPage([]), quota_app.X_PROVIDER))

    assert result["status"] == "schema_changed"
    assert result["new_posts"] == []
    assert sent == []


def _captured(posts: list[dict[str, str]]) -> dict[str, object]:
    return {"provider": quota_app.X_PROVIDER, "status": "healthy", "fields": {"posts": posts}}


def test_daily_report_line_shows_the_latest_hit():
    segments = quota_app._watch_segments(_captured([
        _fresh("All reset for everyone", "111"),
        _fresh("we are so back", "222"),
    ]))

    assert len(segments) == 1
    assert "All reset for everyone" in segments[0]["text"]


def test_daily_report_line_survives_empty_and_missing_fields():
    """⚠️ 这一行抛异常会让整档日报丢失：_maybe_daily_report 已经把 meta 落库，
    异常被 _run 的 except 吞掉，那一档不会补发。"""
    for item in ({"provider": quota_app.X_PROVIDER, "status": "healthy", "fields": {}},
                 {"provider": quota_app.X_PROVIDER, "status": "schema_changed", "fields": None},
                 _captured([_fresh("we are so back", "333")])):
        segments = quota_app._watch_segments(item)
        assert len(segments) == 1
        assert "24h 内无重置相关动态" in segments[0]["text"]
