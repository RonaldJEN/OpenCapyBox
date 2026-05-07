"""Context 组装 + MemoryService 单元测试

覆盖：
- MemoryService CRUD（upsert, get, version）
- 乐观锁冲突检测
- 文本分块
- BM25 关键词搜索
- RRF 融合
- 时间衰减
- 分词器
- Embedding 模型注册表
- AgentService._build_memory_context
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# ── 共享工厂 ────────────────────────────────────────────────


def _make_memory_service(records=None, *, delete_return=None):
    """构建 MemoryService + mock_db，减少样板代码。

    records: 单条记录或 None — 同时配置 .first() 和 .all()
    delete_return: 配置 .delete() 的返回值（用于 index_conversation_round 测试）
    """
    from src.api.services.memory_service import MemoryService

    mock_db = MagicMock()
    if records is not None:
        mock_db.query.return_value.filter.return_value.first.return_value = records
        mock_db.query.return_value.filter.return_value.all.return_value = (
            [records] if records else []
        )
    else:
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.all.return_value = []

    if delete_return is not None:
        mock_db.query.return_value.filter.return_value.delete.return_value = delete_return

    return MemoryService(mock_db), mock_db


class TestMemoryServiceCRUD:
    """MemoryService 基本 CRUD 测试"""

    def test_get_memory_file_not_found(self):
        svc, _ = _make_memory_service(None)
        assert svc.get_memory_file("u1", "user_md") is None

    def test_get_memory_content_default(self):
        svc, _ = _make_memory_service(None)
        assert svc.get_memory_content("u1", "soul_md") == ""

    def test_get_memory_content_exists(self):
        record = MagicMock()
        record.content = "# Soul content"
        svc, _ = _make_memory_service(record)
        assert svc.get_memory_content("u1", "soul_md") == "# Soul content"

    def test_invalid_file_type(self):
        svc, _ = _make_memory_service()
        with pytest.raises(ValueError, match="无效的 file_type"):
            svc.get_memory_file("u1", "invalid_type")

    def test_upsert_creates_new(self):
        svc, mock_db = _make_memory_service(None)
        record = svc.upsert_memory_file("u1", "user_md", "# New user profile")
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    def test_upsert_updates_existing(self):
        existing = MagicMock()
        existing.version = 1
        existing.content = "old"
        svc, mock_db = _make_memory_service(existing)

        svc.upsert_memory_file("u1", "user_md", "updated")
        assert existing.content == "updated"
        assert existing.version == 2
        mock_db.commit.assert_called_once()

    def test_upsert_optimistic_lock_conflict(self):
        existing = MagicMock()
        existing.version = 3
        svc, _ = _make_memory_service(existing)

        with pytest.raises(RuntimeError, match="乐观锁冲突"):
            svc.upsert_memory_file("u1", "user_md", "new", expected_version=1)

    def test_upsert_optimistic_lock_pass(self):
        existing = MagicMock()
        existing.version = 2
        svc, mock_db = _make_memory_service(existing)

        svc.upsert_memory_file("u1", "user_md", "new", expected_version=2)
        assert existing.content == "new"
        assert existing.version == 3


class TestMemoryServiceChunking:
    """文本分块测试"""

    def test_empty_text(self):
        from src.api.services.memory_service import MemoryService

        assert MemoryService._chunk_text("") == []
        assert MemoryService._chunk_text("   ") == []

    def test_single_paragraph(self):
        from src.api.services.memory_service import MemoryService

        chunks = MemoryService._chunk_text("Hello world", chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_multiple_paragraphs_within_chunk(self):
        from src.api.services.memory_service import MemoryService

        text = "Para 1\n\nPara 2\n\nPara 3"
        chunks = MemoryService._chunk_text(text, chunk_size=1000)
        assert len(chunks) == 1  # all fit in one chunk

    def test_multiple_paragraphs_exceed_chunk(self):
        from src.api.services.memory_service import MemoryService

        text = "A" * 300 + "\n\n" + "B" * 300 + "\n\n" + "C" * 300
        chunks = MemoryService._chunk_text(text, chunk_size=400)
        assert len(chunks) >= 2


class TestMemoryServiceKeywordSearch:
    """BM25 关键词检索测试（_search_by_keyword 委托给 _search_by_bm25）"""

    def test_bm25_search_finds_matches(self):
        chunk1 = MagicMock(chunk_text="用户偏好深色模式", file_path="a.md", chunk_index=0)
        chunk2 = MagicMock(chunk_text="项目使用 React", file_path="b.md", chunk_index=0)

        svc, _ = _make_memory_service()
        # 覆盖 all() 返回值
        svc.db.query.return_value.filter.return_value.all.return_value = [chunk1, chunk2]

        results = svc._search_by_bm25("u1", "用户偏好", top_k=5)
        assert len(results) >= 1
        assert results[0]["text"] == "用户偏好深色模式"

    def test_bm25_search_empty_query(self):
        svc, _ = _make_memory_service()
        results = svc._search_by_bm25("u1", "", top_k=5)
        assert results == []

    def test_bm25_search_no_chunks(self):
        svc, _ = _make_memory_service()
        results = svc._search_by_bm25("u1", "hello", top_k=5)
        assert results == []

    def test_bm25_ranking_order(self):
        """包含更多匹配词的文档应排在前面"""
        chunk_high = MagicMock(chunk_text="Python 项目使用 React 和 Python 框架", file_path="a.md", chunk_index=0)
        chunk_low = MagicMock(chunk_text="Java 项目部署指南", file_path="b.md", chunk_index=0)

        svc, _ = _make_memory_service()
        svc.db.query.return_value.filter.return_value.all.return_value = [chunk_low, chunk_high]

        results = svc._search_by_bm25("u1", "Python 项目", top_k=5)
        assert len(results) >= 1
        assert results[0]["file_path"] == "a.md"

    def test_keyword_search_delegates_to_bm25(self):
        """_search_by_keyword 向后兼容，委托给 _search_by_bm25"""
        svc, _ = _make_memory_service()
        results = svc._search_by_keyword("u1", "test", top_k=5)
        assert results == []


class TestTokenizer:
    """分词器测试"""

    def test_english_words(self):
        from src.api.services.memory_service import MemoryService

        tokens = MemoryService._tokenize("Hello World Python3")
        assert tokens == ["hello", "world", "python3"]

    def test_chinese_characters(self):
        from src.api.services.memory_service import MemoryService

        tokens = MemoryService._tokenize("用户偏好")
        assert tokens == ["用", "户", "偏", "好"]

    def test_mixed_chinese_english(self):
        from src.api.services.memory_service import MemoryService

        tokens = MemoryService._tokenize("Python 项目使用 React")
        assert "python" in tokens
        assert "react" in tokens
        assert "项" in tokens

    def test_empty_string(self):
        from src.api.services.memory_service import MemoryService

        assert MemoryService._tokenize("") == []
        assert MemoryService._tokenize(None) == []


class TestRRFFusion:
    """RRF 融合测试"""

    def test_fusion_combines_results(self):
        from src.api.services.memory_service import MemoryService

        vec_results = [
            {"file_path": "a.md", "chunk_index": 0, "text": "text_a", "score": 0.9},
            {"file_path": "b.md", "chunk_index": 0, "text": "text_b", "score": 0.8},
        ]
        bm25_results = [
            {"file_path": "b.md", "chunk_index": 0, "text": "text_b", "score": 3.5},
            {"file_path": "c.md", "chunk_index": 0, "text": "text_c", "score": 2.0},
        ]

        merged = MemoryService._rrf_fusion(vec_results, bm25_results, top_k=5)
        # b.md 在两路都出现，应该排最前
        assert merged[0]["file_path"] == "b.md"
        assert len(merged) == 3  # a, b, c

    def test_fusion_respects_top_k(self):
        from src.api.services.memory_service import MemoryService

        vec = [{"file_path": f"v{i}.md", "chunk_index": 0, "text": f"v{i}", "score": 0.9 - i * 0.1} for i in range(5)]
        bm25 = [{"file_path": f"b{i}.md", "chunk_index": 0, "text": f"b{i}", "score": 5 - i} for i in range(5)]

        merged = MemoryService._rrf_fusion(vec, bm25, top_k=3)
        assert len(merged) == 3

    def test_fusion_empty_inputs(self):
        from src.api.services.memory_service import MemoryService

        assert MemoryService._rrf_fusion([], [], top_k=5) == []
        assert len(MemoryService._rrf_fusion([{"file_path": "a.md", "chunk_index": 0, "text": "a", "score": 1}], [], top_k=5)) == 1


class TestTimeDecay:
    """时间衰减测试"""

    def test_evergreen_files_no_decay(self):
        from src.api.services.memory_service import MemoryService

        results = [
            {"file_path": "MEMORY.md", "chunk_index": 0, "text": "important", "score": 1.0},
            {"file_path": "USER.md", "chunk_index": 0, "text": "profile", "score": 0.8},
        ]
        decayed = MemoryService._apply_time_decay(results)
        assert decayed[0]["score"] == 1.0
        assert decayed[1]["score"] == 0.8

    def test_old_dated_file_decays(self):
        from src.api.services.memory_service import MemoryService

        results = [
            {"file_path": "memory/2020-01-01.md", "chunk_index": 0, "text": "old", "score": 1.0},
        ]
        decayed = MemoryService._apply_time_decay(results, half_life_days=30.0)
        # 2020-01-01 距今超过 5 年，衰减应非常显著
        assert decayed[0]["score"] < 0.01

    def test_today_file_minimal_decay(self):
        from src.api.services.memory_service import MemoryService
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        results = [
            {"file_path": f"memory/{today}.md", "chunk_index": 0, "text": "today", "score": 1.0},
        ]
        decayed = MemoryService._apply_time_decay(results)
        # 今天的文件衰减极小
        assert decayed[0]["score"] >= 0.95

    def test_no_date_in_path_no_created_at_no_decay(self):
        from src.api.services.memory_service import MemoryService

        results = [
            {"file_path": "conversation/sess1/round1", "chunk_index": 0, "text": "chat", "score": 0.7},
        ]
        decayed = MemoryService._apply_time_decay(results)
        assert decayed[0]["score"] == 0.7

    def test_conversation_with_created_at_decays(self):
        """对话记录 file_path 无日期时，fallback 到 created_at 进行时间衰减"""
        from src.api.services.memory_service import MemoryService
        from datetime import datetime, timedelta

        old_date = datetime.now() - timedelta(days=60)
        results = [
            {"file_path": "conversation/sess1/round1", "chunk_index": 0, "text": "old chat", "score": 1.0, "created_at": old_date},
        ]
        decayed = MemoryService._apply_time_decay(results, half_life_days=30.0)
        # 60 天 = 2 个半衰期，衰减到 ~0.25
        assert decayed[0]["score"] < 0.3

    def test_recent_conversation_minimal_decay(self):
        """近期对话通过 created_at 衰减极小"""
        from src.api.services.memory_service import MemoryService
        from datetime import datetime, timedelta

        recent = datetime.now() - timedelta(days=1)
        results = [
            {"file_path": "conversation/sess1/round1", "chunk_index": 0, "text": "recent", "score": 1.0, "created_at": recent},
        ]
        decayed = MemoryService._apply_time_decay(results, half_life_days=30.0)
        assert decayed[0]["score"] >= 0.97

    def test_created_at_reorders_old_vs_new_conversations(self):
        """旧对话通过 created_at 衰减后排名下降，新对话上升"""
        from src.api.services.memory_service import MemoryService
        from datetime import datetime, timedelta

        old_date = datetime.now() - timedelta(days=90)
        recent = datetime.now() - timedelta(days=2)
        results = [
            {"file_path": "conversation/sess-old/round1", "chunk_index": 0, "text": "old", "score": 1.0, "created_at": old_date},
            {"file_path": "conversation/sess-new/round2", "chunk_index": 0, "text": "new", "score": 0.5, "created_at": recent},
        ]
        decayed = MemoryService._apply_time_decay(results, half_life_days=30.0)
        # 新对话 score≈0.48 应排在旧对话 score≈0.12 前面
        assert decayed[0]["file_path"] == "conversation/sess-new/round2"

    def test_empty_results(self):
        from src.api.services.memory_service import MemoryService

        assert MemoryService._apply_time_decay([]) == []

    def test_decay_reorders_results(self):
        """旧文件衰减后排名应下降"""
        from src.api.services.memory_service import MemoryService
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        results = [
            {"file_path": "memory/2020-01-01.md", "chunk_index": 0, "text": "old", "score": 1.0},
            {"file_path": f"memory/{today}.md", "chunk_index": 0, "text": "new", "score": 0.5},
        ]
        decayed = MemoryService._apply_time_decay(results)
        # 今天 score=0.5 应排在 2020 score≈0 前面
        assert decayed[0]["file_path"] == f"memory/{today}.md"


class TestHybridSearchIntegration:
    """search_memory 混合检索集成测试"""

    @pytest.mark.asyncio
    async def test_search_pure_bm25_when_no_embedding(self):
        """无 Embedding 配置时降级为纯 BM25"""
        from src.api.services.memory_service import MemoryService

        chunk1 = MagicMock(chunk_text="Python 深度学习框架", file_path="a.md", chunk_index=0)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [chunk1]
        svc = MemoryService(mock_db)

        with patch.object(MemoryService, "_is_embedding_available", return_value=False):
            results = await svc.search_memory("u1", "Python", top_k=5)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_hybrid_when_embedding_available(self):
        """Embedding 可用时执行混合检索"""
        from src.api.services.memory_service import MemoryService

        chunk1 = MagicMock(
            chunk_text="机器学习模型训练",
            file_path="MEMORY.md", chunk_index=0,
            embedding='[0.1, 0.2, 0.3]',
        )
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [chunk1]
        svc = MemoryService(mock_db)

        with patch.object(MemoryService, "_is_embedding_available", return_value=True), \
             patch.object(MemoryService, "_generate_embeddings", new_callable=AsyncMock, return_value=[[0.1, 0.2, 0.3]]):
            results = await svc.search_memory("u1", "机器学习", top_k=5)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_fallback_on_embedding_failure(self):
        """向量检索失败时降级为纯 BM25"""
        from src.api.services.memory_service import MemoryService

        chunk1 = MagicMock(chunk_text="React 前端组件", file_path="b.md", chunk_index=0)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [chunk1]
        svc = MemoryService(mock_db)

        with patch.object(MemoryService, "_is_embedding_available", return_value=True), \
             patch.object(MemoryService, "_search_by_embedding", new_callable=AsyncMock, side_effect=Exception("API down")):
            results = await svc.search_memory("u1", "React", top_k=5)
        assert len(results) >= 1


class TestSearchMemoryDateRange:
    """search_memory 时间范围过滤测试"""

    def test_parse_date_range_valid(self):
        from src.api.services.memory_service import MemoryService

        dt_start, dt_end = MemoryService._parse_date_range("2026-04-20", "2026-04-26")
        assert dt_start.year == 2026 and dt_start.month == 4 and dt_start.day == 20
        assert dt_end.hour == 23 and dt_end.minute == 59 and dt_end.second == 59
        assert dt_end.day == 26

    def test_parse_date_range_none(self):
        from src.api.services.memory_service import MemoryService

        dt_start, dt_end = MemoryService._parse_date_range(None, None)
        assert dt_start is None
        assert dt_end is None

    def test_parse_date_range_invalid_format(self):
        from src.api.services.memory_service import MemoryService

        dt_start, dt_end = MemoryService._parse_date_range("not-a-date", "2026-04-26")
        assert dt_start is None
        assert dt_end is not None

    def test_bm25_with_date_filter(self):
        """BM25 搜索应传递时间过滤条件到 query filter"""
        from src.api.services.memory_service import MemoryService
        from datetime import datetime

        chunk = MagicMock(chunk_text="Python 深度学习", file_path="a.md", chunk_index=0)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [chunk]
        svc = MemoryService(mock_db)

        dt_start = datetime(2026, 4, 20)
        dt_end = datetime(2026, 4, 26, 23, 59, 59)
        results = svc._search_by_bm25("u1", "Python", top_k=5, dt_start=dt_start, dt_end=dt_end)

        # filter 应被调用（带时间条件）
        mock_db.query.return_value.filter.assert_called()
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_memory_passes_dates_to_bm25(self):
        """search_memory 将 start_date/end_date 传递到 BM25"""
        from src.api.services.memory_service import MemoryService

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        svc = MemoryService(mock_db)

        with patch.object(MemoryService, "_is_embedding_available", return_value=False), \
             patch.object(svc, "_search_by_bm25", return_value=[]) as mock_bm25:
            await svc.search_memory("u1", "test", top_k=5, start_date="2026-04-20", end_date="2026-04-26")

        _, kwargs = mock_bm25.call_args
        assert kwargs["dt_start"].day == 20
        assert kwargs["dt_end"].day == 26

    @pytest.mark.asyncio
    async def test_search_memory_passes_dates_to_embedding(self):
        """search_memory 将 start_date/end_date 传递到向量检索"""
        from src.api.services.memory_service import MemoryService

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        svc = MemoryService(mock_db)

        with patch.object(MemoryService, "_is_embedding_available", return_value=True), \
             patch.object(svc, "_search_by_embedding", new_callable=AsyncMock, return_value=[]) as mock_emb, \
             patch.object(svc, "_search_by_bm25", return_value=[]):
            await svc.search_memory("u1", "test", top_k=5, start_date="2026-04-20", end_date="2026-04-26")

        _, kwargs = mock_emb.call_args
        assert kwargs["dt_start"].day == 20
        assert kwargs["dt_end"].day == 26

    def test_bm25_excludes_evergreen_files_with_date_range(self):
        """指定日期范围时，BM25 filter 应排除 MEMORY.md/USER.md 等常青文件"""
        from src.api.services.memory_service import MemoryService
        from src.api.models.user_memory import MemoryEmbedding
        from datetime import datetime

        mock_db = MagicMock()
        # 模拟两个 chunk: 一个是 MEMORY.md, 一个是对话
        chunk_memory = MagicMock(chunk_text="记忆内容", file_path="MEMORY.md", chunk_index=0)
        chunk_convo = MagicMock(chunk_text="对话内容讨论", file_path="conversation/s1/r1", chunk_index=0)
        mock_db.query.return_value.filter.return_value.all.return_value = [chunk_convo]
        svc = MemoryService(mock_db)

        dt_start = datetime(2026, 4, 20)
        dt_end = datetime(2026, 4, 26, 23, 59, 59)
        results = svc._search_by_bm25("u1", "对话内容", top_k=5, dt_start=dt_start, dt_end=dt_end)

        # 验证 filter 被调用时包含了 notlike 条件（排除常青文件）
        filter_call = mock_db.query.return_value.filter
        filter_args = filter_call.call_args[0]
        # 至少有 user_id + created_at >= + created_at <= + 4 notlike = 7 个条件
        assert len(filter_args) >= 7

    def test_bm25_no_exclusion_without_date_range(self):
        """不指定日期范围时，不排除常青文件"""
        from src.api.services.memory_service import MemoryService
        from datetime import datetime

        mock_db = MagicMock()
        chunk = MagicMock(chunk_text="记忆", file_path="MEMORY.md", chunk_index=0)
        mock_db.query.return_value.filter.return_value.all.return_value = [chunk]
        svc = MemoryService(mock_db)

        svc._search_by_bm25("u1", "记忆", top_k=5)

        filter_call = mock_db.query.return_value.filter
        filter_args = filter_call.call_args[0]
        # 只有 user_id 一个条件
        assert len(filter_args) == 1

    def test_bm25_date_filter_keeps_null_file_path_records(self):
        """指定日期范围时，file_path 为空的对话索引仍应保留。"""
        from datetime import datetime
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from src.api.models.user_memory import MemoryEmbedding
        from src.api.services.memory_service import MemoryService

        engine = create_engine("sqlite:///:memory:")
        MemoryEmbedding.__table__.create(bind=engine)
        TestingSessionLocal = sessionmaker(bind=engine)
        db = TestingSessionLocal()
        try:
            db.add_all([
                MemoryEmbedding(
                    user_id="u1",
                    file_path=None,
                    chunk_index=0,
                    chunk_text="Python 对话内容",
                    created_at=datetime(2026, 4, 22),
                ),
                MemoryEmbedding(
                    user_id="u1",
                    file_path="MEMORY.md",
                    chunk_index=0,
                    chunk_text="Python 常青内容",
                    created_at=datetime(2026, 4, 22),
                ),
            ])
            db.commit()

            svc = MemoryService(db)
            results = svc._search_by_bm25(
                "u1",
                "Python",
                top_k=5,
                dt_start=datetime(2026, 4, 20),
                dt_end=datetime(2026, 4, 26, 23, 59, 59),
            )

            assert [r["file_path"] for r in results] == [None]
        finally:
            db.close()
            engine.dispose()


class TestSearchMemoryToolDateParams:
    """SearchMemoryTool 时间参数透传测试"""

    @pytest.mark.asyncio
    async def test_tool_passes_dates_to_service(self):
        from src.agent.tools.memory_tools import SearchMemoryTool
        from src.api.services.memory_service import MemoryService

        mock_db = MagicMock()
        factory = MagicMock(return_value=mock_db)
        tool = SearchMemoryTool(db_session_factory=factory, user_id="u1")

        with patch.object(MemoryService, "search_memory", new_callable=AsyncMock, return_value=[]) as mock_search:
            await tool.execute(query="test", start_date="2026-04-20", end_date="2026-04-26")

        mock_search.assert_called_once_with("u1", "test", 5, start_date="2026-04-20", end_date="2026-04-26")

    def test_tool_parameters_include_dates(self):
        from src.agent.tools.memory_tools import SearchMemoryTool

        tool = SearchMemoryTool(db_session_factory=MagicMock, user_id="u1")
        props = tool.parameters["properties"]
        assert "start_date" in props
        assert "end_date" in props
        assert "start_date" not in tool.parameters.get("required", [])


class TestEmbeddingVectorNormalization:
    """Embedding 向量维度归一测试"""

    def test_short_embedding_is_padded_to_storage_dimensions(self):
        from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS, normalize_embedding_vector

        vector = normalize_embedding_vector([0.1, -0.2, 0.3])

        assert len(vector) == MEMORY_EMBEDDING_DIMENSIONS
        assert vector[:3] == [0.1, -0.2, 0.3]
        assert vector[3:] == [0.0] * (MEMORY_EMBEDDING_DIMENSIONS - 3)

    def test_pgvector_literal_is_parsed_and_padded(self):
        from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS, normalize_embedding_vector

        vector = normalize_embedding_vector("[0.1,-0.2]")

        assert len(vector) == MEMORY_EMBEDDING_DIMENSIONS
        assert vector[:2] == [0.1, -0.2]

    @pytest.mark.asyncio
    async def test_sqlite_embedding_search_normalizes_legacy_short_vectors(self):
        from src.api.services.memory_service import MemoryService

        chunk = MagicMock()
        chunk.embedding = [0.1, 0.2]
        chunk.file_path = "conversation/s1/r1"
        chunk.chunk_index = 0
        chunk.chunk_text = "legacy short vector"
        chunk.created_at = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [chunk]
        svc = MemoryService(mock_db)

        with patch.object(svc, "_generate_embeddings", new_callable=AsyncMock, return_value=[[0.1, 0.2]]):
            results = await svc._search_by_embedding("u1", "query", top_k=1)

        assert results[0]["score"] == 1.0

    def test_pgvector_search_builds_bound_vector_query(self):
        from sqlalchemy.dialects import postgresql

        from src.api.models.user_memory import MemoryEmbedding
        from src.api.services.memory_service import MemoryService
        from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS

        item = MagicMock()
        item.file_path = "conversation/s1/r1"
        item.chunk_index = 0
        item.chunk_text = "pgvector result"
        item.created_at = None

        captured = {}

        class FakeQuery:
            def __init__(self, *entities):
                captured["entities"] = entities

            def filter(self, *conditions):
                captured["filters"] = conditions
                return self

            def order_by(self, expression):
                captured["order_by"] = expression
                return self

            def limit(self, value):
                captured["limit"] = value
                return self

            def all(self):
                return [(item, 0.75)]

        mock_db = MagicMock()
        mock_db.query.side_effect = lambda *entities: FakeQuery(*entities)
        svc = MemoryService(mock_db)

        results = svc._search_by_pgvector(
            [0.1, 0.2],
            [MemoryEmbedding.user_id == "u1", MemoryEmbedding.embedding.isnot(None)],
            top_k=3,
        )

        compiled_order = str(captured["order_by"].compile(dialect=postgresql.dialect()))
        assert "<=>" in compiled_order
        assert f"vector({MEMORY_EMBEDDING_DIMENSIONS})" in compiled_order
        assert captured["limit"] == 3
        assert results == [
            {
                "file_path": "conversation/s1/r1",
                "chunk_index": 0,
                "text": "pgvector result",
                "score": 0.75,
                "created_at": None,
            }
        ]


class TestEmbeddingModelRegistry:
    """Embedding 模型注册表测试"""

    def test_load_embedding_models_from_yaml(self):
        from src.api.model_registry import EmbeddingModelConfig, ModelRegistry

        # 模拟 YAML 加载后的 raw dict
        raw = {
            "default_model": "",
            "models": {},
            "embedding_models": {
                "text-embedding-v4": {
                    "display_name": "test emb",
                    "api_base": "https://example.com/v1",
                    "api_key": "test-key",
                    "model_name": "text-embedding-v4",
                    "dimensions": 1024,
                    "enabled": True,
                },
            },
            "default_embedding_model": "text-embedding-v4",
        }

        registry = ModelRegistry(
            models={},
            default_model_id="",
            embedding_models={
                "text-embedding-v4": EmbeddingModelConfig(
                    id="text-embedding-v4",
                    display_name="test emb",
                    api_base="https://example.com/v1",
                    api_key="test-key",
                    model_name="text-embedding-v4",
                    dimensions=1024,
                )
            },
            default_embedding_model_id="text-embedding-v4",
        )

        emb = registry.get_embedding_model()
        assert emb is not None
        assert emb.id == "text-embedding-v4"
        assert emb.dimensions == 1024

    def test_get_embedding_model_returns_none_when_empty(self):
        from src.api.model_registry import ModelRegistry

        registry = ModelRegistry(models={}, default_model_id="")
        assert registry.get_embedding_model() is None

    def test_get_embedding_model_disabled(self):
        from src.api.model_registry import EmbeddingModelConfig, ModelRegistry

        registry = ModelRegistry(
            models={}, default_model_id="",
            embedding_models={
                "emb1": EmbeddingModelConfig(
                    id="emb1", display_name="e", api_base="http://x", api_key="k",
                    model_name="m", enabled=False,
                )
            },
            default_embedding_model_id="emb1",
        )
        assert registry.get_embedding_model() is None

    def test_resolve_api_key(self):
        from src.api.model_registry import EmbeddingModelConfig

        emb = EmbeddingModelConfig(
            id="test", display_name="t", api_base="http://x",
            api_key="literal-key", model_name="m",
        )
        assert emb.resolve_api_key() == "literal-key"

    def test_resolve_api_key_env_var(self):
        import os
        from src.api.model_registry import EmbeddingModelConfig

        os.environ["_TEST_EMB_KEY"] = "secret123"
        try:
            emb = EmbeddingModelConfig(
                id="test", display_name="t", api_base="http://x",
                api_key="${_TEST_EMB_KEY}", model_name="m",
            )
            assert emb.resolve_api_key() == "secret123"
        finally:
            del os.environ["_TEST_EMB_KEY"]


class TestContextAssembly:
    """AgentService._build_memory_context 测试"""

    def test_no_memory_returns_empty(self):
        from src.api.services.agent_service import AgentService
        from src.api.services.memory_service import MemoryService

        svc = MagicMock(spec=AgentService)
        svc.user_id = "u1"
        svc.history_service = MagicMock()
        svc.history_service.db = MagicMock()

        with patch.object(MemoryService, "get_all_memory_files", return_value={}):
            result = AgentService._build_memory_context(svc)
        assert result == ""

    def test_with_soul_and_user(self):
        from src.api.services.agent_service import AgentService
        from src.api.services.memory_service import MemoryService

        svc = MagicMock(spec=AgentService)
        svc.user_id = "u1"
        svc.history_service = MagicMock()
        svc.history_service.db = MagicMock()
        svc._token_limit = 80000

        files = {
            "soul_md": "你是一个友善的助手",
            "user_md": "Alice, 软件工程师",
        }
        with patch.object(MemoryService, "get_all_memory_files", return_value=files):
            with patch.object(MemoryService, "get_memory_file", return_value=None):
                result = AgentService._build_memory_context(svc)

        assert "Agent 人格" in result
        assert "友善的助手" in result
        assert "用户画像" in result
        assert "Alice" in result

    def test_without_soul_only_user_and_memory(self):
        """无 soul_md 时仍正确注入 user_md 和 memory_md"""
        from src.api.services.agent_service import AgentService
        from src.api.services.memory_service import MemoryService

        svc = MagicMock(spec=AgentService)
        svc.user_id = "u1"
        svc.history_service = MagicMock()
        svc.history_service.db = MagicMock()
        svc._token_limit = 80000
        # 绑定真实的 _truncate_to_tokens 以避免 mock 返回值
        svc._truncate_to_tokens = AgentService._truncate_to_tokens

        files = {
            "user_md": "Bob, 后端工程师，偏好深色模式",
            "memory_md": "# 项目笔记\n用户在开发一个 AI 聊天应用",
        }
        with patch.object(MemoryService, "get_all_memory_files", return_value=files):
            result = AgentService._build_memory_context(svc)

        assert "Agent 人格" not in result  # 无 soul_md
        assert "用户画像" in result
        assert "Bob" in result
        assert "长期记忆" in result
        assert "AI 聊天应用" in result


class TestTruncateToTokens:
    """_truncate_to_tokens 测试"""

    def test_short_text_not_truncated(self):
        from src.api.services.agent_service import AgentService

        result = AgentService._truncate_to_tokens("hello world", 100, lambda t: len(t))
        assert result == "hello world"

    def test_long_text_truncated(self):
        from src.api.services.agent_service import AgentService

        text = "A" * 1000
        result = AgentService._truncate_to_tokens(text, 100, lambda t: len(t))
        assert len(result) <= 120  # 100 + "...(truncated)"
        assert result.endswith("...(truncated)")


class TestIndexConversationRound:
    """MemoryService.index_conversation_round 测试"""

    @pytest.mark.asyncio
    async def test_index_empty_messages_returns_zero(self):
        svc, _ = _make_memory_service(delete_return=0)
        count = await svc.index_conversation_round("u1", "s1", "r1", "", "")
        assert count == 0

    @pytest.mark.asyncio
    async def test_index_user_message_only(self):
        svc, mock_db = _make_memory_service(delete_return=0)

        with patch.object(svc, "_generate_embeddings", new_callable=AsyncMock, return_value=[None]):
            count = await svc.index_conversation_round(
                "u1", "s1", "r1", "帮我分析大位科技", ""
            )

        assert count >= 1
        mock_db.add.assert_called()
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_index_full_conversation(self):
        svc, mock_db = _make_memory_service(delete_return=0)

        user_msg = "帮我分析大位科技的偿债能力"
        assistant_resp = "大位科技的流动比率为0.93，短期偿债能力较弱"

        with patch.object(svc, "_generate_embeddings", new_callable=AsyncMock, return_value=[None]):
            count = await svc.index_conversation_round(
                "u1", "session-123", "round-456", user_msg, assistant_resp
            )

        assert count >= 1
        # 验证 file_path 包含 conversation 前缀
        added_records = [call.args[0] for call in mock_db.add.call_args_list]
        from src.api.models.user_memory import MemoryEmbedding
        for record in added_records:
            if isinstance(record, MemoryEmbedding):
                assert record.file_path == "conversation/session-123/round-456"
                assert record.user_id == "u1"

    @pytest.mark.asyncio
    async def test_index_stores_embedding_as_padded_float_list(self):
        """向量字段写入 2048 维 Python list，不再 json.dumps 成字符串。"""
        from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS

        svc, mock_db = _make_memory_service(delete_return=0)

        with patch.object(svc, "_generate_embeddings", new_callable=AsyncMock, return_value=[[0.1, 0.2, 0.3]]):
            await svc.index_conversation_round("u1", "s1", "r1", "msg", "resp")

        record = mock_db.add.call_args.args[0]
        assert len(record.embedding) == MEMORY_EMBEDDING_DIMENSIONS
        assert record.embedding[:3] == [0.1, 0.2, 0.3]
        assert record.embedding[3:] == [0.0] * (MEMORY_EMBEDDING_DIMENSIONS - 3)
        assert not isinstance(record.embedding, str)

    @pytest.mark.asyncio
    async def test_index_idempotent_deletes_old(self):
        """重复索引同一轮次应先删除旧数据"""
        svc, mock_db = _make_memory_service(delete_return=0)

        with patch.object(svc, "_generate_embeddings", new_callable=AsyncMock, return_value=[None]):
            await svc.index_conversation_round("u1", "s1", "r1", "msg", "resp")

        # 验证 delete 被调用（幂等清理旧索引）
        mock_db.query.return_value.filter.return_value.delete.assert_called()

    @pytest.mark.asyncio
    async def test_indexed_content_searchable_by_keyword(self):
        """验证索引后的对话内容可以被关键词搜索命中"""
        from src.api.services.memory_service import MemoryService

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.delete.return_value = 0
        svc = MemoryService(mock_db)

        # 模拟索引后的 chunk 数据
        chunk = MagicMock(
            chunk_text="用户: 帮我分析大位科技\n\n助手: 大位科技的流动比率为0.93",
            file_path="conversation/s1/r1",
            chunk_index=0,
        )
        mock_db.query.return_value.filter.return_value.all.return_value = [chunk]

        results = svc._search_by_keyword("u1", "大位科技 流动比率", top_k=5)
        assert len(results) >= 1
        assert "大位科技" in results[0]["text"]
