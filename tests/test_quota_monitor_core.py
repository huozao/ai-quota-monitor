from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from quota_monitor.core import (
    claude_reset_sections,
    codex_reset_sections,
    countdown_label,
    event_key,
    get_meta,
    normalize_reset,
    pick_report_slot,
    record_event_once,
    reset_candidate,
    set_meta,
    screenshot_url,
    weekly_reset_candidate,
)


def test_reset_requires_healthy_previous_and_large_jump():
    old = {"status": "healthy", "fields": {"remaining": "10", "limit": "100"}}
    new = {"status": "healthy", "fields": {"remaining": "90", "limit": "100"}}
    assert reset_candidate(new, old)
    assert not reset_candidate({"status": "schema_changed", "fields": {}}, old)


def test_event_is_idempotent_and_screenshot_url_is_same_origin():
    conn = sqlite3.connect(":memory:")
    assert record_event_once(conn, event_key("codex", {"reset_at": "tomorrow"}), "codex", "now", {})
    assert not record_event_once(conn, event_key("codex", {"reset_at": "tomorrow"}), "codex", "now", {})
    assert screenshot_url(3) == "/v1/quota/captures/3/screenshot"


def test_only_weekly_window_can_trigger_reset_alert():
    old = {"status": "healthy", "fields": {"remaining": "20%", "weekly_remaining": "10%", "weekly_reset_at": "tomorrow"}}
    new = {"status": "healthy", "fields": {"remaining": "100%", "weekly_remaining": "100%", "weekly_reset_at": "next-week"}}
    assert weekly_reset_candidate(new, old)

    five_hour_only = {"status": "healthy", "fields": {"remaining": "100%", "weekly_remaining": "10%", "weekly_reset_at": "tomorrow"}}
    assert not weekly_reset_candidate(five_hour_only, old)


# 以下三份样本逐字取自 2026-09-07 webdock2 生产采集（captures id=48/44/47）的额度面板，
# 只删掉与判定无关的侧栏会话标题。合成样本测不出这类坑：错位只在「某个窗口不渲染
# Resets 行」时出现，而那正是真实页面才有的形状。
CLAUDE_IDLE_SESSION = """Plan usage limits
Pro
Current session
Starts when a message is sent
0% used
Weekly limits

Your limits are temporarily boosted. Your weekly Claude Code limit is 50% higher through September 13.
Learn more about usage limits
All models
Resets Sat 10:00 AM
25% used
Last updated: just now

Usage credits
Turn on usage credits to keep using Claude if you hit a plan limit. Learn more
$0.00 spent
Resets Oct 1
Unlimited
"""

CLAUDE_ACTIVE_SESSION = """Plan usage limits
Pro
Current session
Resets in 5 min
9% used
Weekly limits

Your limits are temporarily boosted. Your weekly Claude Code limit is 50% higher through September 13.
Learn more about usage limits
All models
Resets Sat 10:00 AM
25% used
Last updated: just now

Usage credits
Turn on usage credits to keep using Claude if you hit a plan limit. Learn more
$0.00 spent
Resets Oct 1
Unlimited
"""

CODEX_FULL_FIVE_HOUR = """Codex and Work Analytics
Usage
Code review
Balance

Codex and Work share the same usage limit.

5 hour usage limit

100%
remaining

Weekly usage limit

0%
remaining
Resets Sep 7, 2026 2:24 AM

Credits remaining

441
Credits extend usage beyond your plan limits.
Usage limit resets

Use a reset to restore your 5-hour limit, weekly limit, or both.
"""


def test_claude_reset_times_are_anchored_to_their_own_section():
    idle = claude_reset_sections(CLAUDE_IDLE_SESSION)
    assert idle["anchored"] and idle["session_idle"]
    assert idle["session_reset"] is None
    assert idle["weekly_reset"] == "Sat 10:00 AM"

    active = claude_reset_sections(CLAUDE_ACTIVE_SESSION)
    assert active["session_reset"] == "5 min"
    assert active["weekly_reset"] == "Sat 10:00 AM"
    assert not active["session_idle"]


def test_positional_extraction_puts_the_credits_reset_in_the_weekly_slot():
    """反证：喂旧判据（全页 findall 后按下标取）会在同一份样本上错位一格。"""
    resets = re.findall(r"resets?\s+([^\n]{1,80})", CLAUDE_IDLE_SESSION, flags=re.I)
    assert resets[0] == "Sat 10:00 AM"  # 旧代码当成 5 小时重置
    assert resets[1] == "Oct 1"  # 旧代码当成周重置，实际是 Usage credits 的每月重置
    assert claude_reset_sections(CLAUDE_IDLE_SESSION)["weekly_reset"] != "Oct 1"


def test_codex_reset_belongs_to_the_weekly_block_only():
    resets = codex_reset_sections(CODEX_FULL_FIVE_HOUR)
    assert resets["anchored"]
    assert resets["five_hour_reset"] is None  # 100% 剩余时页面不渲染这一行
    assert resets["weekly_reset"] == "Sep 7, 2026 2:24 AM"


def test_reset_text_is_normalised_against_the_capture_timezone():
    now = datetime(2026, 9, 7, 6, 44, tzinfo=timezone.utc)
    absolute = normalize_reset("Sep 7, 2026 2:24 AM", now)
    # 页面按浏览器时区渲染，容器是 UTC：这一刻在 CST 是 10:24，不是当天凌晨。
    assert absolute == datetime(2026, 9, 7, 2, 24, tzinfo=timezone.utc)
    assert normalize_reset("5 min", now) == datetime(2026, 9, 7, 6, 49, tzinfo=timezone.utc)
    assert normalize_reset("1 hr 29 min", now) == datetime(2026, 9, 7, 8, 13, tzinfo=timezone.utc)
    # 2026-09-07 是周一，下一个周六是 09-12。
    assert normalize_reset("Sat 10:00 AM", now) == datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
    assert normalize_reset("Oct 1", now) == datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    assert normalize_reset("当页面改版之后", now) is None
    assert normalize_reset("", now) is None


def test_weekly_reset_alert_requires_the_remaining_quota_to_recover():
    """重置时间字符串变了但剩余额度没动，是文案漂移，不是重置（2026-09-07 误报的形状）。"""
    old = {"status": "healthy", "fields": {"weekly_remaining": "75%", "weekly_reset_at": "Sat 10:00 AM"}}
    drifted = {"status": "healthy", "fields": {"weekly_remaining": "75%", "weekly_reset_at": "Oct 1"}}
    assert not weekly_reset_candidate(drifted, old)

    exhausted = {"status": "healthy", "fields": {"weekly_remaining": "0%", "weekly_reset_at": "Sep 7, 2026 2:24 AM"}}
    recovered = {"status": "healthy", "fields": {"weekly_remaining": "100%", "weekly_reset_at": "Sep 14, 2026 2:24 AM"}}
    assert weekly_reset_candidate(recovered, exhausted)


# 2026-09-07 上线后第一轮生产采集（capture id=61）：5 小时窗口用掉一部分之后，Codex 把
# 两个窗口的重置时间都渲染成裸时钟，页面顶部还多了一条带 "reset after" 的横幅。
CODEX_PARTIAL_FIVE_HOUR = """Your limit will reset after 2:24 AM.
Upgrade
Add credits
Balance

Codex and Work share the same usage limit.

5 hour usage limit

91%
remaining
Resets 3:59 AM

Weekly usage limit

0%
remaining
Resets 2:24 AM

Credits remaining

0
Credits extend usage beyond your plan limits.
Usage limit resets

Use a reset to restore your 5-hour limit, weekly limit, or both.
"""


def test_codex_banner_outside_the_sections_never_becomes_a_reset_time():
    resets = codex_reset_sections(CODEX_PARTIAL_FIVE_HOUR)
    assert resets["five_hour_reset"] == "3:59 AM"
    assert resets["weekly_reset"] == "2:24 AM"
    # 反证：旧判据取全页第一条，命中的是横幅那句，两个格子会一起变成 "after 2:24 AM."
    stale = re.search(r"resets?\s+([^\n]{1,80})", CODEX_PARTIAL_FIVE_HOUR, flags=re.I)
    assert stale.group(1).strip() == "after 2:24 AM."


def test_bare_clock_reset_resolves_to_the_next_occurrence():
    now = datetime(2026, 9, 7, 1, 9, tzinfo=timezone.utc)
    assert normalize_reset("3:59 AM", now) == datetime(2026, 9, 7, 3, 59, tzinfo=timezone.utc)
    assert normalize_reset("2:24 AM", now) == datetime(2026, 9, 7, 2, 24, tzinfo=timezone.utc)
    # 已经过去的时刻落到明天
    assert normalize_reset("12:30 AM", now) == datetime(2026, 9, 8, 0, 30, tzinfo=timezone.utc)
    # "Oct 1" 不能被当成 1 点
    assert normalize_reset("Oct 1", now) == datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def test_report_slots_are_judged_in_the_display_timezone():
    """08:00/13:00/20:00 必须按展示时区判，不是容器的 UTC。"""
    slots = ["08:00", "13:00", "20:00"]
    sgt = timezone(timedelta(hours=8))
    # 2026-09-06 20:21 UTC —— 按 UTC 判是「晚报」，但那一刻在 SGT 已经是次日 04:21，
    # 一档都不该发（用户实测收到的就是这条错位的「早报」）。
    utc_evening = datetime(2026, 9, 6, 20, 21, tzinfo=timezone.utc)
    assert pick_report_slot(utc_evening, slots) == "20:00"
    assert pick_report_slot(utc_evening.astimezone(sgt), slots) is None

    assert pick_report_slot(datetime(2026, 9, 7, 8, 5, tzinfo=sgt), slots) == "08:00"
    assert pick_report_slot(datetime(2026, 9, 7, 13, 0, tzinfo=sgt), slots) == "13:00"
    assert pick_report_slot(datetime(2026, 9, 7, 23, 59, tzinfo=sgt), slots) == "20:00"
    assert pick_report_slot(datetime(2026, 9, 7, 7, 59, tzinfo=sgt), slots) is None
    assert pick_report_slot(datetime(2026, 9, 7, 12, 0, tzinfo=sgt), ["nonsense"]) is None


def test_countdown_uses_english_units_and_never_bare_m():
    assert countdown_label(6 * 1440 + 21 * 60) == "6d 21h"
    assert countdown_label(5 * 1440 + 5 * 60) == "5d 5h"
    assert countdown_label(3 * 60 + 47) == "3h 47min"
    assert countdown_label(47) == "47min"
    assert countdown_label(2 * 60) == "2h"
    assert countdown_label(3 * 1440) == "3d"
    # 分钟不得写成单个 m —— 时间语境里会被读成 month
    assert not re.search(r"\d+m(?!in)\b", countdown_label(3 * 60 + 47))


def test_daily_report_slot_survives_a_restart():
    """「本日已发到哪一档」必须落库：只放进程内的话，重启一次就补发一遍。"""
    conn = sqlite3.connect(":memory:")
    assert get_meta(conn, "last_daily_report") == ""
    set_meta(conn, "last_daily_report", "2026-09-07:13:00")
    assert get_meta(conn, "last_daily_report") == "2026-09-07:13:00"
    set_meta(conn, "last_daily_report", "2026-09-07:20:00")
    assert get_meta(conn, "last_daily_report") == "2026-09-07:20:00"
