"""Embedding 向量存储工具。"""

import json
from collections.abc import Sequence

from sqlalchemy.types import UserDefinedType


MEMORY_EMBEDDING_DIMENSIONS = 2560


class PGVector(UserDefinedType):
    """Shared PostgreSQL pgvector type with JSON-compatible Python values."""

    cache_ok = True

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def get_col_spec(self, **kw):
        return f"vector({self.dimensions})"

    def bind_processor(self, dialect):
        def process(value):
            return serialize_pgvector(value)

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            if isinstance(value, str):
                return parse_embedding_vector(value)
            return list(value)

        return process


def normalize_embedding_vector(
    vector: Sequence[float] | str | None,
    dimensions: int = MEMORY_EMBEDDING_DIMENSIONS,
) -> list[float] | None:
    """将 embedding 归一为固定维度 float list；短向量右侧补 0。"""
    if vector is None:
        return None

    values = parse_embedding_vector(vector) if isinstance(vector, str) else list(vector)
    normalized = [float(value) for value in values]
    if len(normalized) > dimensions:
        raise ValueError(
            f"Embedding 维度 {len(normalized)} 超出目标维度 {dimensions}，"
            f"请更新 MEMORY_EMBEDDING_DIMENSIONS 常量以匹配新模型输出维度"
        )
    if len(normalized) < dimensions:
        normalized.extend([0.0] * (dimensions - len(normalized)))
    return normalized


def parse_embedding_vector(vector: str) -> list[float]:
    """解析 JSON 数组、PG array 字面量或 pgvector 字面量。"""
    value = vector.strip()
    if value.startswith("["):
        return [float(item) for item in json.loads(value)]
    if value.startswith("{") and value.endswith("}"):
        body = value[1:-1].strip()
        return [float(item) for item in body.split(",")] if body else []
    return [float(item) for item in json.loads(value)]


def serialize_pgvector(vector: Sequence[float] | str | None) -> str | None:
    """序列化为 pgvector 输入格式，例如 [0.1,0.2,0.0]。"""
    normalized = normalize_embedding_vector(vector)
    if normalized is None:
        return None
    return "[" + ",".join(format(value, ".17g") for value in normalized) + "]"
