"""Linux/Vixie 五字段 Cron 的唯一语义实现。"""

from __future__ import annotations

from datetime import datetime
import re

from croniter import croniter

from src.api.utils.timezone import get_timezone, now_naive


class CronExpressionError(ValueError):
    """Cron 表达式不符合 Linux/Vixie 五字段标准。"""


_FIELD_RULES = (
    ("分钟", 0, 59),
    ("小时", 0, 23),
    ("日", 1, 31),
    ("月", 1, 12),
    ("星期", 0, 7),
)
_INTEGER_RE = re.compile(r"^\d+$")


def _validate_numeric_field(
    field: str,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> None:
    """校验项目支持的数字型 Vixie Cron 字段语法。

    支持 ``*``、数字、范围、列表及 ``*/N``/``A-B/N`` 步进；明确拒绝
    croniter 额外支持的 R/L/W/#/? 等扩展，避免预览与逐分钟匹配采用不同
    的随机或扩展语义。
    """
    if not field:
        raise CronExpressionError(f"cron {label}字段不能为空")

    for item in field.split(","):
        if not item:
            raise CronExpressionError(f"cron {label}字段列表包含空项: {field!r}")

        base, separator, step_text = item.partition("/")
        if separator:
            if "/" in step_text or not _INTEGER_RE.fullmatch(step_text):
                raise CronExpressionError(f"cron {label}字段步进无效: {item!r}")
            if int(step_text) <= 0:
                raise CronExpressionError(f"cron {label}字段步进必须大于 0: {item!r}")
            if base != "*" and "-" not in base:
                raise CronExpressionError(
                    f"cron {label}字段步进只能用于通配符或范围: {item!r}"
                )

        if base == "*":
            continue

        if "-" in base:
            start_text, range_separator, end_text = base.partition("-")
            if (
                not range_separator
                or "-" in end_text
                or not _INTEGER_RE.fullmatch(start_text)
                or not _INTEGER_RE.fullmatch(end_text)
            ):
                raise CronExpressionError(f"cron {label}字段范围无效: {item!r}")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise CronExpressionError(f"cron {label}字段范围起点不能大于终点: {item!r}")
            values = (start, end)
        elif _INTEGER_RE.fullmatch(base):
            values = (int(base),)
        else:
            raise CronExpressionError(
                f"cron {label}字段只支持数字、*、范围、列表和步进: {item!r}"
            )

        if any(value < minimum or value > maximum for value in values):
            raise CronExpressionError(
                f"cron {label}字段必须在 {minimum}-{maximum} 范围内: {item!r}"
            )


class CronEngine:
    """统一 Cron 校验、分钟匹配和未来触发时间计算。

    星期字段采用 crontab 约定：0/7=周日，1=周一，…，6=周六；
    日与星期同时受限时使用 Vixie Cron 的 OR 语义。
    """

    @staticmethod
    def validate(expr: str) -> str:
        normalized = " ".join((expr or "").strip().split())
        fields = normalized.split()
        if len(fields) != 5:
            raise CronExpressionError(
                f"cron 表达式必须是 5 个字段（分 时 日 月 周），当前 {len(fields)} 个: {expr!r}"
            )
        for field, (label, minimum, maximum) in zip(fields, _FIELD_RULES):
            _validate_numeric_field(
                field,
                label=label,
                minimum=minimum,
                maximum=maximum,
            )
        try:
            if not croniter.is_valid(normalized):
                raise CronExpressionError(f"cron 表达式解析失败: {normalized!r}")
            # 构造一次以确保使用与 matches/next_fires 完全相同的参数。
            croniter(normalized, datetime.now(get_timezone()), day_or=True)
        except CronExpressionError:
            raise
        except Exception as exc:
            raise CronExpressionError(
                f"cron 表达式解析失败: {normalized!r}: {exc}"
            ) from exc
        return normalized

    @staticmethod
    def matches(expr: str, value: datetime) -> bool:
        normalized = CronEngine.validate(expr)
        tz = get_timezone()
        local = value
        if local.tzinfo is None:
            local = local.replace(tzinfo=tz)
        else:
            local = local.astimezone(tz)
        local = local.replace(second=0, microsecond=0)
        try:
            return bool(croniter.match(normalized, local, day_or=True))
        except Exception as exc:
            raise CronExpressionError(
                f"cron 表达式匹配失败: {normalized!r}: {exc}"
            ) from exc

    @staticmethod
    def next_fires(
        expr: str,
        base: datetime | None = None,
        count: int = 5,
    ) -> list[datetime]:
        if count <= 0:
            return []
        normalized = CronEngine.validate(expr)
        tz = get_timezone()
        start = base if base is not None else now_naive()
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        else:
            start = start.astimezone(tz)
        try:
            iterator = croniter(
                normalized,
                start,
                ret_type=datetime,
                day_or=True,
            )
            return [
                iterator.get_next(datetime).astimezone(tz).replace(tzinfo=None)
                for _ in range(count)
            ]
        except Exception as exc:
            raise CronExpressionError(
                f"cron 表达式计算失败: {normalized!r}: {exc}"
            ) from exc
