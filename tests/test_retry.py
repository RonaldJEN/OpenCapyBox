"""重試機制模組測試

測試 async_retry 裝飾器和 RetryConfig 配置
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.agent.retry import RetryConfig, RetryExhaustedError, async_retry


class TestRetryConfig:
    """測試重試配置類"""

    def test_default_config(self):
        """測試默認配置"""
        config = RetryConfig()
        assert config.enabled is True
        assert config.max_retries == 3
        assert config.initial_delay == 1.0
        assert config.max_delay == 60.0
        assert config.exponential_base == 2.0
        assert config.retryable_exceptions == (Exception,)

    def test_custom_config(self):
        """測試自定義配置"""
        config = RetryConfig(
            enabled=False,
            max_retries=5,
            initial_delay=0.5,
            max_delay=30.0,
            exponential_base=3.0,
            retryable_exceptions=(ValueError, TypeError),
        )
        assert config.enabled is False
        assert config.max_retries == 5
        assert config.initial_delay == 0.5
        assert config.max_delay == 30.0
        assert config.exponential_base == 3.0
        assert config.retryable_exceptions == (ValueError, TypeError)

    def test_calculate_delay_first_attempt(self):
        """測試第一次嘗試的延遲計算"""
        config = RetryConfig(initial_delay=1.0, exponential_base=2.0)
        delay = config.calculate_delay(0)
        assert delay == 1.0  # 1.0 * 2^0 = 1.0

    def test_calculate_delay_second_attempt(self):
        """測試第二次嘗試的延遲計算"""
        config = RetryConfig(initial_delay=1.0, exponential_base=2.0)
        delay = config.calculate_delay(1)
        assert delay == 2.0  # 1.0 * 2^1 = 2.0

    def test_calculate_delay_third_attempt(self):
        """測試第三次嘗試的延遲計算"""
        config = RetryConfig(initial_delay=1.0, exponential_base=2.0)
        delay = config.calculate_delay(2)
        assert delay == 4.0  # 1.0 * 2^2 = 4.0

    def test_calculate_delay_respects_max_delay(self):
        """測試延遲計算不超過最大延遲"""
        config = RetryConfig(initial_delay=1.0, exponential_base=2.0, max_delay=5.0)
        delay = config.calculate_delay(10)  # 1.0 * 2^10 = 1024
        assert delay == 5.0  # 應該限制在 max_delay

    def test_calculate_delay_with_different_base(self):
        """測試不同底數的指數退避"""
        config = RetryConfig(initial_delay=0.5, exponential_base=3.0)
        delay = config.calculate_delay(2)
        assert delay == 4.5  # 0.5 * 3^2 = 4.5


class TestRetryExhaustedError:
    """測試重試耗盡異常"""

    def test_error_creation(self):
        """測試異常創建"""
        original_error = ValueError("Original error")
        error = RetryExhaustedError(original_error, 3)
        assert error.last_exception is original_error
        assert error.attempts == 3
        assert "3 attempts" in str(error)
        assert "Original error" in str(error)

    def test_error_message_format(self):
        """測試異常消息格式"""
        error = RetryExhaustedError(RuntimeError("API failed"), 5)
        assert "Retry failed after 5 attempts" in str(error)
        assert "API failed" in str(error)


class TestAsyncRetry:
    """測試異步重試裝飾器"""

    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        """測試成功時不重試"""
        call_count = 0

        @async_retry()
        async def successful_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = await successful_func()
        assert result == "success"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_exception(self):
        """測試異常時重試"""
        call_count = 0

        config = RetryConfig(max_retries=3, initial_delay=0.01)

        @async_retry(config)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Temporary error")
            return "success"

        result = await flaky_func()
        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        """測試重試耗盡"""
        call_count = 0

        config = RetryConfig(max_retries=2, initial_delay=0.01)

        @async_retry(config)
        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise ValueError("Always fails")

        with pytest.raises(RetryExhaustedError) as exc_info:
            await always_fails()

        assert call_count == 3  # 初始調用 + 2次重試
        assert exc_info.value.attempts == 3

    @pytest.mark.asyncio
    async def test_specific_exception_types(self):
        """測試只重試特定類型的異常"""
        config = RetryConfig(
            max_retries=3,
            initial_delay=0.01,
            retryable_exceptions=(ValueError,),
        )

        @async_retry(config)
        async def raises_type_error():
            raise TypeError("Not retryable")

        # TypeError 不在重試列表中，應該立即拋出
        with pytest.raises(TypeError):
            await raises_type_error()

    @pytest.mark.asyncio
    async def test_on_retry_callback(self):
        """測試重試回調函數"""
        retry_calls = []

        def on_retry(exc, attempt):
            retry_calls.append((str(exc), attempt))

        config = RetryConfig(max_retries=3, initial_delay=0.01)

        call_count = 0

        @async_retry(config, on_retry=on_retry)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError(f"Error {call_count}")
            return "success"

        result = await flaky_func()
        assert result == "success"
        assert len(retry_calls) == 2
        assert retry_calls[0] == ("Error 1", 1)
        assert retry_calls[1] == ("Error 2", 2)

    @pytest.mark.asyncio
    async def test_default_config_used(self):
        """測試未提供配置時使用默認配置"""
        call_count = 0

        @async_retry()  # 不傳配置
        async def func_with_defaults():
            nonlocal call_count
            call_count += 1
            return call_count

        result = await func_with_defaults()
        assert result == 1

    @pytest.mark.asyncio
    async def test_preserves_function_name(self):
        """測試保留函數名稱"""
        @async_retry()
        async def my_named_function():
            pass

        assert my_named_function.__name__ == "my_named_function"

    @pytest.mark.asyncio
    async def test_preserves_function_args(self):
        """測試保留函數參數"""
        @async_retry()
        async def func_with_args(a, b, c=None):
            return (a, b, c)

        result = await func_with_args(1, 2, c=3)
        assert result == (1, 2, 3)

    @pytest.mark.asyncio
    async def test_multiple_retryable_exceptions(self):
        """測試多種可重試異常類型"""
        config = RetryConfig(
            max_retries=5,
            initial_delay=0.01,
            retryable_exceptions=(ValueError, RuntimeError, ConnectionError),
        )

        call_count = 0
        errors = [ValueError, RuntimeError, ConnectionError]

        @async_retry(config)
        async def raises_different_errors():
            nonlocal call_count
            if call_count < len(errors):
                error_type = errors[call_count]
                call_count += 1
                raise error_type(f"Error {call_count}")
            return "success"

        result = await raises_different_errors()
        assert result == "success"
        assert call_count == 3


class TestRetryDelayBehavior:
    """測試重試延遲行為"""

    @pytest.mark.asyncio
    async def test_delay_increases_exponentially(self):
        """測試延遲指數增長"""
        delays = []
        original_sleep = asyncio.sleep

        async def mock_sleep(seconds):
            delays.append(seconds)
            # 實際不等待，加速測試

        call_count = 0
        config = RetryConfig(
            max_retries=3,
            initial_delay=1.0,
            exponential_base=2.0,
            max_delay=100.0,
        )

        @async_retry(config)
        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise ValueError("fail")

        with patch('asyncio.sleep', mock_sleep):
            with pytest.raises(RetryExhaustedError):
                await always_fails()

        # 檢查延遲序列
        assert len(delays) == 3
        assert delays[0] == 1.0   # 1.0 * 2^0
        assert delays[1] == 2.0   # 1.0 * 2^1
        assert delays[2] == 4.0   # 1.0 * 2^2
