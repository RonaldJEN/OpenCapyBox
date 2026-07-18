"""Candidate-scoped hybrid retrieval for deferred MCP tools."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import uuid
import weakref
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable

from sqlalchemy import bindparam, cast, or_, tuple_
from sqlalchemy.orm import load_only

from src.agent.tools.tool_discovery import (
    MAX_TOOL_SEARCH_DESCRIPTION_BYTES,
    ToolSearchDocument,
    bound_tool_search_text,
)
from src.api.models.mcp import McpToolSearchIndex
from src.api.services.embedding_service import (
    EmbeddingRequestConfig,
    generate_embeddings,
    resolve_embedding_request_config,
)
from src.api.utils.embedding_vector import (
    normalize_embedding_vector,
    serialize_pgvector,
)
from src.api.utils.timezone import now_naive


logger = logging.getLogger(__name__)

_SEARCH_DOCUMENT_VERSION = "mcp-tool-search-doc-v1"
_MAX_SERVER_NAME_BYTES = 255
_MAX_SERVER_DESCRIPTION_BYTES = 1024
_MAX_TOOL_NAME_BYTES = 255
_MAX_TOOL_TITLE_BYTES = 512
_RRF_K = 60
_SEMANTIC_MIN_SCORE = 0.25
_INDEX_LEASE_SECONDS = 120
_INDEX_RETRY_SECONDS = 60
_EMBEDDING_DOCUMENTS_PER_REQUEST = 64
_INDEX_WARMUP_DOCUMENTS_PER_SEARCH = 256
_MAX_CONCURRENT_INDEX_WARMUPS = 2
_INDEX_WARMUP_SLOT_WAIT_SECONDS = 0.05

_INDEX_WARMUP_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.Semaphore,
] = weakref.WeakKeyDictionary()


def _index_warmup_semaphore() -> asyncio.Semaphore:
    """Return the process-wide index limiter for the active event loop."""

    loop = asyncio.get_running_loop()
    semaphore = _INDEX_WARMUP_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_INDEX_WARMUPS)
        _INDEX_WARMUP_SEMAPHORES[loop] = semaphore
    return semaphore


async def _try_acquire_index_warmup_slot() -> asyncio.Semaphore | None:
    semaphore = _index_warmup_semaphore()
    try:
        await asyncio.wait_for(
            semaphore.acquire(),
            timeout=_INDEX_WARMUP_SLOT_WAIT_SECONDS,
        )
    except TimeoutError:
        return None
    return semaphore


@dataclass(frozen=True)
class McpToolSearchIndexTarget:
    installation_id: str
    tool_name: str
    server_name: str
    server_description: str
    title: str
    description: str
    schema_hash: str
    connection_fingerprint: str


@dataclass(frozen=True)
class _PreparedCandidate:
    document: ToolSearchDocument
    search_document: str
    search_document_hash: str

    @property
    def key(self) -> tuple[str, str] | None:
        installation_id = self.document.installation_id
        if not installation_id or self.document.provider != "mcp":
            return None
        return installation_id, self.document.tool_name


@dataclass(frozen=True)
class _IndexClaim:
    candidate: _PreparedCandidate
    claim_token: str


def _search_document_parts(
    *,
    tool_name: object,
    server_name: object,
    server_description: object,
    title: object,
    description: object,
) -> tuple[str, str, str, str, str]:
    return (
        bound_tool_search_text(tool_name, max_bytes=_MAX_TOOL_NAME_BYTES),
        bound_tool_search_text(server_name, max_bytes=_MAX_SERVER_NAME_BYTES),
        bound_tool_search_text(
            server_description,
            max_bytes=_MAX_SERVER_DESCRIPTION_BYTES,
        ),
        bound_tool_search_text(title, max_bytes=_MAX_TOOL_TITLE_BYTES),
        bound_tool_search_text(
            description,
            max_bytes=MAX_TOOL_SEARCH_DESCRIPTION_BYTES,
        ),
    )


def build_mcp_tool_search_document(
    *,
    tool_name: object,
    server_name: object,
    server_description: object,
    title: object,
    description: object,
) -> tuple[str, str]:
    """Build the canonical bounded document and its content fingerprint."""

    name, server, server_description_text, title_text, description_text = (
        _search_document_parts(
            tool_name=tool_name,
            server_name=server_name,
            server_description=server_description,
            title=title,
            description=description,
        )
    )
    document = "\n".join((
        _SEARCH_DOCUMENT_VERSION,
        f"tool: {name}",
        f"title: {title_text}",
        f"connection: {server}",
        f"connection description: {server_description_text}",
        f"capability: {description_text}",
    ))
    return document, hashlib.sha256(document.encode("utf-8")).hexdigest()


def sync_mcp_tool_search_indexes(
    db,
    *,
    installation_id: str,
    targets: list[McpToolSearchIndexTarget],
) -> None:
    """Synchronize derived index identities inside the snapshot transaction.

    This function performs no network I/O and never commits. Unchanged rows
    retain their vectors; changed identities are invalidated atomically.
    """

    existing_rows = {
        str(row.tool_name): row
        for row in (
            db.query(McpToolSearchIndex)
            .options(
                load_only(
                    McpToolSearchIndex.tool_name,
                    McpToolSearchIndex.search_document_hash,
                    McpToolSearchIndex.schema_hash,
                    McpToolSearchIndex.connection_fingerprint,
                )
            )
            .filter(McpToolSearchIndex.installation_id == installation_id)
            .order_by(McpToolSearchIndex.tool_name)
            .with_for_update()
            .all()
        )
    }
    target_names = {target.tool_name for target in targets}
    if existing_rows.keys() - target_names:
        db.query(McpToolSearchIndex).filter(
            McpToolSearchIndex.installation_id == installation_id,
            McpToolSearchIndex.tool_name.in_(existing_rows.keys() - target_names),
        ).delete(synchronize_session=False)

    updated_at = now_naive()
    for target in targets:
        document, document_hash = build_mcp_tool_search_document(
            tool_name=target.tool_name,
            server_name=target.server_name,
            server_description=target.server_description,
            title=target.title,
            description=target.description,
        )
        row = existing_rows.get(target.tool_name)
        if row is None:
            db.add(McpToolSearchIndex(
                installation_id=installation_id,
                tool_name=target.tool_name,
                search_document=document,
                search_document_hash=document_hash,
                schema_hash=target.schema_hash,
                connection_fingerprint=target.connection_fingerprint,
                updated_at=updated_at,
            ))
            continue

        unchanged = (
            str(row.search_document_hash or "") == document_hash
            and str(row.schema_hash or "") == target.schema_hash
            and str(row.connection_fingerprint or "")
            == target.connection_fingerprint
        )
        row.search_document = document
        row.search_document_hash = document_hash
        row.schema_hash = target.schema_hash
        row.connection_fingerprint = target.connection_fingerprint
        row.updated_at = updated_at
        if not unchanged:
            row.embedding = None
            row.embedding_model_fingerprint = None
            row.embedded_document_hash = None
            row.claim_token = None
            row.lease_expires_at = None
            row.retry_after = None


def _prepare_candidate(document: ToolSearchDocument) -> _PreparedCandidate:
    search_document, document_hash = build_mcp_tool_search_document(
        tool_name=document.tool_name,
        server_name=document.server_name,
        server_description=document.server_description,
        title=document.title,
        description=document.description,
    )
    return _PreparedCandidate(
        document=document,
        search_document=search_document,
        search_document_hash=document_hash,
    )


def _tokenize(value: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", value.casefold())


def _keyword_ranking(candidates: list[_PreparedCandidate], query: str) -> list[str]:
    query_terms = list(dict.fromkeys(_tokenize(query)))
    if not query_terms or not candidates:
        return []

    weighted_terms: list[Counter[str]] = []
    for candidate in candidates:
        document = candidate.document
        counts: Counter[str] = Counter()
        for value, weight in (
            (document.tool_name, 4),
            (document.title, 3),
            (document.server_name, 2),
            (document.server_description, 1),
            (document.description, 1),
        ):
            for term, count in Counter(_tokenize(value)).items():
                counts[term] += count * weight
        weighted_terms.append(counts)

    document_count = len(candidates)
    average_length = sum(sum(item.values()) for item in weighted_terms) / max(
        document_count,
        1,
    )
    frequencies: Counter[str] = Counter()
    for terms in weighted_terms:
        for term in terms:
            frequencies[term] += 1

    scored: list[tuple[float, str]] = []
    k1, b = 1.5, 0.75
    for candidate, terms in zip(candidates, weighted_terms):
        length = sum(terms.values())
        if not length:
            continue
        score = 0.0
        for term in query_terms:
            term_frequency = terms.get(term, 0)
            if not term_frequency:
                continue
            containing = frequencies.get(term, 0)
            inverse_frequency = math.log(
                (document_count - containing + 0.5) / (containing + 0.5) + 1.0
            )
            denominator = term_frequency + k1 * (
                1 - b + b * length / max(average_length, 1.0)
            )
            score += inverse_frequency * term_frequency * (k1 + 1) / denominator
        if score > 0:
            scored.append((score, candidate.document.model_name))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [model_name for _score, model_name in scored]


def _rrf_ranking(
    keyword_names: list[str],
    semantic_names: list[str],
    *,
    limit: int,
) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in (keyword_names, semantic_names):
        for rank, model_name in enumerate(ranking, start=1):
            scores[model_name] = scores.get(model_name, 0.0) + 1.0 / (
                _RRF_K + rank
            )
    return [
        model_name
        for model_name, _score in sorted(
            scores.items(),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
    ]


def _normalize_valid_vector(value: object) -> list[float] | None:
    try:
        vector = normalize_embedding_vector(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not vector or not all(math.isfinite(item) for item in vector):
        return None
    if not any(item != 0.0 for item in vector):
        return None
    return vector


class SqlAlchemyMcpToolSearchRepository:
    """Lease-fenced persistent vector index restricted to supplied candidates."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    @staticmethod
    def _key_filter(keys: list[tuple[str, str]]):
        return tuple_(
            McpToolSearchIndex.installation_id,
            McpToolSearchIndex.tool_name,
        ).in_(keys)

    def claim_missing(
        self,
        candidates: list[_PreparedCandidate],
        *,
        model_fingerprint: str,
    ) -> list[_IndexClaim]:
        keyed = {candidate.key: candidate for candidate in candidates if candidate.key}
        if not keyed:
            return []
        now = now_naive()
        lease_until = now + timedelta(seconds=_INDEX_LEASE_SECONDS)
        claims: list[_IndexClaim] = []
        candidate_identities = [
            (
                key[0],
                key[1],
                candidate.search_document_hash,
                candidate.document.schema_hash,
                candidate.document.connection_fingerprint,
            )
            for key, candidate in keyed.items()
            if key is not None
        ]
        with self._session_factory() as db:
            rows = (
                db.query(McpToolSearchIndex)
                .options(
                    load_only(
                        McpToolSearchIndex.installation_id,
                        McpToolSearchIndex.tool_name,
                        McpToolSearchIndex.search_document_hash,
                        McpToolSearchIndex.schema_hash,
                        McpToolSearchIndex.connection_fingerprint,
                        McpToolSearchIndex.embedding_model_fingerprint,
                        McpToolSearchIndex.embedded_document_hash,
                        McpToolSearchIndex.claim_token,
                        McpToolSearchIndex.lease_expires_at,
                        McpToolSearchIndex.retry_after,
                    )
                )
                .filter(
                    tuple_(
                        McpToolSearchIndex.installation_id,
                        McpToolSearchIndex.tool_name,
                        McpToolSearchIndex.search_document_hash,
                        McpToolSearchIndex.schema_hash,
                        McpToolSearchIndex.connection_fingerprint,
                    ).in_(candidate_identities),
                    or_(
                        McpToolSearchIndex.embedding.is_(None),
                        McpToolSearchIndex.embedding_model_fingerprint.is_(None),
                        McpToolSearchIndex.embedding_model_fingerprint
                        != model_fingerprint,
                        McpToolSearchIndex.embedded_document_hash.is_(None),
                        McpToolSearchIndex.embedded_document_hash
                        != McpToolSearchIndex.search_document_hash,
                    ),
                    or_(
                        McpToolSearchIndex.retry_after.is_(None),
                        McpToolSearchIndex.retry_after <= now,
                    ),
                    or_(
                        McpToolSearchIndex.claim_token.is_(None),
                        McpToolSearchIndex.lease_expires_at.is_(None),
                        McpToolSearchIndex.lease_expires_at <= now,
                    ),
                )
                .order_by(
                    McpToolSearchIndex.installation_id,
                    McpToolSearchIndex.tool_name,
                )
                .limit(_INDEX_WARMUP_DOCUMENTS_PER_SEARCH)
                .with_for_update(skip_locked=True)
                .all()
            )
            for row in rows:
                candidate = keyed.get((str(row.installation_id), str(row.tool_name)))
                if candidate is None:
                    continue
                document = candidate.document
                if (
                    str(row.search_document_hash or "")
                    != candidate.search_document_hash
                    or str(row.schema_hash or "") != document.schema_hash
                    or str(row.connection_fingerprint or "")
                    != document.connection_fingerprint
                ):
                    continue
                token = uuid.uuid4().hex
                row.claim_token = token
                row.lease_expires_at = lease_until
                row.retry_after = None
                claims.append(_IndexClaim(candidate=candidate, claim_token=token))
            db.commit()
        return claims

    def finalize_claims(
        self,
        claims: list[_IndexClaim],
        vectors: list[list[float] | None],
        *,
        model_fingerprint: str,
    ) -> None:
        if not claims:
            return
        if len(claims) != len(vectors):
            raise ValueError("MCP tool index claim/vector counts do not match")
        now = now_naive()
        retry_after = now + timedelta(seconds=_INDEX_RETRY_SECONDS)
        ordered_claim_vectors = sorted(
            zip(claims, vectors),
            key=lambda item: item[0].candidate.key or ("", ""),
        )
        with self._session_factory() as db:
            for claim, vector in ordered_claim_vectors:
                candidate = claim.candidate
                key = candidate.key
                if key is None:
                    continue
                values: dict[str, object] = {
                    "claim_token": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
                if vector is None:
                    values["retry_after"] = retry_after
                else:
                    values.update({
                        "embedding": vector,
                        "embedding_model_fingerprint": model_fingerprint,
                        "embedded_document_hash": candidate.search_document_hash,
                        "retry_after": None,
                    })
                db.query(McpToolSearchIndex).filter(
                    McpToolSearchIndex.installation_id == key[0],
                    McpToolSearchIndex.tool_name == key[1],
                    McpToolSearchIndex.claim_token == claim.claim_token,
                    McpToolSearchIndex.search_document_hash
                    == candidate.search_document_hash,
                    McpToolSearchIndex.schema_hash
                    == candidate.document.schema_hash,
                    McpToolSearchIndex.connection_fingerprint
                    == candidate.document.connection_fingerprint,
                ).update(values, synchronize_session=False)
            db.commit()

    def vector_ranking(
        self,
        query_vector: list[float],
        candidates: list[_PreparedCandidate],
        *,
        model_fingerprint: str,
        min_score: float,
    ) -> list[str]:
        keyed = {candidate.key: candidate for candidate in candidates if candidate.key}
        if not keyed:
            return []
        vector_literal = serialize_pgvector(query_vector)
        vector_param = cast(
            bindparam("mcp_tool_query_vector", value=vector_literal),
            McpToolSearchIndex.embedding.type,
        )
        distance = McpToolSearchIndex.embedding.op("<=>")(vector_param)
        score_expression = 1 - distance
        with self._session_factory() as db:
            rows = (
                db.query(
                    McpToolSearchIndex.installation_id,
                    McpToolSearchIndex.tool_name,
                    McpToolSearchIndex.search_document_hash,
                    McpToolSearchIndex.embedded_document_hash,
                    McpToolSearchIndex.schema_hash,
                    McpToolSearchIndex.connection_fingerprint,
                    score_expression.label("score"),
                )
                .filter(
                    self._key_filter(list(keyed)),
                    McpToolSearchIndex.embedding.isnot(None),
                    McpToolSearchIndex.embedding_model_fingerprint
                    == model_fingerprint,
                )
                .order_by(distance)
                .all()
            )

        ranked: list[tuple[float, str]] = []
        for row in rows:
            candidate = keyed.get((str(row.installation_id), str(row.tool_name)))
            if candidate is None:
                continue
            document = candidate.document
            if (
                str(row.search_document_hash or "")
                != candidate.search_document_hash
                or str(row.embedded_document_hash or "")
                != candidate.search_document_hash
                or str(row.schema_hash or "") != document.schema_hash
                or str(row.connection_fingerprint or "")
                != document.connection_fingerprint
            ):
                continue
            try:
                score = float(row.score)
            except (TypeError, ValueError):
                continue
            if math.isfinite(score) and score >= min_score:
                ranked.append((score, document.model_name))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [model_name for _score, model_name in ranked]


class McpToolSearchService:
    """BM25 + pgvector + RRF ranking with deterministic lexical fallback."""

    def __init__(
        self,
        repository: SqlAlchemyMcpToolSearchRepository,
        *,
        embedding_provider: Callable[
            [list[str], EmbeddingRequestConfig],
            Awaitable[list[list[float] | None]],
        ] | None = None,
        config_provider: Callable[[], EmbeddingRequestConfig | None] | None = None,
    ) -> None:
        self._repository = repository
        self._embedding_provider = embedding_provider or self._generate_embeddings
        self._config_provider = config_provider or resolve_embedding_request_config

    @staticmethod
    async def _generate_embeddings(
        texts: list[str],
        config: EmbeddingRequestConfig,
    ) -> list[list[float] | None]:
        return await generate_embeddings(texts, config=config)

    async def rank(
        self,
        query: str,
        candidates: list[ToolSearchDocument],
        *,
        limit: int,
    ) -> list[str]:
        prepared = [_prepare_candidate(candidate) for candidate in candidates]
        keyword_names = _keyword_ranking(prepared, query)
        bounded_limit = max(1, min(int(limit), len(prepared))) if prepared else 0
        if not prepared or bounded_limit == 0:
            return []

        try:
            config = self._config_provider()
        except Exception:
            logger.warning("Embedding 配置不可用，MCP 工具检索降级为关键词排序")
            config = None
        if config is None:
            return keyword_names[:bounded_limit]

        warmup_semaphore: asyncio.Semaphore | None = None
        try:
            warmup_semaphore = await _try_acquire_index_warmup_slot()
            claims = (
                self._repository.claim_missing(
                    prepared,
                    model_fingerprint=config.identity,
                )
                if warmup_semaphore is not None
                else []
            )
            if warmup_semaphore is not None and not claims:
                warmup_semaphore.release()
                warmup_semaphore = None
            document_claim_batches = [
                claims[index : index + _EMBEDDING_DOCUMENTS_PER_REQUEST]
                for index in range(0, len(claims), _EMBEDDING_DOCUMENTS_PER_REQUEST)
            ]
            embedding_inputs_by_batch = [[query]] + [
                [claim.candidate.search_document for claim in claim_batch]
                for claim_batch in document_claim_batches
            ]
            generated_batches = await asyncio.gather(
                *(
                    self._embedding_provider(inputs, config)
                    for inputs in embedding_inputs_by_batch
                ),
                return_exceptions=True,
            )

            query_generated = generated_batches[0]
            if isinstance(query_generated, BaseException):
                if not isinstance(query_generated, Exception):
                    raise query_generated
                logger.warning(
                    "MCP 工具查询 embedding 失败，降级为关键词排序: %s",
                    query_generated,
                )
                query_vector = None
            elif len(query_generated) != 1:
                logger.warning("MCP 工具查询 embedding 结果数量异常，降级为关键词排序")
                query_vector = None
            else:
                query_vector = _normalize_valid_vector(query_generated[0])

            document_vectors: list[list[float] | None] = []
            for claim_batch, inputs, generated in zip(
                document_claim_batches,
                embedding_inputs_by_batch[1:],
                generated_batches[1:],
            ):
                if isinstance(generated, BaseException):
                    if not isinstance(generated, Exception):
                        raise generated
                    logger.warning(
                        "MCP 工具索引 embedding 批次失败，稍后重试: %s",
                        generated,
                    )
                    values: list[object] = [None] * len(inputs)
                elif len(generated) != len(inputs):
                    logger.warning(
                        "MCP 工具索引 embedding 批次结果数量异常，稍后重试"
                    )
                    values = [None] * len(inputs)
                else:
                    values = list(generated)
                document_vectors.extend(
                    _normalize_valid_vector(value) for value in values
                )
            self._repository.finalize_claims(
                claims,
                document_vectors,
                model_fingerprint=config.identity,
            )
            if warmup_semaphore is not None:
                warmup_semaphore.release()
                warmup_semaphore = None
            if query_vector is None:
                return keyword_names[:bounded_limit]
            semantic_names = self._repository.vector_ranking(
                query_vector,
                prepared,
                model_fingerprint=config.identity,
                min_score=_SEMANTIC_MIN_SCORE,
            )
        except Exception:
            logger.warning("MCP 工具语义检索失败，降级为关键词排序", exc_info=True)
            return keyword_names[:bounded_limit]
        finally:
            if warmup_semaphore is not None:
                warmup_semaphore.release()

        allowed_names = {candidate.document.model_name for candidate in prepared}
        semantic_names = [
            name for name in semantic_names if name in allowed_names
        ]
        return _rrf_ranking(
            keyword_names,
            semantic_names,
            limit=bounded_limit,
        )


_mcp_tool_search_service: McpToolSearchService | None = None


def get_mcp_tool_search_service() -> McpToolSearchService:
    global _mcp_tool_search_service
    if _mcp_tool_search_service is None:
        from src.api.models.database import SessionLocal

        _mcp_tool_search_service = McpToolSearchService(
            SqlAlchemyMcpToolSearchRepository(SessionLocal)
        )
    return _mcp_tool_search_service


__all__ = [
    "McpToolSearchIndexTarget",
    "McpToolSearchService",
    "SqlAlchemyMcpToolSearchRepository",
    "build_mcp_tool_search_document",
    "get_mcp_tool_search_service",
    "sync_mcp_tool_search_indexes",
]
