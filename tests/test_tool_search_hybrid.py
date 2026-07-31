from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.agent.tools.tool_discovery import ToolSearchDocument
from src.api.services.embedding_service import (
    EmbeddingRequestConfig,
    generate_embeddings,
)
from src.api.services.mcp_tool_search_service import (
    McpToolSearchService,
    _prepare_candidate,
    _rrf_ranking,
    _tokenize,
)


def _document(
    model_name: str,
    description: str,
    *,
    title: str = "",
) -> ToolSearchDocument:
    return ToolSearchDocument(
        model_name=model_name,
        provider="mcp",
        tool_name=model_name,
        installation_id="installation-1",
        server_name="market-data",
        server_description="financial data",
        title=title,
        description=description,
        schema_hash=f"schema-{model_name}",
        connection_fingerprint="connection-1",
    )


class FakeRepository:
    def __init__(self, semantic_names=None, claims=None, *, complete=True):
        self.semantic_names = list(semantic_names or [])
        self.claims = list(claims or [])
        self.complete = complete
        self.claim_calls = 0
        self.vector_calls = 0
        self.finalized = []

    def claim_missing(self, candidates, *, model_fingerprint):
        self.claim_calls += 1
        return list(self.claims) if self.claim_calls == 1 else []

    def finalize_claims(self, claims, vectors, *, model_fingerprint):
        self.finalized.append((claims, vectors, model_fingerprint))

    def vector_ranking(
        self,
        query_vector,
        candidates,
        *,
        model_fingerprint,
        min_score,
    ):
        self.vector_calls += 1
        return list(self.semantic_names)

    def index_complete(self, candidates, *, model_fingerprint):
        return self.complete


def _embedding_config() -> EmbeddingRequestConfig:
    return EmbeddingRequestConfig(
        identity="embedding-fingerprint",
        api_key="secret",
        api_base="https://embedding.example/v1",
        model_name="embedding-model",
        dimensions=3,
    )


@pytest.mark.asyncio
async def test_hybrid_search_uses_lexical_only_without_embedding_config():
    repository = FakeRepository(["semantic-only"])
    provider = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    service = McpToolSearchService(
        repository,
        embedding_provider=provider,
        config_provider=lambda: None,
    )

    ranked = await service.rank(
        "stock realtime",
        [
            _document("history", "stock history"),
            _document("realtime", "stock realtime quotes"),
        ],
        limit=2,
    )

    assert ranked == ["realtime", "history"]
    provider.assert_not_awaited()
    assert repository.claim_calls == 0
    assert repository.vector_calls == 0


@pytest.mark.asyncio
async def test_hybrid_search_can_return_semantic_only_synonym():
    repository = FakeRepository(["realtime_quotes"])
    provider = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    service = McpToolSearchService(
        repository,
        embedding_provider=provider,
        config_provider=_embedding_config,
    )

    ranked = await service.rank(
        "current share price",
        [_document("realtime_quotes", "intraday market snapshot")],
        limit=1,
    )

    assert ranked == ["realtime_quotes"]
    provider.assert_awaited_once()
    assert repository.vector_calls == 1


@pytest.mark.asyncio
async def test_hybrid_search_rrf_rewards_overlap_between_rankings():
    repository = FakeRepository(["b", "c"])
    provider = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    service = McpToolSearchService(
        repository,
        embedding_provider=provider,
        config_provider=_embedding_config,
    )

    ranked = await service.rank(
        "alpha beta",
        [
            _document("a", "alpha beta"),
            _document("b", "alpha"),
            _document("c", "unrelated"),
        ],
        limit=3,
    )

    assert ranked[0] == "b"
    assert set(ranked) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_hybrid_search_embedding_failure_falls_back_to_lexical():
    repository = FakeRepository(["semantic-only"])
    provider = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
    service = McpToolSearchService(
        repository,
        embedding_provider=provider,
        config_provider=_embedding_config,
    )

    ranked = await service.rank(
        "stock realtime",
        [
            _document("history", "stock history"),
            _document("realtime", "stock realtime quotes"),
        ],
        limit=2,
    )

    assert ranked == ["realtime", "history"]


@pytest.mark.asyncio
async def test_hybrid_search_vector_repository_failure_falls_back_to_lexical():
    repository = FakeRepository(["semantic-only"])
    repository.vector_ranking = Mock(side_effect=RuntimeError("pgvector unavailable"))
    service = McpToolSearchService(
        repository,
        embedding_provider=AsyncMock(return_value=[[1.0, 0.0, 0.0]]),
        config_provider=_embedding_config,
    )

    ranked = await service.rank(
        "stock realtime",
        [
            _document("history", "stock history"),
            _document("realtime", "stock realtime quotes"),
        ],
        limit=2,
    )

    assert ranked == ["realtime", "history"]


@pytest.mark.asyncio
async def test_hybrid_search_discards_unknown_vector_result():
    repository = FakeRepository(["foreign-hidden-tool", "visible"])
    provider = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    service = McpToolSearchService(
        repository,
        embedding_provider=provider,
        config_provider=_embedding_config,
    )

    ranked = await service.rank(
        "semantic only query",
        [_document("visible", "different capability wording")],
        limit=2,
    )

    assert ranked == ["visible"]


@pytest.mark.asyncio
async def test_registration_warmup_batches_tool_index_documents():
    documents = [
        _document(f"tool-{index}", f"capability {index}")
        for index in range(130)
    ]
    claims = [
        SimpleNamespace(
            candidate=_prepare_candidate(document),
            claim_token=f"claim-{index}",
        )
        for index, document in enumerate(documents)
    ]
    repository = FakeRepository(["tool-129"], claims=claims)

    async def embed(texts, _config):
        return [[1.0, 0.0, 0.0] for _text in texts]

    provider = AsyncMock(side_effect=embed)
    service = McpToolSearchService(
        repository,
        embedding_provider=provider,
        config_provider=_embedding_config,
    )

    await service.warm_candidates(documents)

    request_sizes = [len(call.args[0]) for call in provider.await_args_list]
    assert sorted(request_sizes) == [2] + ([8] * 16)
    assert len(repository.finalized) == 1
    assert len(repository.finalized[0][0]) == 130
    assert len(repository.finalized[0][1]) == 130


@pytest.mark.asyncio
async def test_incomplete_tool_index_uses_lexical_without_query_embedding():
    repository = FakeRepository(["semantic-only"], complete=False)
    provider = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    service = McpToolSearchService(
        repository,
        embedding_provider=provider,
        config_provider=_embedding_config,
    )

    ranked = await service.rank(
        "stock realtime",
        [
            _document("history", "stock history"),
            _document("realtime", "stock realtime quotes"),
        ],
        limit=2,
    )

    assert ranked == ["realtime", "history"]
    provider.assert_not_awaited()
    assert repository.vector_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generated",
    [
        [None],
        [[0.0, 0.0, 0.0]],
        [[float("nan"), 0.0, 0.0]],
        [],
    ],
)
async def test_hybrid_search_invalid_query_vectors_fall_back_to_lexical(generated):
    repository = FakeRepository(["semantic-only"])
    service = McpToolSearchService(
        repository,
        embedding_provider=AsyncMock(return_value=generated),
        config_provider=_embedding_config,
    )

    ranked = await service.rank(
        "stock realtime",
        [
            _document("history", "stock history"),
            _document("realtime", "stock realtime quotes"),
        ],
        limit=2,
    )

    assert ranked == ["realtime", "history"]


def test_chinese_sparse_tokenizer_uses_jieba_search_terms():
    tokens = _tokenize("调研上市公司的员工持股计划")

    assert "上市公司" in tokens
    assert "员工" in tokens
    assert "持股" in tokens
    assert "计划" in tokens
    assert "公" not in tokens


def test_rrf_sparse_signal_boosts_dense_candidate_into_top_k():
    sparse = ["overlap", "sparse-2", "sparse-3", "sparse-4", "sparse-5"]
    dense = [
        "dense-1",
        "dense-2",
        "dense-3",
        "dense-4",
        "dense-5",
        "overlap",
    ]

    ranked = _rrf_ranking(sparse, dense, limit=5)

    assert "overlap" in ranked
    assert "dense-5" not in ranked


@pytest.mark.asyncio
async def test_generic_sparse_search_does_not_require_domain_rules():
    service = McpToolSearchService(
        FakeRepository(),
        config_provider=lambda: None,
    )
    document = ToolSearchDocument(
        model_name="docs-search",
        provider="mcp",
        tool_name="docs-search",
        installation_id="docs-installation",
        server_name="Acme Knowledge",
        server_description="企业知识库",
        title="文档检索",
        description="搜索内部文档、制度和项目资料",
        schema_hash="schema-docs-search",
        connection_fingerprint="docs-connection",
    )

    ranked = await service.rank("在 Acme Knowledge 里搜索项目资料", [document], limit=5)

    assert ranked == ["docs-search"]


@pytest.mark.asyncio
async def test_embedding_response_is_reordered_by_input_index():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            return FakeResponse()

    with patch("httpx.AsyncClient", FakeAsyncClient):
        embeddings = await generate_embeddings(
            ["query", "document"],
            config=_embedding_config(),
        )

    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.asyncio
async def test_embedding_response_rejects_duplicate_indexes():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 0, "embedding": [0.0, 1.0]},
                ]
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            return FakeResponse()

    with patch("httpx.AsyncClient", FakeAsyncClient):
        embeddings = await generate_embeddings(
            ["query", "document"],
            config=_embedding_config(),
        )

    assert embeddings == [None, None]
