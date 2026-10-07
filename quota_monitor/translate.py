"""X (Twitter) 动态等纯文本即时翻译模块。

使用经由出境代理的轻量无鉴权 Google Translate API 将英文内容翻译为中文。
支持内存 LRU 缓存以避免每轮采集重复请求未变更的推文。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any

LOG = logging.getLogger("quota-monitor.translate")


@functools.lru_cache(maxsize=256)
def _translate_sync(text: str, target_lang: str = "zh-CN", proxy: str = "") -> str | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    encoded = urllib.parse.quote(cleaned)
    url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target_lang}&dt=t&q={encoded}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()

    try:
        with opener.open(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data or not data[0]:
                return None
            translated = "".join(part[0] for part in data[0] if part and part[0])
            return translated.strip() if translated else None
    except Exception as exc:
        LOG.warning("translate failed for text snippet %r: %s", cleaned[:50], exc)
        return None


async def translate_text(
    text: str,
    target_lang: str = "zh-CN",
    proxy: str | None = None,
) -> str | None:
    """异步包装的文本翻译函数。

    失败或超时自动返回 None，绝不中断调用方主流程。
    """
    if not text or not text.strip():
        return None
    actual_proxy = proxy if proxy is not None else os.getenv("CHROME_PROXY_SERVER", "").strip()
    try:
        return await asyncio.to_thread(_translate_sync, text.strip(), target_lang, actual_proxy)
    except Exception as exc:
        LOG.warning("async translate execution failed: %s", exc)
        return None
