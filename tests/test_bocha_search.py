"""博查搜索工具单元测试"""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.agent.tools.glm_search_tool import (
    GLMSearchTool,
    GLMBatchSearchTool,
    QuerySearchResult,
    _parse_bocha_response,
    _format_search_results,
    _VALID_FRESHNESS,
    BOCHA_API_URL,
)
from tests.helpers import make_mock_httpx_client


# ── 模拟 Bocha API 响应 ──────────────────────────────────

MOCK_BOCHA_RESPONSE = {
    "code": 200,
    "log_id": "test123",
    "msg": None,
    "data": {
        "_type": "SearchResponse",
        "queryContext": {"originalQuery": "Python教程"},
        "webPages": {
            "webSearchUrl": "",
            "totalEstimatedMatches": 100,
            "value": [
                {
                    "id": None,
                    "name": "Python官方教程",
                    "url": "https://docs.python.org/zh-cn/3/tutorial/",
                    "displayUrl": "https://docs.python.org/zh-cn/3/tutorial/",
                    "snippet": "Python 是一门易于学习的强大编程语言。",
                    "summary": "Python 官方教程提供了全面的入门指南，涵盖数据结构、模块、输入输出等内容。",
                    "siteName": "Python.org",
                    "siteIcon": "https://example.com/icon.png",
                    "datePublished": "2025-12-01T10:00:00+08:00",
                    "dateLastCrawled": "2025-12-01T10:00:00Z",
                    "cachedPageUrl": None,
                    "language": None,
                    "isFamilyFriendly": None,
                    "isNavigational": None,
                },
                {
                    "id": None,
                    "name": "Python快速入门",
                    "url": "https://example.com/python-quickstart",
                    "displayUrl": "https://example.com/python-quickstart",
                    "snippet": "快速学习 Python 基础知识。",
                    "summary": None,
                    "siteName": "示例网站",
                    "siteIcon": None,
                    "datePublished": None,
                    "dateLastCrawled": None,
                    "cachedPageUrl": None,
                    "language": None,
                    "isFamilyFriendly": None,
                    "isNavigational": None,
                },
            ],
            "someResultsRemoved": False,
        },
        "images": None,
        "videos": None,
    },
}

MOCK_BOCHA_ERROR_RESPONSE = {
    "code": 401,
    "message": "Invalid API KEY",
    "log_id": "err123",
}

MOCK_BOCHA_EMPTY_RESPONSE = {
    "code": 200,
    "log_id": "empty123",
    "msg": None,
    "data": {
        "_type": "SearchResponse",
        "queryContext": {"originalQuery": "xyznonexistent12345"},
        "webPages": {
            "webSearchUrl": "",
            "totalEstimatedMatches": 0,
            "value": [],
        },
        "images": None,
        "videos": None,
    },
}


def _make_ok_response(data=None):
    """创建标准成功 mock_response"""
    resp = MagicMock()
    resp.json.return_value = data or MOCK_BOCHA_RESPONSE
    resp.raise_for_status = MagicMock()
    resp.status_code = 200
    return resp


# ── _parse_bocha_response 单元测试 ──────────────────────

class TestParseBochaResponse:
    def test_parse_normal_response(self):
        results = _parse_bocha_response(MOCK_BOCHA_RESPONSE)
        assert len(results) == 2
        assert results[0]["title"] == "Python官方教程"
        assert results[0]["link"] == "https://docs.python.org/zh-cn/3/tutorial/"
        assert results[0]["source"] == "Python.org"
        # summary 优先于 snippet
        assert "官方教程" in results[0]["snippet"]
        assert results[0]["datePublished"] == "2025-12-01T10:00:00+08:00"
        assert results[0]["dateLastCrawled"] == "2025-12-01T10:00:00Z"

    def test_parse_no_summary_fallback_to_snippet(self):
        results = _parse_bocha_response(MOCK_BOCHA_RESPONSE)
        assert results[1]["snippet"] == "快速学习 Python 基础知识。"

    def test_parse_with_summary_false(self):
        """with_summary=False 时应只取 snippet，不取 summary"""
        results = _parse_bocha_response(MOCK_BOCHA_RESPONSE, with_summary=False)
        assert results[0]["snippet"] == "Python 是一门易于学习的强大编程语言。"

    def test_parse_date_fields_separate(self):
        """datePublished 和 dateLastCrawled 应分开存储，不互相回填"""
        results = _parse_bocha_response(MOCK_BOCHA_RESPONSE)
        assert results[1]["datePublished"] == ""
        assert results[1]["dateLastCrawled"] == ""

    def test_parse_empty_response(self):
        results = _parse_bocha_response(MOCK_BOCHA_EMPTY_RESPONSE)
        assert results == []

    def test_parse_missing_data_key(self):
        results = _parse_bocha_response({"code": 200})
        assert results == []


# ── freshness 验证测试 ──────────────────────────────────

class TestValidFreshness:
    def test_valid_values(self):
        assert "noLimit" in _VALID_FRESHNESS
        assert "oneDay" in _VALID_FRESHNESS
        assert "oneWeek" in _VALID_FRESHNESS
        assert "oneMonth" in _VALID_FRESHNESS
        assert "oneYear" in _VALID_FRESHNESS

    def test_invalid_values(self):
        assert "pastDay" not in _VALID_FRESHNESS
        assert "pastWeek" not in _VALID_FRESHNESS
        assert "invalid" not in _VALID_FRESHNESS


# ── GLMSearchTool 单元测试 ──────────────────────────────

class TestGLMSearchTool:
    def test_init_with_api_key(self):
        tool = GLMSearchTool(api_key="test-appcode")
        assert tool.api_key == "test-appcode"

    def test_init_missing_key_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="BOCHA_SEARCH_APPCODE"):
                GLMSearchTool(api_key=None)

    def test_name(self):
        tool = GLMSearchTool(api_key="test")
        assert tool.name == "glm_search"

    def test_parameters_schema(self):
        tool = GLMSearchTool(api_key="test")
        params = tool.parameters
        assert "query" in params["properties"]
        assert "count" in params["properties"]
        assert "freshness" in params["properties"]
        assert params["required"] == ["query"]
        assert "search_recency_filter" not in params["properties"]
        assert "search_engine" not in params["properties"]
        assert "content_size" not in params["properties"]

    def test_auth_header_uses_appcode(self):
        tool = GLMSearchTool(api_key="my-appcode")
        assert tool.api_key == "my-appcode"

    def test_api_url_is_alicloud_marketplace(self):
        assert "bocha.market.alicloudapi.com" in BOCHA_API_URL

    @pytest.mark.asyncio
    async def test_execute_sends_correct_auth_header(self):
        tool = GLMSearchTool(api_key="test-appcode-123")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            await tool.execute(query="test")

        sent_headers = mock_client.post.call_args.kwargs.get("headers", {})
        assert sent_headers["Authorization"] == "APPCODE test-appcode-123"

    @pytest.mark.asyncio
    async def test_execute_success(self):
        tool = GLMSearchTool(api_key="test-appcode")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            result = await tool.execute(query="Python教程", count=5)

        assert result.success is True
        assert "Python官方教程" in result.content
        assert "Python.org" in result.content

    @pytest.mark.asyncio
    async def test_execute_api_error(self):
        tool = GLMSearchTool(api_key="bad-key")

        async with make_mock_httpx_client(_make_ok_response(MOCK_BOCHA_ERROR_RESPONSE)) as (mock_client, _):
            result = await tool.execute(query="test")

        assert result.success is False
        assert "Bocha API error" in result.error

    @pytest.mark.asyncio
    async def test_execute_network_error(self):
        tool = GLMSearchTool(api_key="test")

        async with make_mock_httpx_client(side_effect=Exception("Connection timeout")) as (mock_client, _):
            result = await tool.execute(query="test")

        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_empty_results(self):
        tool = GLMSearchTool(api_key="test")

        async with make_mock_httpx_client(_make_ok_response(MOCK_BOCHA_EMPTY_RESPONSE)) as (mock_client, _):
            result = await tool.execute(query="xyznonexistent12345")

        assert result.success is True
        assert "No results found" in result.content

    # ── freshness 参数化测试（合并 5 个几乎相同的测试） ──

    @pytest.mark.asyncio
    @pytest.mark.parametrize("freshness_input,expected", [
        ("oneWeek", "oneWeek"),
        ("pastDay", "oneDay"),
        ("pastWeek", "oneWeek"),
        ("pastMonth", "oneMonth"),
        ("totallyInvalid", "noLimit"),
    ])
    async def test_freshness_mapping(self, freshness_input, expected):
        """测试各种 freshness 值被正确映射到 Bocha 等价值"""
        tool = GLMSearchTool(api_key="test")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            await tool.execute(query="test", freshness=freshness_input)

        sent_body = mock_client.post.call_args.kwargs.get("json", {})
        assert sent_body["freshness"] == expected, f"{freshness_input} should map to {expected}"

    @pytest.mark.asyncio
    async def test_default_freshness_is_noLimit(self):
        """不传 freshness 时默认为 noLimit"""
        tool = GLMSearchTool(api_key="test")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            await tool.execute(query="test")

        sent_body = mock_client.post.call_args.kwargs.get("json", {})
        assert sent_body["freshness"] == "noLimit"

    @pytest.mark.asyncio
    async def test_count_clamping(self):
        tool = GLMSearchTool(api_key="test")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            result = await tool.execute(query="test", count=100)

        assert result.success is True
        sent_body = mock_client.post.call_args.kwargs.get("json", {})
        assert sent_body["count"] == 50

    # ── summary 参数化测试（合并 3 个几乎相同的测试） ──

    @pytest.mark.asyncio
    @pytest.mark.parametrize("summary_input,expected", [
        ("false", False),
        ("true", True),
        (None, True),
    ])
    async def test_summary_normalization(self, summary_input, expected):
        """测试 summary 参数被正确归一化为布尔值"""
        tool = GLMSearchTool(api_key="test")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            await tool.execute(query="test", summary=summary_input)

        sent_body = mock_client.post.call_args.kwargs.get("json", {})
        assert sent_body["summary"] is expected

    def test_openai_schema(self):
        tool = GLMSearchTool(api_key="test")
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "glm_search"

    def test_to_schema(self):
        tool = GLMSearchTool(api_key="test")
        schema = tool.to_schema()
        assert schema["name"] == "glm_search"


# ── GLMBatchSearchTool 单元测试 ─────────────────────────

class TestGLMBatchSearchTool:
    def test_init_with_api_key(self):
        tool = GLMBatchSearchTool(api_key="test-appcode")
        assert tool.api_key == "test-appcode"

    def test_name(self):
        tool = GLMBatchSearchTool(api_key="test")
        assert tool.name == "glm_batch_search"

    @pytest.mark.asyncio
    async def test_execute_batch_success(self):
        tool = GLMBatchSearchTool(api_key="test")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            result = await tool.execute(queries=["Python教程", "FastAPI指南"])

        assert result.success is True
        assert "Python官方教程" in result.content

    @pytest.mark.asyncio
    async def test_execute_json_string_queries(self):
        """queries 作为 JSON 字符串传入时应被正确解析"""
        tool = GLMBatchSearchTool(api_key="test")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            result = await tool.execute(queries='["query1", "query2"]')

        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_json_bare_string_query(self):
        """json.loads('"single query"') 返回字符串而非列表，应正确包装"""
        tool = GLMBatchSearchTool(api_key="test")

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            result = await tool.execute(queries='"single query"')

        assert result.success is True
        assert mock_client.post.call_count == 1
        sent_body = mock_client.post.call_args.kwargs.get("json", {})
        assert sent_body["query"] == "single query"

    @pytest.mark.asyncio
    async def test_execute_empty_queries(self):
        tool = GLMBatchSearchTool(api_key="test")
        result = await tool.execute(queries=[])
        assert result.success is False
        assert "No queries" in result.error

    @pytest.mark.asyncio
    async def test_execute_partial_failure(self):
        tool = GLMBatchSearchTool(api_key="test")

        success_resp = _make_ok_response()
        error_resp = _make_ok_response(MOCK_BOCHA_ERROR_RESPONSE)

        async with make_mock_httpx_client() as (mock_client, _):
            mock_client.post.side_effect = [success_resp, error_resp]
            result = await tool.execute(queries=["good_query", "bad_query"])

        assert result.success is False
        assert "Some searches failed" in result.error

    @pytest.mark.asyncio
    async def test_concurrency_limit(self):
        """验证批量搜索并发不超过 5"""
        import asyncio
        from src.agent.tools.glm_search_tool import _bocha_search as real_bocha_search

        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def tracking_search(api_key, query, count, freshness, summary):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            try:
                return await real_bocha_search(api_key, query, count, freshness, summary)
            finally:
                async with lock:
                    current_concurrent -= 1

        async with make_mock_httpx_client(_make_ok_response()) as (mock_client, _):
            with patch("src.agent.tools.glm_search_tool._bocha_search", side_effect=tracking_search):
                tool = GLMBatchSearchTool(api_key="test")
                result = await tool.execute(
                    queries=[f"q{i}" for i in range(10)]
                )

        assert result.success is True
        assert max_concurrent <= 5


# ── 格式化输出测试 ──────────────────────────────────────

class TestFormatSearchResults:
    def test_format_with_publish_date(self):
        qr = QuerySearchResult(
            query="test",
            results=[{
                "title": "Test Title",
                "snippet": "Test content",
                "link": "https://example.com",
                "source": "Example",
                "datePublished": "2026-03-23T12:00:00+08:00",
                "dateLastCrawled": "",
            }],
            success=True,
        )
        output = _format_search_results([qr])
        assert "Published: 2026-03-23T12:00:00+08:00" in output
        assert "Crawled:" not in output
        assert "[1] Test Title" in output

    def test_format_with_only_crawled_date(self):
        """只有 dateLastCrawled 时应显示 Crawled: 而非 Published:"""
        qr = QuerySearchResult(
            query="test",
            results=[{
                "title": "Crawled Only",
                "snippet": "Content",
                "link": "https://example.com",
                "source": "Src",
                "datePublished": "",
                "dateLastCrawled": "2026-03-20T10:00:00Z",
            }],
            success=True,
        )
        output = _format_search_results([qr])
        assert "Published:" not in output
        assert "Crawled: 2026-03-20T10:00:00Z" in output

    def test_format_without_any_date(self):
        qr = QuerySearchResult(
            query="test",
            results=[{
                "title": "No Date",
                "snippet": "Content",
                "link": "https://example.com",
                "source": "Src",
                "datePublished": "",
                "dateLastCrawled": "",
            }],
            success=True,
        )
        output = _format_search_results([qr])
        assert "Published:" not in output
        assert "Crawled:" not in output

    def test_format_error_result(self):
        qr = QuerySearchResult(
            query="broken", results=[], success=False, error_message="timeout"
        )
        output = _format_search_results([qr])
        assert "Error: timeout" in output

    def test_format_empty_results(self):
        output = _format_search_results([])
        assert "No search results found" in output
