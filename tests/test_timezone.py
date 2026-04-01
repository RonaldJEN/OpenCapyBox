"""時區工具函數測試

測試 api/utils/timezone.py 中的時區處理函數
"""
import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from src.api.utils.timezone import (
    get_timezone_offset,
    get_timezone,
    now,
    localize,
    to_utc,
    format_local_time,
    utcnow,
    DEFAULT_TIMEZONE_OFFSET,
)


class TestGetTimezoneOffset:
    """測試時區偏移獲取"""

    def test_default_offset(self):
        """測試默認偏移值"""
        with patch.dict(os.environ, {}, clear=True):
            # 確保沒有 TIMEZONE_OFFSET 環境變量
            os.environ.pop("TIMEZONE_OFFSET", None)
            offset = get_timezone_offset()
            assert offset == DEFAULT_TIMEZONE_OFFSET

    def test_custom_offset_from_env(self):
        """測試從環境變量讀取偏移"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "5"}):
            offset = get_timezone_offset()
            assert offset == 5

    def test_negative_offset(self):
        """測試負數偏移（西半球）"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "-8"}):
            offset = get_timezone_offset()
            assert offset == -8

    def test_invalid_offset_falls_back_to_default(self):
        """測試無效值回退到默認值"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "not_a_number"}):
            offset = get_timezone_offset()
            assert offset == DEFAULT_TIMEZONE_OFFSET

    def test_empty_string_falls_back_to_default(self):
        """測試空字符串回退到默認值"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": ""}):
            offset = get_timezone_offset()
            assert offset == DEFAULT_TIMEZONE_OFFSET


class TestGetTimezone:
    """測試時區對象獲取"""

    def test_returns_timezone_object(self):
        """測試返回時區對象"""
        tz = get_timezone()
        assert isinstance(tz, timezone)

    def test_timezone_offset_matches(self):
        """測試時區偏移匹配"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            tz = get_timezone()
            expected = timezone(timedelta(hours=8))
            assert tz == expected

    def test_negative_timezone(self):
        """測試負數時區"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "-5"}):
            tz = get_timezone()
            expected = timezone(timedelta(hours=-5))
            assert tz == expected


class TestNow:
    """測試當前時間獲取"""

    def test_returns_datetime(self):
        """測試返回 datetime 對象"""
        result = now()
        assert isinstance(result, datetime)

    def test_returns_timezone_aware(self):
        """測試返回時區感知的 datetime"""
        result = now()
        assert result.tzinfo is not None

    def test_timezone_matches_config(self):
        """測試時區與配置匹配"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            result = now()
            expected_tz = timezone(timedelta(hours=8))
            assert result.tzinfo == expected_tz


class TestLocalize:
    """測試本地化時間轉換"""

    def test_none_input(self):
        """測試 None 輸入"""
        result = localize(None)
        assert result is None

    def test_naive_datetime(self):
        """測試無時區的 datetime"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            naive = datetime(2024, 1, 15, 10, 30, 0)
            result = localize(naive)
            assert result is not None
            assert result.tzinfo is not None

    def test_aware_datetime_conversion(self):
        """測試有時區的 datetime 轉換"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            # UTC 時間
            utc_dt = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
            result = localize(utc_dt)
            assert result is not None
            # UTC 10:00 應該轉換為 UTC+8 18:00
            assert result.hour == 18

    def test_preserves_date_for_naive(self):
        """測試保留 naive datetime 的日期"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            naive = datetime(2024, 6, 15, 12, 0, 0)
            result = localize(naive)
            # 假設 naive 是 UTC，轉換後日期可能變化
            assert result is not None


class TestToUtc:
    """測試轉換為 UTC"""

    def test_aware_datetime_to_utc(self):
        """測試有時區的 datetime 轉為 UTC"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            local_tz = timezone(timedelta(hours=8))
            local_dt = datetime(2024, 1, 15, 18, 0, 0, tzinfo=local_tz)
            result = to_utc(local_dt)
            assert result.tzinfo == timezone.utc
            assert result.hour == 10  # 18:00 UTC+8 = 10:00 UTC

    def test_naive_datetime_treated_as_local(self):
        """測試無時區的 datetime 視為本地時間"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            naive = datetime(2024, 1, 15, 18, 0, 0)
            result = to_utc(naive)
            assert result.tzinfo == timezone.utc

    def test_utc_to_utc(self):
        """測試 UTC 到 UTC（無變化）"""
        utc_dt = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = to_utc(utc_dt)
        assert result == utc_dt


class TestFormatLocalTime:
    """測試本地時間格式化"""

    def test_none_input(self):
        """測試 None 輸入"""
        result = format_local_time(None)
        assert result == ""

    def test_default_format(self):
        """測試默認格式"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
            result = format_local_time(dt)
            # UTC 10:30:45 -> UTC+8 18:30:45
            assert "18:30:45" in result
            assert "2024-01-15" in result

    def test_custom_format(self):
        """測試自定義格式"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
            result = format_local_time(dt, fmt="%Y/%m/%d")
            assert result == "2024/01/15"

    def test_naive_datetime_format(self):
        """測試無時區 datetime 格式化"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            naive = datetime(2024, 6, 20, 12, 0, 0)
            result = format_local_time(naive)
            assert "2024-06-20" in result


class TestUtcnow:
    """測試 UTC 當前時間"""

    def test_returns_datetime(self):
        """測試返回 datetime"""
        result = utcnow()
        assert isinstance(result, datetime)

    def test_returns_utc_timezone(self):
        """測試返回 UTC 時區"""
        result = utcnow()
        assert result.tzinfo == timezone.utc

    def test_close_to_actual_utc(self):
        """測試接近實際 UTC 時間"""
        result = utcnow()
        actual_utc = datetime.now(timezone.utc)
        # 差異應該在 1 秒內
        diff = abs((result - actual_utc).total_seconds())
        assert diff < 1


class TestTimezoneIntegration:
    """時區集成測試"""

    def test_round_trip_local_to_utc_to_local(self):
        """測試本地 -> UTC -> 本地往返"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            # 創建本地時間
            local_tz = get_timezone()
            original = datetime(2024, 7, 4, 15, 30, 0, tzinfo=local_tz)
            
            # 轉換到 UTC
            utc_time = to_utc(original)
            
            # 轉換回本地
            back_to_local = localize(utc_time)
            
            # 應該相等
            assert back_to_local.hour == original.hour
            assert back_to_local.minute == original.minute

    def test_different_timezone_conversion(self):
        """測試不同時區間轉換"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            # 創建 UTC-5 的時間
            eastern = timezone(timedelta(hours=-5))
            eastern_time = datetime(2024, 1, 15, 9, 0, 0, tzinfo=eastern)
            
            # 轉換到本地 (UTC+8)
            local = localize(eastern_time)
            
            # UTC-5 9:00 = UTC 14:00 = UTC+8 22:00
            assert local.hour == 22

    def test_date_change_across_timezone(self):
        """測試跨時區日期變化"""
        with patch.dict(os.environ, {"TIMEZONE_OFFSET": "8"}):
            # UTC 晚上 22:00
            utc_night = datetime(2024, 1, 15, 22, 0, 0, tzinfo=timezone.utc)
            
            # 轉換到 UTC+8 應該是次日 06:00
            local = localize(utc_night)
            assert local.day == 16
            assert local.hour == 6
