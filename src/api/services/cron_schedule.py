"""Cron Schedule — 结构化时间配置 ↔ cron 表达式

前端不再直接输入 cron 表达式。用户通过 SchedulePicker 选择频率与时间，
后端把 ``Schedule`` 结构体转成 5 字段 cron 表达式存入 ``CronJob.cron_expr``，
同时把原始 ``Schedule`` JSON 存入 ``CronJob.schedule`` 用于编辑回显。

支持的 5 种 kind：
    - daily     : 每天 HH:MM
    - weekdays  : 周一-五 HH:MM
    - weekly    : 每周指定几天 HH:MM
    - monthly   : 每月 N 日 HH:MM
    - interval  : 每 N 分钟 / 每 N 小时

仅做 schedule → cron 单向转换；不做反向（避免歧义，老数据 schedule=null
直接展示原始 cron 表达式即可）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger

from src.api.utils.timezone import get_timezone, now_naive


class ScheduleError(ValueError):
    """Schedule 校验或转换错误"""


def _validate_time(time_str: str) -> tuple[int, int]:
    if not isinstance(time_str, str) or ":" not in time_str:
        raise ScheduleError(f"time 必须是 HH:MM 格式: {time_str!r}")
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ScheduleError(f"time 必须是 HH:MM 格式: {time_str!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as e:
        raise ScheduleError(f"time 解析失败: {time_str!r}") from e
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"time 越界: {time_str!r}")
    return hour, minute


def schedule_to_cron(schedule: dict) -> str:
    """Schedule 结构体转 5 字段 cron 表达式。

    示例：
        {"kind": "daily", "time": "09:30"}                          → "30 9 * * *"
        {"kind": "weekdays", "time": "09:30"}                       → "30 9 * * 0-4"
        {"kind": "weekly", "time": "09:30", "days": [1,3,5]}        → "30 9 * * 1,3,5"
        {"kind": "monthly", "time": "09:30", "dayOfMonth": 15}      → "30 9 15 * *"
        {"kind": "interval", "everyMinutes": 30}                    → "*/30 * * * *"
        {"kind": "interval", "everyHours": 2}                       → "0 */2 * * *"

    Raises:
        ScheduleError: schedule 结构非法或字段缺失。
    """
    if not isinstance(schedule, dict):
        raise ScheduleError("schedule 必须是 object")
    kind = schedule.get("kind")

    if kind == "daily":
        h, m = _validate_time(schedule.get("time", ""))
        return f"{m} {h} * * *"

    if kind == "weekdays":
        h, m = _validate_time(schedule.get("time", ""))
        # APScheduler day_of_week: 0=周一 .. 6=周日
        return f"{m} {h} * * 0-4"

    if kind == "weekly":
        h, m = _validate_time(schedule.get("time", ""))
        days = schedule.get("days")
        # 注意：与 APScheduler CronTrigger 对齐 → 0=周一 .. 6=周日
        if not isinstance(days, list) or not days:
            raise ScheduleError("weekly 必须提供非空 days 列表 (0=周一 .. 6=周日)")
        norm_days: list[int] = []
        for d in days:
            if not isinstance(d, int) or not (0 <= d <= 6):
                raise ScheduleError(f"weekly.days 元素必须为 0-6 整数: {d!r}")
            if d not in norm_days:
                norm_days.append(d)
        norm_days.sort()
        return f"{m} {h} * * {','.join(str(d) for d in norm_days)}"

    if kind == "monthly":
        h, m = _validate_time(schedule.get("time", ""))
        dom = schedule.get("dayOfMonth")
        if not isinstance(dom, int) or not (1 <= dom <= 31):
            raise ScheduleError(f"monthly.dayOfMonth 必须为 1-31 整数: {dom!r}")
        return f"{m} {h} {dom} * *"

    if kind == "interval":
        every_m = schedule.get("everyMinutes")
        every_h = schedule.get("everyHours")
        if every_m is not None and every_h is not None:
            raise ScheduleError("interval 不能同时设置 everyMinutes 和 everyHours")
        if every_m is not None:
            if not isinstance(every_m, int) or not (1 <= every_m <= 59):
                raise ScheduleError(f"interval.everyMinutes 必须为 1-59 整数: {every_m!r}")
            return f"*/{every_m} * * * *"
        if every_h is not None:
            if not isinstance(every_h, int) or not (1 <= every_h <= 23):
                raise ScheduleError(f"interval.everyHours 必须为 1-23 整数: {every_h!r}")
            return f"0 */{every_h} * * *"
        raise ScheduleError("interval 必须设置 everyMinutes 或 everyHours")

    raise ScheduleError(f"未知 schedule.kind: {kind!r}")


def next_fire_at(cron_expr: str, n: int = 5, base: datetime | None = None) -> list[datetime]:
    """计算 cron 表达式接下来 n 次触发时间（本地时区 naive datetime）。

    用于前端表单"未来 5 次执行预览"。借用 APScheduler 的 CronTrigger.get_next_fire_time。

    Raises:
        ScheduleError: cron 表达式非法。
    """
    if n <= 0:
        return []
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ScheduleError(f"cron 表达式必须是 5 个字段: {cron_expr!r}")

    tz = get_timezone()
    try:
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            timezone=tz,
        )
    except Exception as e:
        raise ScheduleError(f"cron 表达式解析失败: {cron_expr!r}: {e}") from e

    # 起始点：base 或当前本地时间，加 1 秒避开"当前正好命中"的边界
    if base is None:
        base = now_naive()
    base_aware = base.replace(tzinfo=tz) + timedelta(seconds=1)

    fires: list[datetime] = []
    cur = base_aware
    for _ in range(n):
        nxt = trigger.get_next_fire_time(None, cur)
        if nxt is None:
            break
        # 转回 naive 本地时间
        fires.append(nxt.replace(tzinfo=None))
        cur = nxt + timedelta(seconds=1)
    return fires
