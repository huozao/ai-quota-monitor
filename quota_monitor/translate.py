"""X (Twitter) 动态等纯文本即时翻译模块。

使用经由出境代理的轻量无鉴权 Google Translate API 将英文内容翻译为中文。
仅在翻译成功时写入内存缓存，避免网络偶发失败（None）被永久负缓存毒化。
支持并发信号量控制，防止突发请求打满代理或被接口限流。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from typing import Any

LOG = logging.getLogger("quota-monitor.translate")

_CACHE_MAX_SIZE = 256
_CACHE: OrderedDict[tuple[str, str], str] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_SEMAPHORE_MAP: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
_SEMAPHORE_LOCK = threading.Lock()


def clear_cache() -> None:
    """清空翻译内存缓存（主要供测试使用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def get_cached(text: str, target_lang: str = "zh-CN") -> str | None:
    """从缓存读取已翻译内容（只存成功结果）。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    key = (cleaned, target_lang)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    return None


def set_cached(text: str, translated: str, target_lang: str = "zh-CN") -> None:
    """将有效翻译结果写入缓存（LRU 淘汰）。"""
    cleaned = (text or "").strip()
    trans_cleaned = (translated or "").strip()
    if not cleaned or not trans_cleaned:
        return
    key = (cleaned, target_lang)
    with _CACHE_LOCK:
        _CACHE[key] = trans_cleaned
        _CACHE.move_to_end(key)
        if len(_CACHE) > _CACHE_MAX_SIZE:
            _CACHE.popitem(last=False)


def _translate_sync(
    text: str,
    target_lang: str = "zh-CN",
    proxy: str = "",
    timeout: float = 10.0,
) -> str | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    # 1. 优先查缓存（仅存成功结果，绝不缓存 None）
    cached = get_cached(cleaned, target_lang)
    if cached is not None:
        return cached

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
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data or not data[0]:
                return None
            translated = "".join(part[0] for part in data[0] if part and part[0]).strip()
            if translated:
                set_cached(cleaned, translated, target_lang)
                return translated
            return None
    except Exception as exc:
        LOG.warning("translate failed for text snippet %r: %s", cleaned[:50], exc)
        return None


def _get_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _SEMAPHORE_LOCK:
        sem = _SEMAPHORE_MAP.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(2)
            _SEMAPHORE_MAP[loop] = sem
            dead_loops = [lp for lp in _SEMAPHORE_MAP if lp.is_closed()]
            for lp in dead_loops:
                _SEMAPHORE_MAP.pop(lp, None)
        return sem


async def translate_text(
    text: str,
    target_lang: str = "zh-CN",
    proxy: str | None = None,
) -> str | None:
    """异步包装的文本翻译函数。

    受并发信号量（最大并发 2）保护，防止瞬时并发打满代理或被接口限流。
    失败或超时自动返回 None，绝不中断调用方主流程，且绝不把 None 写入缓存。
    """
    if not text or not text.strip():
        return None
    cleaned = text.strip()

    # 排队前查一次缓存
    cached = get_cached(cleaned, target_lang)
    if cached is not None:
        return cached

    actual_proxy = proxy if proxy is not None else os.getenv("CHROME_PROXY_SERVER", "").strip()
    sem = _get_semaphore()
    try:
        async with sem:
            # 拿到信号量后再查一次，防止排队期间已有相同请求翻译完成写入缓存
            cached_after_wait = get_cached(cleaned, target_lang)
            if cached_after_wait is not None:
                return cached_after_wait
            return await asyncio.to_thread(_translate_sync, cleaned, target_lang, actual_proxy)
    except Exception as exc:
        LOG.warning("async translate execution failed: %s", exc)
        return None
