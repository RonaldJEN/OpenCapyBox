"""测试 cron_schedule 模块：schedule → cron 表达式转换 + next_fire_at 预览。"""

from datetime import datetime

import pytest

from src.api.services.cron_schedule import (
    ScheduleError,
    next_fire_at,
    schedule_to_cron,
)


class TestScheduleToCron:
    def test_daily(self):
        assert schedule_to_cron({"kind": "daily", "time": "09:30"}) == "30 9 * * *"
        assert schedule_to_cron({"kind": "daily", "time": "00:00"}) == "0 0 * * *"
        assert schedule_to_cron({"kind": "daily", "time": "23:59"}) == "59 23 * * *"

    def test_weekdays(self):
        assert schedule_to_cron({"kind": "weekdays", "time": "08:00"}) == "0 8 * * 0-4"

    def test_weekly_sorts_and_dedups(self):
        expr = schedule_to_cron({"kind": "weekly", "time": "12:00", "days": [5, 1, 1, 3]})
        assert expr == "0 12 * * 1,3,5"

    def test_weekly_full_week(self):
        expr = schedule_to_cron({"kind": "weekly", "time": "07:15", "days": [0, 1, 2, 3, 4, 5, 6]})
        assert expr == "15 7 * * 0,1,2,3,4,5,6"

    def test_monthly(self):
        assert schedule_to_cron({"kind": "monthly", "time": "09:30", "dayOfMonth": 15}) == "30 9 15 * *"
        assert schedule_to_cron({"kind": "monthly", "time": "00:00", "dayOfMonth": 1}) == "0 0 1 * *"
        assert schedule_to_cron({"kind": "monthly", "time": "23:59", "dayOfMonth": 31}) == "59 23 31 * *"

    def test_interval_minutes(self):
        assert schedule_to_cron({"kind": "interval", "everyMinutes": 30}) == "*/30 * * * *"
        assert schedule_to_cron({"kind": "interval", "everyMinutes": 1}) == "*/1 * * * *"
        assert schedule_to_cron({"kind": "interval", "everyMinutes": 59}) == "*/59 * * * *"

    def test_interval_hours(self):
        assert schedule_to_cron({"kind": "interval", "everyHours": 2}) == "0 */2 * * *"
        assert schedule_to_cron({"kind": "interval", "everyHours": 1}) == "0 */1 * * *"
        assert schedule_to_cron({"kind": "interval", "everyHours": 23}) == "0 */23 * * *"

    # ────────── 错误用例 ──────────

    def test_not_dict(self):
        with pytest.raises(ScheduleError):
            schedule_to_cron("not-a-dict")  # type: ignore[arg-type]

    def test_unknown_kind(self):
        with pytest.raises(ScheduleError, match="未知 schedule.kind"):
            schedule_to_cron({"kind": "yearly", "time": "00:00"})

    @pytest.mark.parametrize("bad_time", ["", "abc", "24:00", "10:60", "10", "10:30:00"])
    def test_daily_bad_time(self, bad_time):
        with pytest.raises(ScheduleError):
            schedule_to_cron({"kind": "daily", "time": bad_time})

    def test_weekly_missing_days(self):
        with pytest.raises(ScheduleError, match="非空 days"):
            schedule_to_cron({"kind": "weekly", "time": "09:00"})

    def test_weekly_empty_days(self):
        with pytest.raises(ScheduleError):
            schedule_to_cron({"kind": "weekly", "time": "09:00", "days": []})

    @pytest.mark.parametrize("bad_day", [-1, 7, "1", 1.5])
    def test_weekly_bad_day(self, bad_day):
        with pytest.raises(ScheduleError):
            schedule_to_cron({"kind": "weekly", "time": "09:00", "days": [bad_day]})

    @pytest.mark.parametrize("bad_dom", [0, 32, -1, "15", None])
    def test_monthly_bad_dayofmonth(self, bad_dom):
        with pytest.raises(ScheduleError):
            schedule_to_cron({"kind": "monthly", "time": "09:00", "dayOfMonth": bad_dom})

    def test_interval_missing_both(self):
        with pytest.raises(ScheduleError, match="必须设置"):
            schedule_to_cron({"kind": "interval"})

    def test_interval_both_set(self):
        with pytest.raises(ScheduleError, match="不能同时"):
            schedule_to_cron({"kind": "interval", "everyMinutes": 30, "everyHours": 2})

    @pytest.mark.parametrize("bad_m", [0, 60, -1, "30"])
    def test_interval_bad_minutes(self, bad_m):
        with pytest.raises(ScheduleError):
            schedule_to_cron({"kind": "interval", "everyMinutes": bad_m})

    @pytest.mark.parametrize("bad_h", [0, 24, -1, "2"])
    def test_interval_bad_hours(self, bad_h):
        with pytest.raises(ScheduleError):
            schedule_to_cron({"kind": "interval", "everyHours": bad_h})


class TestNextFireAt:
    def test_daily_returns_n_fires(self):
        # 每天 09:00；从 2025-01-01 00:00:00 起，前 5 次应该是 1/1..1/5 的 09:00
        base = datetime(2025, 1, 1, 0, 0, 0)
        fires = next_fire_at("0 9 * * *", n=5, base=base)
        assert len(fires) == 5
        assert fires[0] == datetime(2025, 1, 1, 9, 0)
        assert fires[1] == datetime(2025, 1, 2, 9, 0)
        assert fires[4] == datetime(2025, 1, 5, 9, 0)

    def test_n_zero_returns_empty(self):
        assert next_fire_at("0 9 * * *", n=0) == []

    def test_default_n_is_5(self):
        base = datetime(2025, 1, 1, 0, 0, 0)
        fires = next_fire_at("0 9 * * *", base=base)
        assert len(fires) == 5

    def test_skips_current_minute(self):
        # base 正好命中触发点：应返回下一次而非当下
        base = datetime(2025, 1, 1, 9, 0, 0)
        fires = next_fire_at("0 9 * * *", n=1, base=base)
        assert fires[0] == datetime(2025, 1, 2, 9, 0)

    def test_weekly_pattern(self):
        # APScheduler day_of_week: 0=Mon..6=Sun，'1' 表示周二
        # 2025-01-01 是周三，下一个周二是 2025-01-07
        base = datetime(2025, 1, 1, 0, 0, 0)
        fires = next_fire_at("0 9 * * 1", n=2, base=base)
        assert fires[0] == datetime(2025, 1, 7, 9, 0)
        assert fires[1] == datetime(2025, 1, 14, 9, 0)

    def test_invalid_cron_raises(self):
        with pytest.raises(ScheduleError):
            next_fire_at("0 9 * *", n=1)  # 4 字段
        with pytest.raises(ScheduleError):
            next_fire_at("invalid expr here pls", n=1)

    def test_returns_naive_datetime(self):
        fires = next_fire_at("0 9 * * *", n=1, base=datetime(2025, 1, 1))
        assert fires[0].tzinfo is None
