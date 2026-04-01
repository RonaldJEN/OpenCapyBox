"""时区工具函数

统一管理项目中的时间处理，支持配置时区
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
import os


# 默认时区：台湾/亚洲时区 (UTC+8)
DEFAULT_TIMEZONE_OFFSET = 8  # 小时


def get_timezone_offset() -> int:
    """获取配置的时区偏移（小时）

    从环境变量 TIMEZONE_OFFSET 读取，默认为 8 (台湾/亚洲)
    """
    try:
        return int(os.getenv("TIMEZONE_OFFSET", str(DEFAULT_TIMEZONE_OFFSET)))
    except ValueError:
        return DEFAULT_TIMEZONE_OFFSET


def get_timezone() -> timezone:
    """获取配置的时区对象"""
    offset_hours = get_timezone_offset()
    return timezone(timedelta(hours=offset_hours))


def now() -> datetime:
    """获取当前本地时间（时区感知）

    Returns:
        当前时区的时间（timezone-aware datetime）
    """
    return datetime.now(get_timezone())


def localize(dt: Optional[datetime]) -> Optional[datetime]:
    """将 naive datetime 转换为本地时区的 aware datetime

    Args:
        dt: naive datetime 对象（无时区信息）

    Returns:
        本地时区的 aware datetime，如果输入为 None 则返回 None
    """
    if dt is None:
        return None

    if dt.tzinfo is not None:
        # 已经有时区信息，转换到本地时区
        return dt.astimezone(get_timezone())

    # 假设输入是 UTC 时间，转换到本地时区
    utc_dt = dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(get_timezone())


def to_utc(dt: datetime) -> datetime:
    """将本地时间转换为 UTC 时间

    Args:
        dt: 本地时区的 datetime

    Returns:
        UTC 时区的 datetime
    """
    if dt.tzinfo is None:
        # 假设是本地时间
        local_dt = dt.replace(tzinfo=get_timezone())
    else:
        local_dt = dt

    return local_dt.astimezone(timezone.utc)


def format_local_time(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """格式化时间为本地时区字符串

    Args:
        dt: datetime 对象
        fmt: 格式化字符串，默认为 "YYYY-MM-DD HH:MM:SS"

    Returns:
        格式化后的时间字符串
    """
    if dt is None:
        return ""

    local_dt = localize(dt)
    if local_dt is None:
        return ""

    return local_dt.strftime(fmt)


# 兼容性：提供一个替代 datetime.utcnow() 的函数
def utcnow() -> datetime:
    """获取当前 UTC 时间（已弃用，建议使用 now()）

    为了向后兼容保留，但推荐使用 now() 获取本地时间
    """
    return datetime.now(timezone.utc)


def now_naive() -> datetime:
    """获取当前本地时间（naive datetime，用于数据库存储）

    返回不带时区信息的本地时间，适合存储到 SQLAlchemy DateTime 列

    Returns:
        当前本地时间（naive datetime）
    """
    return datetime.now(get_timezone()).replace(tzinfo=None)

