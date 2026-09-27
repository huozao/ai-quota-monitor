"""周限额变化趋势曲线图生成模块。

使用 Pillow 轻量高性能绘制高清（2x Retina 采样）深色科技风折线图，
展示周限额从 100% 逐步下降及发生周重置跃升的真实轨迹。
"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

CANDIDATE_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_LANCZOS = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", 1))


def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_path in CANDIDATE_FONTS:
        if Path(font_path).is_file():
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def extract_weekly_points(
    conn: sqlite3.Connection,
    provider: str,
    days: int = 7,
    tz: Any = timezone.utc,
) -> list[tuple[datetime, float]]:
    """从数据库中提取指定 provider 过去指定天数内的周限额剩余数据点。"""
    rows = conn.execute(
        """
        SELECT captured_at, fields_json
        FROM captures
        WHERE provider = ? AND status = 'healthy'
        ORDER BY id ASC
        """,
        (provider,),
    ).fetchall()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    points: list[tuple[datetime, float]] = []

    for captured_at_str, fields_json in rows:
        try:
            dt_utc = datetime.fromisoformat(captured_at_str)
            if dt_utc < cutoff:
                continue
            fields = json.loads(fields_json or "{}")
            rem_str = fields.get("weekly_remaining")
            if rem_str is not None:
                val = float(str(rem_str).rstrip("%"))
            elif fields.get("weekly_used_percent") is not None:
                val = 100.0 - float(str(fields["weekly_used_percent"]).rstrip("%"))
            else:
                continue
            val = max(0.0, min(100.0, val))
            dt_local = dt_utc.astimezone(tz)
            points.append((dt_local, val))
        except Exception:
            continue

    points.sort(key=lambda x: x[0])
    return points


def generate_weekly_trend_chart(
    provider: str,
    conn: sqlite3.Connection,
    title_label: str = "",
    days: int = 7,
    tz: Any = timezone.utc,
) -> bytes:
    """生成周限额剩余趋势图的 PNG 图片二进制数据。"""
    points = extract_weekly_points(conn, provider, days=days, tz=tz)

    SCALE = 2
    WIDTH, HEIGHT = 900 * SCALE, 440 * SCALE
    PAD_L, PAD_R, PAD_T, PAD_B = 75 * SCALE, 45 * SCALE, 80 * SCALE, 50 * SCALE
    PLOT_W = WIDTH - PAD_L - PAD_R
    PLOT_H = HEIGHT - PAD_T - PAD_B

    img = Image.new("RGBA", (WIDTH, HEIGHT), (11, 18, 32, 255))
    draw = ImageDraw.Draw(img)

    # 容器边框与卡片背景
    draw.rounded_rectangle(
        [10 * SCALE, 10 * SCALE, WIDTH - 10 * SCALE, HEIGHT - 10 * SCALE],
        radius=14 * SCALE,
        fill=(13, 23, 40, 255),
        outline=(42, 58, 85, 255),
        width=1 * SCALE,
    )

    font_title = _get_font(20 * SCALE)
    font_sub = _get_font(13 * SCALE)
    font_axis = _get_font(11 * SCALE)
    font_badge = _get_font(12 * SCALE)

    display_title = title_label or provider
    chart_title = f"周限额剩余变化趋势 · {display_title}"

    # Y 轴网格线与刻度（0%, 25%, 50%, 75%, 100%）
    for y_pct in [0, 25, 50, 75, 100]:
        y = PAD_T + PLOT_H - int(PLOT_H * y_pct / 100)
        line_col = (42, 58, 85, 255) if y_pct in (0, 100) else (28, 42, 65, 255)
        draw.line([(PAD_L, y), (PAD_L + PLOT_W, y)], fill=line_col, width=1 * SCALE)
        lbl = f"{y_pct}%"
        draw.text((PAD_L - 48 * SCALE, y - 8 * SCALE), lbl, fill=(145, 162, 187, 255), font=font_axis)

    if not points:
        # 无数据占位
        draw.text((PAD_L, 22 * SCALE), chart_title, fill=(233, 239, 248, 255), font=font_title)
        draw.text((PAD_L, 48 * SCALE), "暂无足够历史趋势数据（需要至少 1 次健康采集）", fill=(145, 162, 187, 255), font=font_sub)
        mid_x = PAD_L + PLOT_W // 2
        mid_y = PAD_T + PLOT_H // 2
        draw.text((mid_x - 90 * SCALE, mid_y - 10 * SCALE), "暂无采集数据记录", fill=(145, 162, 187, 255), font=font_axis)
        final = img.resize((WIDTH // SCALE, HEIGHT // SCALE), RESAMPLE_LANCZOS)
        buf = io.BytesIO()
        final.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    min_t = points[0][0].timestamp()
    max_t = points[-1][0].timestamp()
    span_t = max(1.0, max_t - min_t)

    # 坐标转换与重置跃升检测
    coords: list[tuple[int, int]] = []
    resets: list[tuple[int, datetime, float]] = []
    prev_val: float | None = None

    for dt, val in points:
        x = PAD_L + int(PLOT_W * (dt.timestamp() - min_t) / span_t)
        y = PAD_T + PLOT_H - int(PLOT_H * min(100.0, max(0.0, val)) / 100.0)
        coords.append((x, y))
        # 判定周重置跃升（剩余额度回升 >= 20%）
        if prev_val is not None and val - prev_val >= 20.0:
            resets.append((x, dt, val))
        prev_val = val

    # 曲线下方半透明渐变面积
    if len(coords) >= 2:
        poly = [(coords[0][0], PAD_T + PLOT_H)] + coords + [(coords[-1][0], PAD_T + PLOT_H)]
        area_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        area_draw = ImageDraw.Draw(area_layer)
        area_draw.polygon(poly, fill=(66, 211, 155, 38))
        img = Image.alpha_composite(img, area_layer)
        draw = ImageDraw.Draw(img)

    # 周重置指示虚线与标签
    for rx, rdt, rval in resets:
        draw.line([(rx, PAD_T), (rx, PAD_T + PLOT_H)], fill=(244, 189, 97, 180), width=1 * SCALE)
        draw.text((rx + 4 * SCALE, PAD_T + 8 * SCALE), "♻ 周重置", fill=(244, 189, 97, 255), font=font_badge)

    # 绘制主折线
    if len(coords) >= 2:
        for i in range(len(coords) - 1):
            draw.line([coords[i], coords[i + 1]], fill=(66, 211, 155, 255), width=3 * SCALE)
    else:
        cx, cy = coords[0]
        draw.line([(PAD_L, cy), (PAD_L + PLOT_W, cy)], fill=(66, 211, 155, 255), width=3 * SCALE)

    # 采样渲染细微数据点
    step = max(1, len(coords) // 40)
    for x, y in coords[::step]:
        draw.ellipse([x - 2 * SCALE, y - 2 * SCALE, x + 2 * SCALE, y + 2 * SCALE], fill=(66, 211, 155, 180))

    # 最新点光晕高亮
    lx, ly = coords[-1]
    draw.ellipse([lx - 9 * SCALE, ly - 9 * SCALE, lx + 9 * SCALE, ly + 9 * SCALE], fill=(66, 211, 155, 60))
    draw.ellipse([lx - 5 * SCALE, ly - 5 * SCALE, lx + 5 * SCALE, ly + 5 * SCALE], fill=(66, 211, 155, 255))
    draw.ellipse([lx - 2 * SCALE, ly - 2 * SCALE, lx + 2 * SCALE, ly + 2 * SCALE], fill=(255, 255, 255, 255))

    # 最新值徽标胶囊
    latest_val = points[-1][1]
    badge_text = f"剩余 {latest_val:g}%"
    bw = 82 * SCALE
    bx = min(WIDTH - PAD_R - bw, max(PAD_L, lx - bw // 2))
    by = max(PAD_T + 8 * SCALE, ly - 28 * SCALE)
    draw.rounded_rectangle(
        [bx, by, bx + bw, by + 22 * SCALE],
        radius=6 * SCALE,
        fill=(23, 38, 60, 255),
        outline=(66, 211, 155, 255),
        width=1 * SCALE,
    )
    draw.text((bx + 8 * SCALE, by + 3 * SCALE), badge_text, fill=(66, 211, 155, 255), font=font_badge)

    # X 轴时间刻度
    num_x_labels = 6
    if len(points) >= 2 and span_t > 60:
        for i in range(num_x_labels):
            t_val = min_t + i * span_t / (num_x_labels - 1)
            x = PAD_L + int(i * PLOT_W / (num_x_labels - 1))
            dt_label = datetime.fromtimestamp(t_val, tz=tz).strftime("%m/%d %H:%M")
            draw.text((x - 28 * SCALE, PAD_T + PLOT_H + 10 * SCALE), dt_label, fill=(145, 162, 187, 255), font=font_axis)
    else:
        only_date = points[0][0].strftime("%m/%d %H:%M")
        draw.text((PAD_L, PAD_T + PLOT_H + 10 * SCALE), only_date, fill=(145, 162, 187, 255), font=font_axis)

    # 顶部标题与副标题元信息
    sub_title = f"当前周限额剩余: {latest_val:g}% (已用 {100 - latest_val:g}%) · 过去 {days} 天 · 采集点: {len(points)} 次"
    draw.text((PAD_L, 22 * SCALE), chart_title, fill=(233, 239, 248, 255), font=font_title)
    draw.text((PAD_L, 48 * SCALE), sub_title, fill=(145, 162, 187, 255), font=font_sub)

    # Lanczos 高保真抗锯齿下采样
    final = img.resize((WIDTH // SCALE, HEIGHT // SCALE), RESAMPLE_LANCZOS)
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


PALETTE = [
    (119, 181, 255, 255),  # 蓝色 (Codex)
    (251, 146, 60, 255),   # 橙色 (Claude)
    (66, 211, 155, 255),   # 绿色 (Codex副号)
    (192, 132, 252, 255),  # 紫色
    (244, 189, 97, 255),   # 黄色
]


def generate_combined_weekly_trend_chart(
    conn: sqlite3.Connection,
    providers: list[tuple[str, str]],
    days: int = 7,
    tz: Any = timezone.utc,
) -> bytes:
    """生成多个模型周限额合并走势对比的单图看板（一图流）。"""
    SCALE = 2
    WIDTH, HEIGHT = 920 * SCALE, 460 * SCALE
    PAD_L, PAD_R, PAD_T, PAD_B = 75 * SCALE, 45 * SCALE, 88 * SCALE, 50 * SCALE
    PLOT_W = WIDTH - PAD_L - PAD_R
    PLOT_H = HEIGHT - PAD_T - PAD_B

    img = Image.new("RGBA", (WIDTH, HEIGHT), (11, 18, 32, 255))
    draw = ImageDraw.Draw(img)

    # 容器边框与卡片背景
    draw.rounded_rectangle(
        [10 * SCALE, 10 * SCALE, WIDTH - 10 * SCALE, HEIGHT - 10 * SCALE],
        radius=14 * SCALE,
        fill=(13, 23, 40, 255),
        outline=(42, 58, 85, 255),
        width=1 * SCALE,
    )

    font_title = _get_font(20 * SCALE)
    font_sub = _get_font(12 * SCALE)
    font_axis = _get_font(11 * SCALE)
    font_badge = _get_font(12 * SCALE)

    chart_title = "周限额剩余综合走势看板 · 多模型对比"
    draw.text((PAD_L, 20 * SCALE), chart_title, fill=(233, 239, 248, 255), font=font_title)

    # 提取所有 provider 的数据点
    series: list[dict[str, Any]] = []
    all_timestamps: list[float] = []

    for idx, (p_id, p_label) in enumerate(providers):
        color = PALETTE[idx % len(PALETTE)]
        pts = extract_weekly_points(conn, p_id, days=days, tz=tz)
        if pts:
            for dt, _ in pts:
                all_timestamps.append(dt.timestamp())
        series.append({
            "provider": p_id,
            "label": p_label,
            "color": color,
            "points": pts,
        })

    # Y 轴网格线与刻度（0%, 25%, 50%, 75%, 100%）
    for y_pct in [0, 25, 50, 75, 100]:
        y = PAD_T + PLOT_H - int(PLOT_H * y_pct / 100)
        line_col = (42, 58, 85, 255) if y_pct in (0, 100) else (28, 42, 65, 255)
        draw.line([(PAD_L, y), (PAD_L + PLOT_W, y)], fill=line_col, width=1 * SCALE)
        lbl = f"{y_pct}%"
        draw.text((PAD_L - 48 * SCALE, y - 8 * SCALE), lbl, fill=(145, 162, 187, 255), font=font_axis)

    if not all_timestamps:
        draw.text((PAD_L, 50 * SCALE), "暂无足够历史走势数据", fill=(145, 162, 187, 255), font=font_sub)
        final = img.resize((WIDTH // SCALE, HEIGHT // SCALE), RESAMPLE_LANCZOS)
        buf = io.BytesIO()
        final.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    min_t = min(all_timestamps)
    max_t = max(all_timestamps)
    span_t = max(1.0, max_t - min_t)

    # 绘制顶部图例与最新状态徽标
    leg_x = PAD_L
    leg_y = 52 * SCALE
    for s in series:
        pts = s["points"]
        latest_str = f"{pts[-1][1]:g}%" if pts else "暂无"
        lbl_text = f"● {s['label']}: {latest_str}"
        draw.text((leg_x, leg_y), lbl_text, fill=s["color"], font=font_badge)
        leg_x += int(len(lbl_text) * 11 * SCALE) + 20 * SCALE

    # 绘制各模型折线
    for s in series:
        pts = s["points"]
        if not pts:
            continue
        color = s["color"]
        coords: list[tuple[int, int]] = []
        for dt, val in pts:
            x = PAD_L + int(PLOT_W * (dt.timestamp() - min_t) / span_t)
            y = PAD_T + PLOT_H - int(PLOT_H * min(100.0, max(0.0, val)) / 100.0)
            coords.append((x, y))

        if len(coords) >= 2:
            for i in range(len(coords) - 1):
                draw.line([coords[i], coords[i + 1]], fill=color, width=3 * SCALE)
        else:
            cx, cy = coords[0]
            draw.line([(PAD_L, cy), (PAD_L + PLOT_W, cy)], fill=color, width=3 * SCALE)

        # 细微采样点
        step = max(1, len(coords) // 30)
        for x, y in coords[::step]:
            draw.ellipse([x - 2 * SCALE, y - 2 * SCALE, x + 2 * SCALE, y + 2 * SCALE], fill=color)

        # 最新点光晕高亮
        lx, ly = coords[-1]
        c_alpha = (color[0], color[1], color[2], 70)
        draw.ellipse([lx - 8 * SCALE, ly - 8 * SCALE, lx + 8 * SCALE, ly + 8 * SCALE], fill=c_alpha)
        draw.ellipse([lx - 4 * SCALE, ly - 4 * SCALE, lx + 4 * SCALE, ly + 4 * SCALE], fill=color)
        draw.ellipse([lx - 2 * SCALE, ly - 2 * SCALE, lx + 2 * SCALE, ly + 2 * SCALE], fill=(255, 255, 255, 255))

    # X 轴时间刻度
    num_x_labels = 6
    if span_t > 60:
        for i in range(num_x_labels):
            t_val = min_t + i * span_t / (num_x_labels - 1)
            x = PAD_L + int(i * PLOT_W / (num_x_labels - 1))
            dt_label = datetime.fromtimestamp(t_val, tz=tz).strftime("%m/%d %H:%M")
            draw.text((x - 28 * SCALE, PAD_T + PLOT_H + 10 * SCALE), dt_label, fill=(145, 162, 187, 255), font=font_axis)

    final = img.resize((WIDTH // SCALE, HEIGHT // SCALE), RESAMPLE_LANCZOS)
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
