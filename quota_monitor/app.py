from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from patchright.async_api import async_playwright

from .core import (
    alertable_posts,
    claude_reset_sections,
    codex_reset_sections,
    event_key,
    countdown_label,
    get_meta,
    normalize_reset,
    is_reset_post,
    percent,
    post_id,
    pick_report_slot,
    record_event_once,
    set_meta,
    weekly_reset_candidate,
)

LOG = logging.getLogger("quota-monitor")
DATA_DIR = Path(os.getenv("QUOTA_DATA_DIR", "/app/quota_data"))
PROFILE_DIR = Path(os.getenv("QUOTA_PROFILE_DIR", "/app/quota_browser_data"))
ENABLE_FILE = DATA_DIR / "ATTACH_ENABLED"
DB_PATH = DATA_DIR / "quota.sqlite3"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
POLL_MIN = float(os.getenv("QUOTA_POLL_MINUTES_MIN", "20"))
POLL_MAX = float(os.getenv("QUOTA_POLL_MINUTES_MAX", "30"))
PAGE_SETTLE_SECONDS = float(os.getenv("QUOTA_PAGE_SETTLE_SECONDS", "5"))
SCREENSHOT_TIMEOUT_MS = int(float(os.getenv("QUOTA_SCREENSHOT_TIMEOUT_SECONDS", "15")) * 1000)
try:
    RETENTION_DAYS = max(1, int(os.getenv("QUOTA_RETENTION_DAYS", "7")))
except ValueError:
    RETENTION_DAYS = 7
# 容器跑在 UTC（页面也就按 UTC 渲染），但卡片和报表时刻都按这个时区走。
# 值的判定一律用归一化后的绝对时间，展示时区只决定「几点算早报」和文案怎么写。
DISPLAY_TZ = os.getenv("QUOTA_DISPLAY_TZ", "Asia/Singapore")
TZ_LABEL = os.getenv("QUOTA_TZ_LABEL", "SGT")
PUBLIC_API_PREFIX = os.getenv("QUOTA_PUBLIC_API_PREFIX", "/console/quota/api").rstrip("/")
PUBLIC_LINK = os.getenv("QUOTA_PUBLIC_LINK", "")

PROVIDERS = {
    "codex": "https://chatgpt.com/codex/cloud/settings/usage",
    "claude": "https://claude.ai/settings/usage",
}

# 额度页之外的观察位：Codex 的重置常在这个账号先放出来，比额度页早。
# 置空即关闭该采集。provider 名是 "x-<账号>"，与额度 provider 分开存、分开渲染。
X_ACCOUNT = os.getenv("QUOTA_X_ACCOUNT", "thsottiaux").strip().lstrip("@")
X_PROVIDER = f"x-{X_ACCOUNT}" if X_ACCOUNT else ""
X_URL = f"https://x.com/{X_ACCOUNT}" if X_ACCOUNT else ""
X_POST_LIMIT = 8
# 只有命中关键词的新帖才推送；其余照样入库、在看板上看得到。该账号发帖很杂（玩梗贴占多数），
# 全推等于把告警变成时间线。
X_KEYWORDS = tuple(
    word.strip().lower()
    for word in os.getenv("QUOTA_X_KEYWORDS", "reset,limit,quota,credit").split(",")
    if word.strip()
)
# ⚠️ 首次上线时时间线上全是旧帖，没有这道年龄闸门会一次性推出一串历史告警。
X_MAX_AGE_HOURS = float(os.getenv("QUOTA_X_MAX_AGE_HOURS", "24"))
# 时间线是虚拟列表，reload 之后要等它自己渲染出来；页面框架先到、帖子后到。
X_POST_WAIT_MS = int(float(os.getenv("QUOTA_X_POST_WAIT_SECONDS", "15")) * 1000)
X_SCREENSHOT_PADDING_PX = 40
X_SCREENSHOT_MAX_HEIGHT_PX = 3000

app = FastAPI(title="quota-monitor", version="0.1.0")
_task: asyncio.Task[None] | None = None
_stop = asyncio.Event()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS captures (
          id INTEGER PRIMARY KEY, provider TEXT NOT NULL, captured_at TEXT NOT NULL,
          url TEXT NOT NULL, text TEXT NOT NULL, fields_json TEXT NOT NULL,
          status TEXT NOT NULL, confidence REAL NOT NULL, screenshot_path TEXT,
          screenshot_sha256 TEXT, error TEXT, reset_key TEXT
        )"""
    )
    conn.commit()
    return conn


def _prune_expired(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """删除超过保留期的采集记录及其截图，避免历史卷无限增长。"""
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment.astimezone(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT id, screenshot_path FROM captures WHERE captured_at < ?", (cutoff,)
    ).fetchall()
    screenshot_root = SCREENSHOT_DIR.resolve()
    for row in rows:
        raw_path = row["screenshot_path"]
        if not raw_path:
            continue
        try:
            path = Path(raw_path).resolve()
            if path.parent == screenshot_root and path.is_file():
                path.unlink()
        except OSError:
            LOG.warning("failed to remove expired screenshot capture_id=%s", row["id"])
    if rows:
        conn.execute("DELETE FROM captures WHERE captured_at < ?", (cutoff,))
        conn.commit()
    return len(rows)


def _parse(provider: str, text: str) -> tuple[dict[str, Any], float, str]:
    """保守解析：页面结构变化时保留原文和截图，不把未知内容当成零额度。"""
    lowered = text.lower()
    if any(x in lowered for x in ("log in", "sign in", "登录", "登录后")):
        return {}, 0.0, "auth_required"
    if provider == "codex":
        fields: dict[str, Any] = {}
        five_hour = re.search(r"5\s*hour\s*usage\s*limit\s*([\d,.]+\s*%)\s*remaining", text, flags=re.I | re.S)
        weekly = re.search(r"weekly\s*usage\s*limit\s*([\d,.]+\s*%)\s*remaining", text, flags=re.I | re.S)
        credits = re.search(r"credits\s*remaining\s*([\d,.]+)", text, flags=re.I | re.S)
        # 重置时间按小节边界取。页面在某个窗口还没用满时不渲染该窗口的 Resets 行，
        # 全页第一条 Resets 因此可能属于任何一个窗口。
        resets = codex_reset_sections(text)
        if five_hour:
            remaining = five_hour.group(1).strip()
            try:
                used = f"{100.0 - float(remaining.rstrip('%')):g}%"
            except ValueError:
                used = None
            fields.update({"remaining": remaining, "window": "5-hour", "limit": "100%"})
            if used is not None:
                fields["used"] = used
        if weekly:
            fields["weekly_remaining"] = weekly.group(1).strip()
            try:
                fields["weekly_used_percent"] = f"{100.0 - float(fields['weekly_remaining'].rstrip('%')):g}%"
            except ValueError:
                pass
        if credits:
            fields["credits_remaining"] = credits.group(1).strip()
        # ⚠️ 该写法自 2026-09-07 起改正：此前把周重置时间同时写进 reset_at，看板「5 小时
        # 限额」格子于是显示的是周重置时间，读起来像 5 小时窗口要等到那一刻。
        if resets["five_hour_reset"]:
            fields["reset_at"] = resets["five_hour_reset"]
        if resets["weekly_reset"]:
            fields["weekly_reset_at"] = resets["weekly_reset"]
        return fields, min(1.0, 0.6 + 0.1 * len(fields)) if fields else 0.0, "healthy" if fields else "schema_changed"
    fields: dict[str, Any] = {}
    patterns = {
        "used": r"(?:used|已用)\s*[:：]?\s*([\d,.]+\s*%?)",
        "remaining": r"(?:remaining|left|剩余)\s*[:：]?\s*([\d,.]+\s*%?)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.I)
        if match:
            fields[key] = match.group(1).strip()
    # 重置时间只按小节边界取：全页第一条 Resets 在会话未开始时属于周额度块，
    # 第二条属于与额度窗口无关的 Usage credits。
    sections = claude_reset_sections(text)
    if sections["session_reset"]:
        fields["reset_at"] = sections["session_reset"]
    if sections["weekly_reset"]:
        fields["weekly_reset_at"] = sections["weekly_reset"]
    if sections["session_idle"]:
        fields["session_state"] = "idle"
    confidence = min(1.0, 0.35 + 0.25 * len(fields))
    status = "healthy" if fields else "schema_changed"
    if provider == "codex" and "usage" in lowered and fields:
        confidence = min(1.0, confidence + 0.1)
    return fields, confidence, status


# ⚠️ 帖子正文不能从 body 文本里切。时间线是虚拟列表，正文、转发说明、引用卡片和「显示更多」
# 在纯文本里连成一片，切不出边界也拿不到永久链接；每条帖子的 article 节点才是稳定边界。
POST_SELECTOR = "article[data-testid='tweet']"
_POST_JS = """
els => els.slice(0, LIMIT).map(el => {
  const time = el.querySelector('time');
  const anchor = time ? time.closest('a') : null;
  const body = el.querySelector("[data-testid='tweetText']");
  const name = el.querySelector("[data-testid='User-Name']");
  const handle = name ? (name.innerText.match(/@[A-Za-z0-9_]+/) || [''])[0] : '';
  return {
    url: anchor ? anchor.getAttribute('href') : '',
    posted_at: time ? time.getAttribute('datetime') : '',
    author: handle,
    text: body ? body.innerText.slice(0, 600) : '',
  };
})
"""


async def _read_posts(page: Any) -> list[dict[str, Any]]:
    items = await page.locator(POST_SELECTOR).evaluate_all(_POST_JS.replace("LIMIT", str(X_POST_LIMIT)))
    posts: list[dict[str, Any]] = []
    for item in items:
        href = str(item.get("url") or "")
        if not post_id(href):
            continue
        posts.append({
            "id": post_id(href),
            "url": f"https://x.com{href}" if href.startswith("/") else href,
            "posted_at": item.get("posted_at") or "",
            "author": item.get("author") or "",
            "text": (item.get("text") or "").strip(),
        })
    return posts


async def _parse_posts(page: Any, text: str) -> tuple[dict[str, Any], float, str]:
    """X 观察位的解析。拿不到帖子就是页面结构变了或登录态掉了，绝不当成「没有新消息」。"""
    lowered = text.lower()
    # ⚠️ 不能只靠 QUOTA_PAGE_SETTLE_SECONDS（默认 5 秒）就读。2026-09-08 生产实测 capture
    # id=249：页面文字里资料头、关注数、Posts/Replies 标签都在（登录态正常），只是时间线
    # 还没渲染，于是 0 条帖子被判成 schema_changed——看板显示「未读到帖子」、日报挂「采集
    # 异常」，全是假警报。等第一条 article 出现再读，等不到才按空结果判。
    try:
        await page.locator(POST_SELECTOR).first.wait_for(timeout=X_POST_WAIT_MS)
    except Exception:  # noqa: BLE001 - 等不到就走下面的空结果分支，由那里区分登录态与结构变化
        LOG.warning("timeline did not render within %dms", X_POST_WAIT_MS)
    posts = await _read_posts(page)
    if not posts:
        if "sign in" in lowered or "log in" in lowered or "登录" in lowered:
            return {}, 0.0, "auth_required"
        return {}, 0.0, "schema_changed"
    for item in posts:
        item["is_reset"] = is_reset_post(item["text"], X_KEYWORDS)
    return {"account": X_ACCOUNT, "posts": posts}, 0.9, "healthy"


PROVIDER_LABELS = {
    "codex": {"name": "Codex", "icon": "🤖", "color": "blue", "tag_color": "blue"},
    "claude": {"name": "Claude", "icon": "🟣", "color": "violet", "tag_color": "violet"},
}
STATUS_LABELS = {
    "healthy": "正常", "stale": "数据过期", "auth_required": "需重新登录",
    "blocked": "访问受限", "schema_changed": "页面结构变化", "network_error": "网络错误",
}


def _display_tz() -> tzinfo:
    try:
        return ZoneInfo(DISPLAY_TZ)
    except Exception:  # noqa: BLE001 - 时区库缺失不该让日报发不出去
        return timezone.utc


def _with_absolute_resets(fields: dict[str, Any]) -> dict[str, Any]:
    """把两个重置文案换算成绝对时间存下来，下游不再各自猜时区。"""
    now = datetime.now().astimezone()
    for source, target in (("reset_at", "reset_at_iso"), ("weekly_reset_at", "weekly_reset_at_iso")):
        moment = normalize_reset(fields.get(source), now)
        if moment is not None:
            fields[target] = moment.isoformat()
    return fields


def _reset_phrase(fields: dict[str, Any], key: str) -> str:
    """渲染成「重置 16:40 · 3h 47min」；认不出来就原样回显，不猜。

    移动端卡片一格只有半屏宽，文案每长一个字就更容易折行：同一天省掉日期，倒计时用
    英文单位，且**不带「后」**——这一行的语境已经是重置倒计时，那个字纯占宽度。
    """
    raw = str(fields.get(key) or "").strip()
    iso = fields.get(f"{key}_iso")
    if not iso:
        return raw
    try:
        moment = datetime.fromisoformat(str(iso)).astimezone(_display_tz())
    except ValueError:
        return raw
    now = datetime.now(tz=_display_tz())
    stamp = f"{moment:%H:%M}" if moment.date() == now.date() else f"{moment:%-m/%-d %H:%M}"
    minutes = int((moment - now).total_seconds() // 60)
    if minutes <= 0:
        return f"{stamp} · 已过"
    return f"{stamp} · {countdown_label(minutes)}"


def _quota_color(left: float | None) -> str:
    return "red" if left is not None and left <= 0 else "green"


def _metric_cell(name: str, remaining: Any, note: str, color: str = "") -> dict[str, str]:
    """一格：指标名、数值，副信息走 ``note``。

    副信息**必须**放 note 不能拼进 value——飞书 markdown 不支持行内字号，同一个 markdown
    元素里的字只能一样大；中枢把 note 单独渲染成 notation 号，窄屏下正好省出那点宽度，
    「重置 9/14 10:33 · 6d 21h」才不折行。
    """
    if remaining is None:
        return {"name": name, "value": "暂无数据"}
    head = f"<font color='{color}'>**{remaining}**</font>" if color else f"**{remaining}**"
    cell = {"name": name, "value": head}
    if note:
        cell["note"] = f"<font color='grey'>{note}</font>"
    return cell


def _provider_segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    """一个平台两段：一行标题 + 一行两格指标。

    ⚠️ 不要用 ``section`` 三列排这个。中枢的 section 把指标名和指标值各拼成**一个**
    markdown 块靠行数对齐，值一旦折行两列就整体错位——2026-09-07 实测卡片里
    「Credits」对到了上一行的值上。流量日报不出问题是因为它的值短到不折行，而额度
    这边「85% 剩余 · 9/14 10:33 · 6天22小时后」在半屏宽下必然折行。``fields`` 的每一
    格是独立 column，折行只会让那一格变高，不会牵动邻格。
    """
    provider = item["provider"]
    label = PROVIDER_LABELS.get(provider, {"name": provider, "icon": "•", "color": "grey"})
    fields = item.get("fields") or {}
    status = item.get("status", "stale")
    captured = datetime.fromisoformat(item["captured_at"]).astimezone(_display_tz())

    weekly_remaining = fields.get("weekly_remaining")
    weekly_left = percent(weekly_remaining)
    five_remaining = fields.get("remaining")
    five_left = percent(five_remaining)
    # 页面在窗口没用满时不给它自己的重置时间，这不是缺数据。周额度耗尽时 5 小时窗口
    # 有额度也用不了，那一行要说清楚在等谁。
    # 只有真拿到重置时间才写「重置 …」；拿不到就说明页面这一格没给，不人为推算。
    if fields.get("session_state") == "idle":
        five_note = "会话未开始"
    elif fields.get("reset_at"):
        five_note = f"重置 {_reset_phrase(fields, 'reset_at')}"
    elif weekly_left is not None and weekly_left <= 0:
        five_note = "等待周额度重置"
    elif five_left is not None and five_left >= 100:
        five_note = "额度充足"
    else:
        five_note = ""

    meta = [f"{captured:%H:%M} 采集"]
    if fields.get("credits_remaining") is not None:
        meta.append(f"Credits {fields['credits_remaining']}")
    if status != "healthy":
        meta.append(STATUS_LABELS.get(status, status))
    weekly_reset = _reset_phrase(fields, "weekly_reset_at")
    return [
        {
            "kind": "text",
            "text": f"**{label['icon']} {label['name']}**　<font color='grey'>{' · '.join(meta)}</font>",
        },
        {
            "kind": "fields",
            "fields": [
                _metric_cell("5h", five_remaining, five_note, _quota_color(five_left)),
                _metric_cell(
                    "周额度",
                    weekly_remaining,
                    f"重置 {weekly_reset}" if weekly_reset else "",
                    _quota_color(weekly_left),
                ),
            ],
        },
    ]


async def _notify(event: str, title: str, *, summary: str = "", subtitle: str = "",
                  theme: str = "", level: str = "info", tags: list[dict[str, str]] | None = None,
                  segments: list[dict[str, Any]] | None = None,
                  screenshot_paths: list[Path] | None = None, dedup_key: str = "") -> None:
    endpoint = os.getenv("NOTIFY_ENDPOINT", "").strip()
    token = os.getenv("NOTIFY_SOURCE_TOKEN", "").strip()
    if not endpoint or not token:
        return
    images: list[dict[str, str]] = []
    # ⚠️ summary 和 segments 会被飞书卡片**依次**渲染：同一段文字既传 summary 又传一个
    # text segment，卡片里就会出现两遍（2026-09-07 实测的日报重复就是这么来的）。
    # 明细一律走 segments，summary 只留一句概述或留空。
    body_segments: list[dict[str, Any]] = list(segments or [])
    for index, path in enumerate(screenshot_paths or []):
        try:
            raw = path.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                LOG.warning("notification image too large path=%s", path)
                continue
            ref = f"screen-{index}"
            caption = PROVIDER_LABELS.get(path.stem.split("-")[0], {}).get("name", path.stem)
            images.append({"ref": ref, "caption": f"{caption} 页面截图", "png_base64": __import__("base64").b64encode(raw).decode()})
            body_segments.append({"kind": "image", "image_ref": ref})
        except OSError:
            continue
    payload = {"source": "quota-monitor", "event": event, "level": level,
               "title": title, "subtitle": subtitle, "summary": summary,
               "segments": body_segments, "images": images,
               "link": {"text": "查看额度历史", "url": PUBLIC_LINK},
               "dedup_key": dedup_key or f"quota:{event}:{int(time.time())}"}
    if theme:
        payload["theme"] = theme
    if tags:
        payload["tags"] = tags
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Notify-Source": "quota-monitor", "X-Notify-Token": token,
    })
    try:
        await asyncio.to_thread(lambda: urllib.request.urlopen(request, timeout=20).read())
    except Exception as exc:
        LOG.warning("notification failed event=%s reason=%s", event, type(exc).__name__)


async def _force_repaint(page: Any) -> None:
    """截图前强制页面产出一帧新画面。

    ⚠️ Xvfb 下的 Chromium 对**完全静止**的页面会停止产帧，而 ``Page.captureScreenshot``
    要等一帧新画面才返回——2026-09-08 生产实测：codex 分析页 ``document.getAnimations()``
    为 0，空闲后第一次截图必然 30s 超时（连续 8 轮采集全挂）；claude 页有 5 个常驻动画一直
    产帧，因此从没失败过。先做一次可见变化（切前台 + 指针移动 + 1px 滚动回滚），同一张图
    随即 0.1s 返回。这不是重试能解决的问题，重试前必须先制造这一帧。
    """
    await page.bring_to_front()
    await page.mouse.move(20, 20)
    await page.evaluate("() => { window.scrollBy(0, 1); window.scrollBy(0, -1); }")
    await page.wait_for_timeout(400)


def _x_screenshot_height(viewport_height: float, article_bottom: float) -> int:
    """Return a bounded document height ending shortly after the last loaded post."""
    height = max(viewport_height, article_bottom + X_SCREENSHOT_PADDING_PX)
    return min(int(height), X_SCREENSHOT_MAX_HEIGHT_PX)


async def _x_screenshot_clip(page: Any) -> dict[str, int] | None:
    """Build a compact clip from the X timeline's rendered article boundary."""
    try:
        metrics = await page.evaluate(
            """() => {
                const articles = Array.from(document.querySelectorAll("article[data-testid='tweet']"));
                const bottoms = articles.map((article) => {
                    const rect = article.getBoundingClientRect();
                    return rect.bottom + window.scrollY;
                }).filter(Number.isFinite);
                return {
                    viewport_width: window.innerWidth,
                    viewport_height: window.innerHeight,
                    article_bottom: bottoms.length ? Math.max(...bottoms) : 0,
                };
            }"""
        )
        if not isinstance(metrics, dict):
            return None
        width = float(metrics.get("viewport_width", 0))
        viewport_height = float(metrics.get("viewport_height", 0))
        article_bottom = float(metrics.get("article_bottom", 0))
        if width <= 0 or viewport_height <= 0 or article_bottom <= 0:
            return None
        return {
            "x": 0,
            "y": 0,
            "width": int(width),
            "height": _x_screenshot_height(viewport_height, article_bottom),
        }
    except Exception as exc:  # noqa: BLE001 - screenshot can safely fall back to the viewport
        LOG.warning("failed to calculate X screenshot clip: %s: %s", type(exc).__name__, exc)
        return None


async def _screenshot(page: Any, provider: str) -> tuple[Path | None, str]:
    """取证截图。失败时返回 ``(None, 错误)``，**不产出指向不存在文件的路径**。

    ⚠️ 该写法自 2026-09-08 起改正：截图原本与读文字共用一个 ``try``，超时就把整条采集的
    ``status`` 写成 ``network_error``。文字其实解析成功（confidence 1.0），后果有三层：
    看板显示「网络错误」、日报常挂「采集异常」、而 ``weekly_reset_candidate`` 要求前后两次
    都 healthy，于是 **codex 的周额度重置告警被静默停用**。截图缺一张只是证据少一张。
    另外失败时旧代码仍把路径写进库，看板的 ``<img>`` 因此 404，页面上是一排碎图。
    """
    error = ""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)  # 首轮采集时 _db() 还没建过目录
    for attempt in range(2):
        path = SCREENSHOT_DIR / f"{provider}-{int(time.time())}.png"
        try:
            await _force_repaint(page)
            if provider == X_PROVIDER:
                clip = await _x_screenshot_clip(page)
                await page.screenshot(
                    path=str(path), clip=clip, full_page=False, timeout=SCREENSHOT_TIMEOUT_MS
                )
            else:
                await page.screenshot(path=str(path), full_page=True, timeout=SCREENSHOT_TIMEOUT_MS)
            return path, ""
        except Exception as exc:  # noqa: BLE001 - 截图失败不影响本轮采集结果
            error = f"screenshot: {type(exc).__name__}: {str(exc)[:200]}"
            LOG.warning("screenshot failed provider=%s attempt=%d reason=%s", provider, attempt + 1, error)
    return None, error


async def _notify_post(item: dict[str, Any], screenshot_path: Path | None) -> None:
    """观察位的推送：一条帖子一张卡。

    正文按 300 字截断——飞书卡片一格半屏宽，长推文会把卡片撑成一屏；要看全文点原帖。
    """
    stamp = ""
    try:
        stamp = f"{datetime.fromisoformat(str(item['posted_at']).replace('Z', '+00:00')).astimezone(_display_tz()):%m/%d %H:%M}"
    except ValueError:
        stamp = str(item.get("posted_at") or "")
    body = item["text"].strip().replace("\n", " ")
    if len(body) > 300:
        body = body[:300] + "…"
    await _notify(
        "quota.x_post",
        f"@{X_ACCOUNT} 发布重置相关动态",
        subtitle=f"发布于 {stamp} · {TZ_LABEL}",
        level="warn",
        tags=[{"text": "重置预告", "color": "orange"}],
        segments=[
            {"kind": "text", "text": f"**📣 @{item.get('author') or X_ACCOUNT}**　<font color='grey'>{stamp}</font>"},
            {"kind": "text", "text": body},
            {"kind": "text", "text": f"<font color='grey'>{item['url']}</font>"},
        ],
        screenshot_paths=[screenshot_path] if screenshot_path else [],
        dedup_key=f"quota:x_post:{X_ACCOUNT}:{item['id']}",
    )


async def _capture(page: Any, provider: str) -> dict[str, Any]:
    captured_at = _utc_now()
    error = ""
    status = "healthy"
    confidence = 0.0
    text = ""
    fields: dict[str, Any] = {}
    screenshot_path: Path | None = None
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(int(PAGE_SETTLE_SECONDS * 1000))
        text = await page.locator("body").inner_text(timeout=15_000)
        if provider == X_PROVIDER:
            fields, confidence, status = await _parse_posts(page, text)
        else:
            fields, confidence, status = _parse(provider, text)
        if provider == "claude":
            meters = await page.locator("[role=meter][aria-valuenow][aria-valuemax='100']").evaluate_all(
                "els => els.map(e => ({value:e.getAttribute('aria-valuenow'), text:e.getAttribute('aria-valuetext') || ''}))"
            )
            numeric_meters = [item for item in meters if re.fullmatch(r"\d+(?:\.\d+)?", str(item.get("value", "")))]
            if numeric_meters:
                session_used = float(numeric_meters[0]["value"])
                fields["session_used_percent"] = numeric_meters[0]["value"]
                fields["used"] = f"{session_used:g}%"
                fields["remaining"] = f"{100.0 - session_used:g}%"
                fields["unit"] = "%"
                fields["window"] = "5-hour session"
                if len(numeric_meters) > 1:
                    weekly_used = float(numeric_meters[1]["value"])
                    fields["weekly_used_percent"] = f"{weekly_used:g}%"
                    fields["weekly_remaining"] = f"{100.0 - weekly_used:g}%"
                confidence = max(confidence, 0.9)
                status = "healthy"
    except Exception as exc:  # 保留失败记录，不能静默丢失截图/错误
        error = type(exc).__name__ + ": " + str(exc)[:500]
        status = "network_error"
    screenshot_path, screenshot_error = await _screenshot(page, provider)
    if screenshot_error:
        error = f"{error} | {screenshot_error}" if error else screenshot_error
    fields = _with_absolute_resets(fields)
    digest = ""
    if screenshot_path and screenshot_path.exists():
        digest = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
    conn = _db()
    previous = conn.execute(
        "SELECT fields_json, status FROM captures WHERE provider=? ORDER BY id DESC LIMIT 1", (provider,)
    ).fetchone()
    reset_key = f"{provider}:{fields.get('reset_at','')}" if fields.get("reset_at") else None
    reset_detected = False
    old_fields: dict[str, Any] = {}
    if provider != X_PROVIDER and previous and status == "healthy" and previous["status"] == "healthy":
        try:
            old_fields = json.loads(previous["fields_json"] or "{}")
        except json.JSONDecodeError:
            old_fields = {}
        reset_detected = weekly_reset_candidate(
            {"status": status, "fields": fields},
            {"status": previous["status"], "fields": old_fields},
        )
    conn.execute(
        "INSERT INTO captures(provider,captured_at,url,text,fields_json,status,confidence,screenshot_path,screenshot_sha256,error,reset_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (provider, captured_at, str(getattr(page, "url", "")), text, json.dumps(fields, ensure_ascii=False), status,
         confidence, str(screenshot_path) if screenshot_path else None, digest, error, reset_key),
    )
    conn.commit()
    pruned = _prune_expired(conn)
    if reset_detected:
        reset_detected = record_event_once(
            conn, event_key(provider, {"weekly_reset_at": fields.get("weekly_reset_at"), "weekly_remaining": fields.get("weekly_remaining")}), provider, captured_at,
            {"provider": provider, "fields": fields, "screenshot_sha256": digest},
        )
    # 观察位的告警按帖子去重：键用 status id，和额度那条重置事件走同一张表，
    # 因此过了保留期被清掉采集明细也不会重复推送。
    new_posts: list[dict[str, Any]] = []
    if provider == X_PROVIDER and status == "healthy":
        for item in alertable_posts(fields.get("posts", []), datetime.now(timezone.utc),
                                    keywords=X_KEYWORDS, max_age_hours=X_MAX_AGE_HOURS):
            if record_event_once(conn, f"x:{X_ACCOUNT}:{item['id']}", provider, captured_at, item):
                new_posts.append(item)
    conn.close()
    LOG.info("capture provider=%s status=%s confidence=%.2f screenshot=%s pruned=%d", provider, status, confidence, bool(digest), pruned)
    result = {"provider": provider, "status": status, "fields": fields, "screenshot_path": screenshot_path,
              "captured_at": captured_at, "reset_detected": reset_detected, "new_posts": new_posts}
    for item in new_posts:
        await _notify_post(item, screenshot_path)
    if reset_detected:
        label = PROVIDER_LABELS.get(provider, {"name": provider, "icon": "•", "color": "grey"})
        # 与日报同样的理由：值会折行，不能用靠行数对齐的 section 三列。
        cells: list[dict[str, str]] = [{
            "name": "剩余",
            "value": f"<font color='green'>**{fields.get('weekly_remaining', '未知')}**</font>",
        }]
        if old_fields.get("weekly_remaining"):
            cells[0]["note"] = f"<font color='grey'>重置前 {old_fields['weekly_remaining']}</font>"
        next_reset = _reset_phrase(fields, "weekly_reset_at")
        if next_reset:
            cells.append({"name": "下次重置", "value": f"**{next_reset}**"})
        moment = datetime.fromisoformat(captured_at).astimezone(_display_tz())
        await _notify(
            "quota.reset",
            f"{label['name']} 周额度已重置",
            subtitle=f"检测于 {moment:%Y/%m/%d %H:%M} · {TZ_LABEL}",
            level="warn",
            tags=[{"text": "周额度", "color": label.get("tag_color", "blue")}],
            segments=[
                {"kind": "text", "text": f"**♻️ {label['name']} 周额度**"},
                {"kind": "fields", "fields": cells},
            ],
            screenshot_paths=[screenshot_path] if screenshot_path else [],
            dedup_key=f"quota:weekly_reset:{provider}:{fields.get('weekly_reset_at','')}",
        )
    return result


def _find_page(pages: list[Any], provider: str) -> Any | None:
    """按 provider 找已经开着的标签页。

    ⚠️ 不能沿用「provider 名出现在 URL 里」这条通用规则：观察位的 provider 是
    ``x-<账号>``，而单字母 ``x`` 会命中任何含 x 的 URL。观察位按 ``x.com`` 域名匹配。
    """
    if provider == X_PROVIDER:
        return next((p for p in pages if "x.com/" in p.url.lower()), None)
    return next((p for p in pages if provider in p.url.lower()), None)


async def _run() -> None:
    # 关键安全边界：在 ATTACH_ENABLED 出现前，浏览器存在但 Playwright/CDP 不接管。
    while not _stop.is_set():
        if not ENABLE_FILE.exists():
            await asyncio.sleep(5)
            continue
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9224")
                context = browser.contexts[0] if browser.contexts else None
                if context is None:
                    raise RuntimeError("quota Chrome has no browser context")
                pages = list(context.pages)
                captured: list[dict[str, Any]] = []
                targets = dict(PROVIDERS)
                if X_PROVIDER:
                    targets[X_PROVIDER] = X_URL
                for provider, url in targets.items():
                    page = _find_page(pages, provider)
                    if page is None:
                        page = await context.new_page()
                        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                        pages.append(page)
                    elif provider == "claude" and "#settings/usage" not in page.url:
                        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    elif provider == "codex" and "/codex/cloud/settings/" not in page.url:
                        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    elif provider == X_PROVIDER and X_ACCOUNT.lower() not in page.url.lower():
                        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    captured.append(await _capture(page, provider))
                await _maybe_daily_report(captured)
                # CDP attach 的 browser.close() 会关闭用户仍需查看的 Chrome；
                # async with 退出时只结束 Playwright 连接，浏览器进程和手工登录态保持不动。
        except Exception as exc:
            LOG.warning("monitor cycle failed: %s: %s", type(exc).__name__, exc)
        delay = random.uniform(POLL_MIN, POLL_MAX) * 60
        await asyncio.sleep(delay)


def _watch_segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    """日报里的观察位：只写最近一条命中关键词的帖子，没有就写一句「无」。

    这一行的作用是让人知道**观察位还活着**——完全不渲染的话，采集挂了在日报上看不出来。
    """
    posts = (item.get("fields") or {}).get("posts") or []
    hits = alertable_posts(posts, datetime.now(timezone.utc),
                           keywords=X_KEYWORDS, max_age_hours=24.0)
    head = f"**📣 @{X_ACCOUNT}**"
    if not hits:
        return [{"kind": "text", "text": f"{head}　<font color='grey'>24h 内无重置相关动态</font>"}]
    latest = hits[-1]
    body = latest["text"].replace("\n", " ")
    if len(body) > 160:
        body = body[:160] + "…"
    return [{"kind": "text", "text": f"{head}　<font color='grey'>{body}</font>"}]


REPORT_META_KEY = "last_daily_report"


async def _maybe_daily_report(captured: list[dict[str, Any]]) -> None:
    slots = [item.strip() for item in os.getenv("QUOTA_REPORT_TIMES", "08:00,13:00,20:00").split(",")]
    # ⚠️ 该写法自 2026-09-07 起改正：这里原本取 datetime.now().astimezone()，容器没设 TZ
    # 就是 UTC，于是 08:00/13:00/20:00 三档实际落在 16:00/21:00/04:00 (SGT)——文档写的
    # 「早/中/晚」，收到的却是下午、深夜和凌晨（09-06 那条日报 20:00 档 04:21 才到）。
    # 报表时刻必须按展示时区判，日期键同理，否则跨零点还会多发一次。
    now = datetime.now(tz=_display_tz())
    slot = pick_report_slot(now, slots)
    key = f"{now.date()}:{slot}" if slot else ""
    if not slot:
        return
    # 「本日已发到哪一档」落库：只放进程内的话，容器每重启一次就补发一遍。
    conn = _db()
    try:
        if get_meta(conn, REPORT_META_KEY) == key:
            return
        set_meta(conn, REPORT_META_KEY, key)
    finally:
        conn.close()
    # ⚠️ 观察位不能进 _provider_segments：那个函数按「5h + 周额度」两格排版，
    # 传一条没有额度字段的记录进去会渲染出两格「暂无数据」。
    quota_items = [item for item in captured if item["provider"] != X_PROVIDER]
    watch_items = [item for item in captured if item["provider"] == X_PROVIDER]
    paths = [item["screenshot_path"] for item in quota_items if item.get("screenshot_path")]
    stamp = now
    segments = [seg for item in quota_items for seg in _provider_segments(item)]
    for item in watch_items:
        segments.extend(_watch_segments(item))
    unhealthy = [item["provider"] for item in captured if item.get("status") != "healthy"]
    if unhealthy:
        segments.append({
            "kind": "text",
            "text": f"<font color='red'>⚠️</font> 采集异常：{'、'.join(unhealthy)}，以截图为准。",
        })
    tags = [
        {"text": PROVIDER_LABELS.get(item["provider"], {}).get("name", item["provider"]),
         "color": PROVIDER_LABELS.get(item["provider"], {}).get("tag_color", "blue")}
        for item in captured
    ][:3]
    await _notify(
        "quota.daily_report",
        "AI 额度日报",
        subtitle=f"截至 {stamp:%Y/%m/%d %H:%M} · {TZ_LABEL}",
        tags=tags,
        segments=segments,
        screenshot_paths=paths,
        dedup_key=f"quota:daily_report:{now.date()}:{slot}",
    )


@app.on_event("startup")
async def startup() -> None:
    global _task
    _stop.clear()
    _task = asyncio.create_task(_run())


@app.on_event("shutdown")
async def shutdown() -> None:
    _stop.set()
    if _task:
        _task.cancel()


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "attach_enabled": ENABLE_FILE.exists(), "db": str(DB_PATH)}


@app.post("/v1/monitor/enable")
def enable_monitor() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ENABLE_FILE.touch()
    return {"ok": True, "attach_enabled": True}


@app.post("/v1/monitor/disable")
def disable_monitor() -> dict[str, Any]:
    ENABLE_FILE.unlink(missing_ok=True)
    return {"ok": True, "attach_enabled": False}


def _quota_public(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "used": fields.get("used"),
        "remaining": fields.get("remaining"),
        "weekly_used": fields.get("weekly_used_percent"),
        "weekly_remaining": fields.get("weekly_remaining"),
        "credits_remaining": fields.get("credits_remaining"),
        "unit": "%" if any("%" in str(v) for v in fields.values()) else "",
        "window": fields.get("window", ""),
        "reset_at": fields.get("reset_at"),
        "weekly_reset_at": fields.get("weekly_reset_at"),
        # 归一化后的绝对时间；前端优先用它，拿不到时才回退显示原始字符串。
        "reset_at_iso": fields.get("reset_at_iso"),
        "weekly_reset_at_iso": fields.get("weekly_reset_at_iso"),
        "session_state": fields.get("session_state"),
        # 观察位的字段；额度 provider 这两项为 None，前端据此区分要渲染哪种卡片。
        "account": fields.get("account"),
        "posts": fields.get("posts"),
    }


def _shot_url(row: Any) -> str | None:
    """只有截图文件真的还在，才给看板 URL。

    ⚠️ 采集失败或截图超时的记录以前照样带 ``screenshot_url``，看板的 ``<img>`` 拿到 404，
    页面上就是一排碎图（2026-09-08 实测 codex 那一列）。过期清理也会删文件、留不住的
    历史行同理，所以判据是**文件存在**，不是路径非空。
    """
    path = row["screenshot_path"]
    if not path or not Path(path).is_file():
        return None
    return f"{PUBLIC_API_PREFIX}/captures/{row['id']}/screenshot"


@app.get("/v1/quota/latest")
def latest() -> dict[str, Any]:
    conn = _db()
    rows = conn.execute("SELECT * FROM captures WHERE id IN (SELECT MAX(id) FROM captures GROUP BY provider)").fetchall()
    conn.close()
    providers: dict[str, Any] = {}
    for row in rows:
        fields = json.loads(row["fields_json"] or "{}")
        public = _quota_public(fields)
        providers[row["provider"]] = {
            "id": row["id"], "status": row["status"], "used": fields.get("used"),
            "remaining": fields.get("remaining"), "unit": "%" if any("%" in str(v) for v in fields.values()) else "",
            "window": fields.get("window", ""), "reset_at": fields.get("reset_at"),
            "screenshot_url": _shot_url(row),
            "screenshot_captured_at": row["captured_at"], "confidence": row["confidence"],
            "text": row["text"], "error": row["error"],
        }
        providers[row["provider"]].update(public)
    return {"providers": providers, "generated_at": _utc_now()}


@app.get("/v1/quota/history")
def history(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    conn = _db()
    rows = conn.execute("SELECT * FROM captures ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    result: dict[str, list[dict[str, Any]]] = {name: [] for name in PROVIDERS}
    for row in rows:
        fields = json.loads(row["fields_json"] or "{}")
        public = _quota_public(fields)
        result.setdefault(row["provider"], []).append({
            "id": row["id"], "captured_at": row["captured_at"], "status": row["status"],
            "used": fields.get("used"), "remaining": fields.get("remaining"),
            "unit": "%" if any("%" in str(v) for v in fields.values()) else "",
            "window": fields.get("window", ""), "reset_at": fields.get("reset_at"),
            "screenshot_url": _shot_url(row),
            "confidence": row["confidence"], "error": row["error"],
        })
        result[row["provider"]][-1].update(public)
    return {"providers": result, "generated_at": _utc_now()}


@app.get("/v1/quota/captures/{capture_id}/screenshot")
@app.get("/console/quota/api/captures/{capture_id}/screenshot")
def screenshot(capture_id: int) -> FileResponse:
    conn = _db()
    row = conn.execute("SELECT screenshot_path FROM captures WHERE id=?", (capture_id,)).fetchone()
    conn.close()
    if not row or not row["screenshot_path"] or not Path(row["screenshot_path"]).is_file():
        raise HTTPException(status_code=404, detail="screenshot not found")
    return FileResponse(row["screenshot_path"], media_type="image/png")
