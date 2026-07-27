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

from datetime import datetime

from src.api.services.cron_engine import CronEngine, CronExpressionError


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
        {"kind": "weekdays", "time": "09:30"}                       → "30 9 * * 1-5"
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
        return f"{m} {h} * * 1-5"

    if kind == "weekly":
        h, m = _validate_time(schedule.get("time", ""))
        days = schedule.get("days")
        if not isinstance(days, list) or not days:
            raise ScheduleError("weekly 必须提供非空 days 列表 (0=周日, 1=周一 .. 6=周六)")
        norm_days: list[int] = []
        for d in days:
            if not isinstance(d, int) or not (0 <= d <= 6):
                raise ScheduleError(f"weekly.days 元素必须为 0-6 整数: {d!r}")
            if d not in norm_days:
                norm_days.append(d)
        # 前端按周一到周日提交，保留该顺序；连续三天及以上压缩为范围。
        order = [1, 2, 3, 4, 5, 6, 0]
        ordered = [d for d in order if d in norm_days]
        chunks: list[str] = []
        run: list[int] = []
        for day in ordered:
            if day == 0:
                if run:
                    chunks.append(_format_day_run(run))
                    run = []
                chunks.append("0")
            elif not run or day == run[-1] + 1:
                run.append(day)
            else:
                chunks.append(_format_day_run(run))
                run = [day]
        if run:
            chunks.append(_format_day_run(run))
        return f"{m} {h} * * {','.join(chunks)}"

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


def _format_day_run(days: list[int]) -> str:
    if len(days) >= 3:
        return f"{days[0]}-{days[-1]}"
    return ",".join(str(day) for day in days)


_DAY_NAMES = {
    0: "周日",
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
}


def describe_schedule(schedule: dict | None, cron_expr: str | None = None) -> str:
    """生成保存确认和工具回执使用的自然语言计划。"""
    if not schedule:
        return f"自定义计划（{cron_expr or ''}）"
    kind = schedule.get("kind")
    time_str = schedule.get("time", "")
    if kind == "daily":
        return f"每天 {time_str}"
    if kind == "weekdays":
        return f"周一至周五 {time_str}"
    if kind == "weekly":
        days = schedule.get("days") or []
        ordered = [day for day in [1, 2, 3, 4, 5, 6, 0] if day in days]
        if ordered == [2, 3, 4, 5, 6, 0]:
            return f"周二至周日 {time_str}"
        return f"{'、'.join(_DAY_NAMES[day] for day in ordered)} {time_str}"
    if kind == "monthly":
        return f"每月 {schedule.get('dayOfMonth')} 日 {time_str}"
    if kind == "interval":
        if schedule.get("everyMinutes") is not None:
            return f"每 {schedule['everyMinutes']} 分钟"
        return f"每 {schedule.get('everyHours')} 小时"
    return f"自定义计划（{cron_expr or ''}）"


def next_fire_at(cron_expr: str, n: int = 5, base: datetime | None = None) -> list[datetime]:
    """计算 cron 表达式接下来 n 次触发时间（本地时区 naive datetime）。

    用于前端表单"未来 5 次执行预览"。

    Raises:
        ScheduleError: cron 表达式非法。
    """
    try:
        return CronEngine.next_fires(cron_expr, base=base, count=n)
    except CronExpressionError as exc:
        raise ScheduleError(str(exc)) from exc
