"""纯函数与持久化辅助，供 quota-monitor API 使用。

这里不包含浏览器控制；登录和验证永远由 noVNC 人工完成，采集器只在
``ATTACH_ENABLED`` 明确开启后读取页面。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def reset_candidate(current: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    if not previous:
        return False
    now, old = current.get("fields", {}), previous.get("fields", {})
    if current.get("status") != "healthy" or previous.get("status") != "healthy":
        return False
    if now.get("reset_at") and old.get("reset_at") and now["reset_at"] != old["reset_at"]:
        return True
    try:
        remaining = float(str(now["remaining"]).replace(",", "").rstrip("%"))
        old_remaining = float(str(old["remaining"]).replace(",", "").rstrip("%"))
    except (KeyError, TypeError, ValueError):
        return False
    try:
        limit = float(str(now.get("limit", old.get("limit"))).replace(",", "").rstrip("%"))
    except (TypeError, ValueError):
        return False
    return remaining > old_remaining and remaining - old_remaining >= max(10.0, limit * 0.2)


def percent(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").rstrip("%").strip())
    except (TypeError, ValueError):
        return None


def weekly_reset_candidate(current: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    """Only detect weekly-window resets; 5-hour window changes are not alerts.

    ⚠️ 重置时间字符串变了**不足以**判定重置。2026-09-07 实测：Claude 页面在会话未开始时
    不渲染 session 的 Resets 行，按出现顺序取值就把「Usage credits 每月重置」当成了周重置，
    ``weekly_reset_at`` 从 "Sat 10:00 AM" 变成 "Oct 1"，于是发出一条「周额度已重置」——
    而 ``weekly_remaining`` 全程 75% 没动过。真正的重置一定伴随剩余额度回升，判据落在剩余
    额度上，重置时间变化只作为附加条件。
    """
    if not previous or current.get("status") != "healthy" or previous.get("status") != "healthy":
        return False
    now, old = current.get("fields", {}), previous.get("fields", {})
    remaining, old_remaining = percent(now.get("weekly_remaining")), percent(old.get("weekly_remaining"))
    if remaining is None or old_remaining is None:
        return False
    delta = remaining - old_remaining
    reset_changed = bool(
        now.get("weekly_reset_at")
        and old.get("weekly_reset_at")
        and now["weekly_reset_at"] != old["weekly_reset_at"]
    )
    return (reset_changed and delta > 0) or delta >= 20.0


# 「Resets in 5 min」「Resets at 3:59 AM」「will reset after 2:24 AM.」三种前缀都出现过，
# 引导词不属于时间本身，跟着进字段会让归一化认不出来。
_RESET_LINE = re.compile(r"resets?\s+(?:in\s+|at\s+|after\s+)?([^\n]{1,80})", re.I)
_SESSION_IDLE = "starts when a message is sent"
# 小节边界。最后一项只当边界用（credits 的重置与额度窗口无关，必须被切在外面）。
CLAUDE_SECTIONS = (("session", "current session"), ("weekly", "weekly limits"), ("", "usage credits"))
CODEX_SECTIONS = (("five_hour", "5 hour usage limit"), ("weekly", "weekly usage limit"), ("", "credits remaining"))


def section_resets(text: str, sections: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """按小节边界取各窗口的重置时间，不按出现顺序取。

    ⚠️ 位置取值（``findall`` 之后 ``[0]`` 当 5 小时、``[1]`` 当周）在**某个窗口不渲染
    Resets 行**时会整体错位一格。2026-09-07 capture id=48 实证：Claude 会话未开始时显示
    "Starts when a message is sent"、没有 Resets 行，于是周重置被当成会话重置、与额度窗口
    无关的 "Usage credits" 每月重置被当成周重置，看板和飞书卡片上两个格子全错。Codex 侧
    5 小时用满前不显示自己的重置行，是同一个形状的坑。

    返回 ``{"anchored": bool, "<key>": "重置文案或 None", "blocks": {...}}``。
    """
    lowered = text.lower()
    positions: list[tuple[str, int]] = []
    cursor = 0
    for key, marker in sections:
        index = lowered.find(marker, cursor)
        if index < 0:
            continue
        positions.append((key, index))
        cursor = index + len(marker)
    result: dict[str, Any] = {"anchored": False, "blocks": {}}
    for key, _ in sections:
        if key:
            result[key] = None
    named = [item for item in positions if item[0]]
    if len(named) < len([key for key, _ in sections if key]):
        return result
    result["anchored"] = True
    ends = [index for _, index in positions[1:]] + [len(text)]
    for (key, start), end in zip(positions, ends):
        if not key:
            continue
        block = text[start:end]
        result["blocks"][key] = block
        match = _RESET_LINE.search(block)
        result[key] = match.group(1).strip().rstrip(".") if match else None
    return result


def claude_reset_sections(text: str) -> dict[str, Any]:
    """Claude 的会话/周重置时间，外加「会话尚未开始」这个状态。"""
    result = section_resets(text, CLAUDE_SECTIONS)
    session_block = result.get("blocks", {}).get("session", "")
    return {
        "anchored": result["anchored"],
        "session_reset": result.get("session"),
        "weekly_reset": result.get("weekly"),
        "session_idle": _SESSION_IDLE in session_block.lower(),
    }


def codex_reset_sections(text: str) -> dict[str, Any]:
    """Codex 的 5 小时/周重置时间。页面在窗口未用满时不渲染该窗口的 Resets 行。"""
    result = section_resets(text, CODEX_SECTIONS)
    return {
        "anchored": result["anchored"],
        "five_hour_reset": result.get("five_hour"),
        "weekly_reset": result.get("weekly"),
    }


_RELATIVE_UNITS = (
    (re.compile(r"(\d+)\s*d(?:ays?)?\b", re.I), "days"),
    (re.compile(r"(\d+)\s*h(?:r|rs|our|ours)?\b", re.I), "hours"),
    (re.compile(r"(\d+)\s*m(?:in|ins|inute|inutes)?\b", re.I), "minutes"),
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ABSOLUTE_FORMATS = ("%b %d, %Y %I:%M %p", "%b %d, %Y %H:%M", "%B %d, %Y %I:%M %p", "%b %d, %Y")


def _parse_clock(value: str) -> tuple[int, int] | None:
    match = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value.strip(), flags=re.I)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_clock_exact(value: str) -> tuple[int, int] | None:
    """整串就是一个时钟时间时才返回；"Oct 1" 这类必须落空，否则会被当成 1 点。"""
    if not re.fullmatch(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)", value.strip(), flags=re.I):
        return None
    return _parse_clock(value)


def normalize_reset(raw: Any, now: datetime) -> datetime | None:
    """把页面上的重置文案换算成带时区的绝对时间。

    页面按**浏览器所在时区**渲染，采集容器跑在 UTC，而 console 和飞书卡片在 CST 里读——
    裸字符串每往下游传一层就再猜一次时区，"Sep 7, 2026 2:24 AM" 因此在看板上被当成 CST，
    整整早了 8 小时，显示成「已过期」（2026-09-07 实测）。归一化只在采集侧做一次。

    ``now`` 必须带 tzinfo（容器本地时区），返回值沿用同一时区；认不出来返回 ``None``，
    由调用方保留原始字符串，不猜。
    """
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    delta: dict[str, int] = {}
    for pattern, unit in _RELATIVE_UNITS:
        match = pattern.search(value)
        if match:
            delta[unit] = int(match.group(1))
    if delta:
        return now + timedelta(**delta)
    cleaned = value.rstrip(".")
    for fmt in _ABSOLUTE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
    weekday_match = re.match(r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s+(.+)$", cleaned, flags=re.I)
    if weekday_match:
        prefix = weekday_match.group(1).lower()
        target = next(index for index, name in enumerate(_WEEKDAYS) if name.startswith(prefix))
        clock = _parse_clock(weekday_match.group(2))
        if clock is None:
            return None
        candidate = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        ahead = (target - now.weekday()) % 7
        if ahead == 0 and candidate <= now:
            ahead = 7
        return candidate + timedelta(days=ahead)
    # ⚠️ 2026-09-07 生产实测：5 小时窗口用掉一部分后，页面把重置时间渲染成裸时钟
    # （"Resets 3:59 AM"），既没有日期也没有星期。认不出来就没有 iso，看板会退回原文
    # 且不显示倒计时——这一格因此看着像坏了。按「下一次出现该时刻」解析。
    clock_only = _parse_clock_exact(cleaned)
    if clock_only is not None:
        candidate = now.replace(hour=clock_only[0], minute=clock_only[1], second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)
    month_day = re.match(r"([A-Za-z]{3,9})\s+(\d{1,2})$", cleaned)
    if month_day:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                parsed = datetime.strptime(
                    f"{month_day.group(1)} {month_day.group(2)} {now.year}", fmt
                ).replace(tzinfo=now.tzinfo)
            except ValueError:
                continue
            return parsed if parsed >= now else parsed.replace(year=now.year + 1)
    return None


def countdown_label(minutes: int) -> str:
    """倒计时文案，英文单位：``6d 21h`` / ``3h 47min`` / ``47min``。

    分钟一律写 ``min`` 不写 ``m``——单个 m 在时间语境里会被读成 month。卡片一格只有半屏
    宽，中文单位（「6天21小时」）比英文长一半，容易把这一行挤到折行。
    """
    days, hours, mins = minutes // 1440, minutes % 1440 // 60, minutes % 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}min" if mins else f"{hours}h"
    return f"{mins}min"


def pick_report_slot(now: datetime, slots: list[str]) -> str | None:
    """选出 ``now`` 所在这一天已经走过的最后一档报表时刻。

    ⚠️ ``now`` 必须带**展示时区**。容器跑在 UTC，按容器本地时间判会让 08:00/13:00/20:00
    三档落到 16:00/21:00/04:00 (SGT)——2026-09-06 那条「早报」是次日 04:21 才到的。
    """
    slot = None
    for item in slots:
        try:
            hour, minute = (int(value) for value in item.split(":", 1))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (TypeError, ValueError):
            continue
        if now >= candidate:
            slot = item
    return slot


def event_key(provider: str, fields: dict[str, Any]) -> str:
    marker = (fields.get("weekly_reset_at") or fields.get("reset_at") or
              fields.get("weekly_remaining") or fields.get("remaining") or "unknown")
    window = fields.get("weekly_window") or fields.get("window", "")
    return f"{provider}:{window}:{marker}"


def record_event_once(conn: sqlite3.Connection, key: str, provider: str, detected_at: str,
                      payload: dict[str, Any]) -> bool:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quota_events (event_key TEXT PRIMARY KEY, provider TEXT NOT NULL, detected_at TEXT NOT NULL, payload_json TEXT NOT NULL)"
    )
    cursor = conn.execute(
        "INSERT OR IGNORE INTO quota_events(event_key,provider,detected_at,payload_json) VALUES(?,?,?,?)",
        (key, provider, detected_at, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_meta(conn: sqlite3.Connection, key: str) -> str:
    """读一条跨重启的小状态。表不存在时返回空串，不抛。"""
    conn.execute("CREATE TABLE IF NOT EXISTS quota_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM quota_meta WHERE key=?", (key,)).fetchone()
    return "" if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """写一条跨重启的小状态。

    ⚠️ 日报的「本日已发到哪一档」必须落库，不能只放在进程内。放在模块级变量里时，
    容器每重启一次就会把当天最近一档重发一遍——2026-09-07 一天之内因为连续换镜像，
    同一档日报补发了好几次。
    """
    conn.execute("CREATE TABLE IF NOT EXISTS quota_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO quota_meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def screenshot_url(capture_id: int, *, prefix: str = "/v1/quota/captures") -> str:
    """Return a same-origin URL; callers should expose it behind Authelia."""
    return f"{prefix}/{int(capture_id)}/screenshot"


# ---- X（推文）观察位 --------------------------------------------------------

_STATUS_ID = re.compile(r"/status/(\d+)")


def post_id(url: Any) -> str:
    """从帖子永久链接里取出 status id。

    取 id 而不是整条 URL 当去重键：同一条帖子的链接会带 ``/photo/1``、``/analytics``
    等后缀，按整条 URL 去重会把同一条帖子重复推送。认不出来返回空串，调用方跳过——
    宁可漏推一条，也不要用不稳定的键去重。
    """
    match = _STATUS_ID.search(str(url or ""))
    return match.group(1) if match else ""


def is_reset_post(text: Any, keywords: tuple[str, ...]) -> bool:
    """帖子正文是否命中重置相关关键词（大小写不敏感）。"""
    lowered = str(text or "").lower()
    return any(word in lowered for word in keywords)


def post_is_fresh(posted_at: Any, now: datetime, max_age_hours: float) -> bool:
    """帖子是否落在告警窗口内。

    ⚠️ 首次上线时时间线上全是旧帖，缺这道闸门会一次性推出一串历史告警。时间取
    ``<time datetime>`` 的 ISO 值（X 给的是 UTC），认不出来当作**不新鲜**：宁可漏推。
    """
    raw = str(posted_at or "").strip()
    if not raw:
        return False
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo)
    age_hours = (now - moment).total_seconds() / 3600.0
    return -1.0 <= age_hours <= max_age_hours


def alertable_posts(posts: list[dict[str, Any]], now: datetime, *,
                    keywords: tuple[str, ...], max_age_hours: float) -> list[dict[str, Any]]:
    """挑出「新鲜 + 命中关键词 + 有稳定 id」的帖子，按时间从旧到新。

    顺序是从旧到新，多条一起出现时通知的先后跟发帖顺序一致。
    """
    picked = [
        post for post in posts
        if post_id(post.get("url"))
        and is_reset_post(post.get("text"), keywords)
        and post_is_fresh(post.get("posted_at"), now, max_age_hours)
    ]
    return sorted(picked, key=lambda item: str(item.get("posted_at") or ""))
