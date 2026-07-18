"""Shared embedding model resolution and API client."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from src.api.config import get_settings
from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingRequestConfig:
    """Resolved non-persistent request configuration for one embedding model."""

    identity: str
    api_key: str
    api_base: str
    model_name: str
    dimensions: int | None = None


def _config_identity(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ordered_response_embeddings(
    payload: object,
    *,
    expected_count: int,
) -> list[list[float]]:
    """Bind OpenAI-compatible response rows to inputs by their index."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Embedding API returned an invalid response payload")
    rows = payload["data"]
    if len(rows) != expected_count or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Embedding API returned an unexpected result count")

    index_presence = ["index" in row for row in rows]
    if any(index_presence) and not all(index_presence):
        raise ValueError("Embedding API returned partially indexed results")
    if not all(index_presence):
        return [row["embedding"] for row in rows]

    missing = object()
    ordered: list[object] = [missing] * expected_count
    for row in rows:
        index = row["index"]
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= expected_count
            or ordered[index] is not missing
        ):
            raise ValueError("Embedding API returned invalid result indexes")
        ordered[index] = row["embedding"]
    if any(value is missing for value in ordered):
        raise ValueError("Embedding API returned incomplete result indexes")
    return ordered  # type: ignore[return-value]


def resolve_embedding_request_config() -> EmbeddingRequestConfig | None:
    """Resolve the same registry-first embedding configuration for every feature."""

    try:
        from src.api.model_registry import get_model_registry

        config = get_model_registry().get_embedding_model()
        if config is not None:
            api_key = config.resolve_api_key()
            payload = {
                "source": "registry",
                "id": config.id,
                "api_base": config.api_base.rstrip("/"),
                "model_name": config.model_name,
                "dimensions": config.dimensions,
                "storage_dimensions": MEMORY_EMBEDDING_DIMENSIONS,
            }
            return EmbeddingRequestConfig(
                identity=_config_identity(payload),
                api_key=api_key,
                api_base=config.api_base,
                model_name=config.model_name,
                dimensions=config.dimensions,
            )
    except Exception:
        # Keep the legacy settings fallback aligned with MemoryService's
        # historical behaviour when registry credentials are unavailable.
        pass

    settings = get_settings()
    if not settings.embedding_api_key:
        return None
    payload = {
        "source": "settings",
        "api_base": settings.embedding_api_base.rstrip("/"),
        "model_name": settings.embedding_model,
        "dimensions": None,
        "storage_dimensions": MEMORY_EMBEDDING_DIMENSIONS,
    }
    return EmbeddingRequestConfig(
        identity=_config_identity(payload),
        api_key=settings.embedding_api_key,
        api_base=settings.embedding_api_base,
        model_name=settings.embedding_model,
    )


async def generate_embeddings(
    texts: list[str],
    *,
    config: EmbeddingRequestConfig | None = None,
) -> list[list[float] | None]:
    """Generate embeddings, returning ``None`` entries on an unavailable backend."""

    if not texts:
        return []
    resolved = config or resolve_embedding_request_config()
    if resolved is None:
        return [None] * len(texts)

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            payload: dict[str, object] = {
                "model": resolved.model_name,
                "input": texts,
            }
            if resolved.dimensions is not None:
                payload["dimensions"] = resolved.dimensions
            response = await client.post(
                f"{resolved.api_base.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {resolved.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            return _ordered_response_embeddings(
                response.json(),
                expected_count=len(texts),
            )
    except Exception as exc:
        logger.warning("Embedding API 调用失败: %s", exc)
        return [None] * len(texts)
