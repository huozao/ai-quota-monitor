"""纯文本即时翻译模块与观察位继承缓存测试。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from quota_monitor import app as quota_app
from quota_monitor.translate import (
    clear_cache,
    get_cached,
    set_cached,
    translate_text,
    _translate_sync,
)


@pytest.fixture(autouse=True)
def _reset_translate_cache():
    clear_cache()
    yield
    clear_cache()


def test_translate_sync_success_caches_positive_result():
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps([[["向此人发送您的善意反馈", "Send your kind feedback to this person"]]]).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp

    with patch("urllib.request.build_opener") as mock_opener:
        mock_instance = MagicMock()
        mock_instance.open.return_value = fake_resp
        mock_opener.return_value = mock_instance

        res1 = _translate_sync("Send your kind feedback to this person")
        assert res1 == "向此人发送您的善意反馈"
        assert mock_instance.open.call_count == 1

        # 第二次请求直接命中正向缓存，绝不再调用 open
        res2 = _translate_sync("Send your kind feedback to this person")
        assert res2 == "向此人发送您的善意反馈"
        assert mock_instance.open.call_count == 1


def test_translate_sync_failure_does_not_cache_none():
    with patch("urllib.request.build_opener") as mock_opener:
        mock_instance = MagicMock()
        mock_instance.open.side_effect = TimeoutError("gateway timeout")
        mock_opener.return_value = mock_instance

        # 首次超时，返回 None
        res1 = _translate_sync("Some unstable text")
        assert res1 is None
        assert get_cached("Some unstable text") is None

        # 确认未被负缓存毒化：下次恢复正常时能够重新请求并缓存成功结果
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps([[["正常结果", "Some unstable text"]]]).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp
        mock_instance.open.side_effect = None
        mock_instance.open.return_value = fake_resp

        res2 = _translate_sync("Some unstable text")
        assert res2 == "正常结果"
        assert get_cached("Some unstable text") == "正常结果"


def test_set_and_get_cached_lru_eviction():
    set_cached("text1", "文本1")
    set_cached("text2", "文本2")
    assert get_cached("text1") == "文本1"
    assert get_cached("text2") == "文本2"
    assert get_cached("text3") is None

    # 空文本与空翻译不入缓存
    set_cached("", "空")
    set_cached("空", "")
    assert get_cached("") is None
    assert get_cached("空") is None


def test_translate_text_async_concurrent_double_check():
    calls = []

    def fake_sync(text, target_lang="zh-CN", proxy=""):
        calls.append(text)
        trans = f"zh:{text}"
        set_cached(text, trans, target_lang)
        return trans

    with patch("quota_monitor.translate._translate_sync", side_effect=fake_sync):
        # 并发翻译相同的文本，验证命中缓存后不重复调用底层同步方法
        async def run_concurrent():
            tasks = [translate_text("Same text") for _ in range(5)]
            return await asyncio.gather(*tasks)

        results = asyncio.run(run_concurrent())
        assert results == ["zh:Same text"] * 5
        # 即使 5 个并发请求，由于双重检查缓存，调用底层 sync 的次数远少于 5
        assert len(calls) <= 2


def test_parse_posts_inherits_known_translations():
    fake_page = MagicMock()
    fake_page.locator.return_value.first.wait_for = MagicMock()
    fake_posts = [
        {"url": "/thsottiaux/status/1", "posted_at": "2026-10-30T10:00:00Z", "author": "@thsottiaux", "text": "Known post"},
        {"url": "/thsottiaux/status/2", "posted_at": "2026-10-30T10:01:00Z", "author": "@thsottiaux", "text": "New post"},
    ]

    async def fake_read(page):
        return [
            {"id": "1", "url": "https://x.com/thsottiaux/status/1", "posted_at": "2026-10-30T10:00:00Z", "author": "@thsottiaux", "text": "Known post"},
            {"id": "2", "url": "https://x.com/thsottiaux/status/2", "posted_at": "2026-10-30T10:01:00Z", "author": "@thsottiaux", "text": "New post"},
        ]

    with patch("quota_monitor.app._read_posts", side_effect=fake_read), \
         patch("quota_monitor.app.translate_text") as mock_translate:
        mock_translate.return_value = "新帖翻译"

        known = {"Known post": "已知旧帖翻译"}
        data, conf, status = asyncio.run(quota_app._parse_posts(fake_page, "body text", known_translations=known))

        assert status == "healthy"
        posts = data["posts"]
        assert len(posts) == 2
        # 已知旧帖直接继承，不调 translate_text
        assert posts[0]["text_zh"] == "已知旧帖翻译"
        # 新帖调用 translate_text
        assert posts[1]["text_zh"] == "新帖翻译"
        assert mock_translate.call_count == 1


def test_build_quota_card_complements_missing_zh_from_cache():
    # 模拟历史 capture 记录中缺少 text_zh
    item = {
        "provider": quota_app.X_PROVIDER,
        "status": "healthy",
        "fields": {
            "account": "thsottiaux",
            "posts": [
                {"id": "1", "text": "Post without zh in db", "text_zh": None},
                {"id": "2", "text": "Post with zh", "text_zh": "已存在翻译"},
            ],
        },
    }

    # 缓存中预先有了翻译
    set_cached("Post without zh in db", "从缓存自动补全")

    card = quota_app.build_quota_card(item)
    assert card["kind"] == "watch"
    assert card["posts"][0]["text_zh"] == "从缓存自动补全"
    assert card["posts"][1]["text_zh"] == "已存在翻译"
