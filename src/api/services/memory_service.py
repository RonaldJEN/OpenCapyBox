"""记忆服务 — 分层记忆的 CRUD、Embedding 索引与混合检索

职责：
- UserMemory 表的读写（USER.md / MEMORY.md / SOUL.md）
- AGENTS.md 由平台模板管理，不写入用户 DB
- 乐观锁版本控制（防止并发写冲突）
- Embedding 分块 + 写入 MemoryEmbedding 表
- 混合检索：BM25 关键词 + 向量语义 + RRF 融合 + 时间衰减
- 沙箱文件同步（DB → sandbox）
- 新用户默认注入文件（SOUL.md / MEMORY.md / USER.md）
"""

import logging
import math
import posixpath
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.user_memory import UserMemory, MemoryEmbedding
from src.api.utils.timezone import now_naive
from src.api.utils.embedding_vector import normalize_embedding_vector, serialize_pgvector

logger = logging.getLogger(__name__)
settings = get_settings()

# 有效的 file_type 枚举。agents_md 保留为兼容旧数据/旧接口识别，
# 但不再作为用户 DB 可写配置。
VALID_FILE_TYPES = {"user_md", "memory_md", "soul_md", "agents_md"}
DB_BACKED_FILE_TYPES = {"user_md", "memory_md", "soul_md"}
TEMPLATE_MANAGED_FILE_TYPES = {"agents_md"}

# file_type → 沙箱文件名映射
FILE_TYPE_TO_FILENAME = {
    "user_md": "USER.md",
    "memory_md": "MEMORY.md",
    "soul_md": "SOUL.md",
    "agents_md": "AGENTS.md",
}

# 默认模板目录（docs/sandbox_template/ 下的同名文件）
_TEMPLATE_DIR = Path(__file__).parent.parent.parent.parent / "docs" / "sandbox_template"

# file_type → 模板文件名映射
_TEMPLATE_FILES: dict[str, str] = {
    "soul_md": "SOUL.md",
    "memory_md": "MEMORY.md",
    "user_md": "USER.md",
}
_AGENTS_TEMPLATE_FILE = "AGENTS.md"


def get_agent_config_file_type_for_path(path: str, mount_path: str | None = None) -> str | None:
    """Return file_type only for root-level Agent config files in the sandbox mount."""
    if not path:
        return None

    if mount_path is None:
        from src.api.services.sandbox_service import get_sandbox_mount_path

        mount_path = get_sandbox_mount_path()

    normalized_path = posixpath.normpath(path)
    normalized_mount = posixpath.normpath(mount_path)

    for file_type, filename in FILE_TYPE_TO_FILENAME.items():
        if file_type not in DB_BACKED_FILE_TYPES:
            continue
        if normalized_path == posixpath.join(normalized_mount, filename):
            return file_type
    return None


class MemoryService:
    """分层记忆服务"""

    def __init__(self, db: DBSession):
        self.db = db

    # ------------------------------------------------------------------
    # UserMemory CRUD
    # ------------------------------------------------------------------

    def get_memory_file(self, user_id: str, file_type: str) -> Optional[UserMemory]:
        """读取指定类型的记忆文件"""
        if file_type not in VALID_FILE_TYPES:
            raise ValueError(f"无效的 file_type: {file_type}")
        if file_type in TEMPLATE_MANAGED_FILE_TYPES:
            return None
        return (
            self.db.query(UserMemory)
            .filter(UserMemory.user_id == user_id, UserMemory.file_type == file_type)
            .first()
        )

    def get_memory_content(self, user_id: str, file_type: str) -> str:
        """读取记忆内容（不存在则返回空字符串）"""
        if file_type == "agents_md":
            return self.get_agents_template_content()
        record = self.get_memory_file(user_id, file_type)
        return record.content if record else ""

    def upsert_memory_file(
        self,
        user_id: str,
        file_type: str,
        content: str,
        expected_version: int | None = None,
    ) -> UserMemory:
        """写入/更新记忆文件

        Args:
            user_id: 用户 ID
            file_type: 文件类型
            content: 新内容
            expected_version: 乐观锁版本号（不为 None 时校验）

        Returns:
            更新后的 UserMemory 对象

        Raises:
            ValueError: file_type 无效
            RuntimeError: 乐观锁冲突
        """
        if file_type not in VALID_FILE_TYPES:
            raise ValueError(f"无效的 file_type: {file_type}")
        if file_type in TEMPLATE_MANAGED_FILE_TYPES:
            raise ValueError("AGENTS.md 由平台模板管理，不能写入用户 DB")

        record = (
            self.db.query(UserMemory)
            .filter(UserMemory.user_id == user_id, UserMemory.file_type == file_type)
            .first()
        )

        if record:
            if expected_version is not None and record.version != expected_version:
                raise RuntimeError(
                    f"乐观锁冲突: 期望版本 {expected_version}, 实际版本 {record.version}"
                )
            record.content = content
            record.version = record.version + 1
            record.updated_at = now_naive()
        else:
            record = UserMemory(
                user_id=user_id,
                file_type=file_type,
                content=content,
                version=1,
            )
            self.db.add(record)

        self.db.commit()
        self.db.refresh(record)
        return record

    def upsert_memory_file_if_changed(
        self,
        user_id: str,
        file_type: str,
        content: str,
    ) -> tuple[UserMemory, bool]:
        """Write a memory file only when content differs.

        Empty strings are valid content and are persisted.
        """
        record = self.get_memory_file(user_id, file_type)
        if record and record.content == content:
            return record, False

        return self.upsert_memory_file(user_id, file_type, content), True

    async def sync_agent_config_content(
        self,
        user_id: str,
        file_type: str,
        content: str,
    ) -> tuple[UserMemory, bool]:
        """Persist Agent config content and refresh searchable indexes when needed."""
        record, changed = self.upsert_memory_file_if_changed(user_id, file_type, content)
        if changed and file_type in ("user_md", "memory_md"):
            await self.rebuild_embeddings(user_id, FILE_TYPE_TO_FILENAME[file_type], content)
        return record, changed

    def get_all_memory_files(self, user_id: str) -> dict[str, str]:
        """获取用户 DB-backed 记忆文件的内容（不含模板管理的 AGENTS.md）"""
        records = (
            self.db.query(UserMemory)
            .filter(
                UserMemory.user_id == user_id,
                UserMemory.file_type.in_(tuple(DB_BACKED_FILE_TYPES)),
            )
            .all()
        )
        return {r.file_type: r.content for r in records}

    def is_new_user(self, user_id: str) -> bool:
        """判断用户是否为新用户（DB 中无任何 DB-backed 记忆文件）"""
        count = (
            self.db.query(UserMemory)
            .filter(
                UserMemory.user_id == user_id,
                UserMemory.file_type.in_(tuple(DB_BACKED_FILE_TYPES)),
            )
            .count()
        )
        return count == 0

    def provision_default_files(self, user_id: str) -> int:
        """为新用户写入默认注入文件模板（从 docs/ 目录读取）

        仅在用户 DB 中无任何记忆文件时执行（幂等）。
        写入 SOUL.md / MEMORY.md / USER.md(PROFILE) 到 DB。
        AGENTS.md 始终从平台模板读取，不写入用户 DB。

        Args:
            user_id: 用户 ID

        Returns:
            写入的文件数量（0 表示非新用户，跳过）
        """
        if not self.is_new_user(user_id):
            return 0

        count = 0
        for file_type, template_name in _TEMPLATE_FILES.items():
            template_path = _TEMPLATE_DIR / template_name
            if not template_path.exists():
                logger.warning("默认模板不存在: %s", template_path)
                continue

            content = template_path.read_text(encoding="utf-8")
            # 去除 YAML frontmatter（--- 之间的内容）
            content = self._strip_frontmatter(content)
            if not content.strip():
                continue

            self.upsert_memory_file(user_id, file_type, content)
            count += 1
            logger.info("已为新用户写入默认模板: user=%s, file=%s", user_id, template_name)

        logger.info("新用户默认文件初始化完成: user=%s, count=%d", user_id, count)
        return count

    def get_agents_template_content(self) -> str:
        """读取平台级 AGENTS.md 模板内容，去除 frontmatter。"""
        template_path = _TEMPLATE_DIR / _AGENTS_TEMPLATE_FILE
        if not template_path.exists():
            logger.warning("AGENTS.md 平台模板不存在: %s", template_path)
            return ""
        content = template_path.read_text(encoding="utf-8")
        return self._strip_frontmatter(content)

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        """去除 Markdown YAML frontmatter（--- 之间的内容）"""
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                return text[end + 3:].lstrip("\n")
        return text

    # ------------------------------------------------------------------
    # Embedding 索引
    # ------------------------------------------------------------------

    async def rebuild_embeddings(self, user_id: str, file_path: str, text: str) -> int:
        """为文本重建向量索引

        Args:
            user_id: 用户 ID
            file_path: 来源文件路径 (e.g., "memory/2026-03-26.md")
            text: 要索引的文本

        Returns:
            创建的 embedding 数量
        """
        # 删除旧索引
        self.db.query(MemoryEmbedding).filter(
            MemoryEmbedding.user_id == user_id,
            MemoryEmbedding.file_path == file_path,
        ).delete()

        # 分块
        chunks = self._chunk_text(text, settings.embedding_chunk_size)
        if not chunks:
            self.db.commit()
            return 0

        # 生成 embedding
        embeddings = await self._generate_embeddings([c for c in chunks])

        # 写入
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            record = MemoryEmbedding(
                user_id=user_id,
                file_path=file_path,
                chunk_index=i,
                chunk_text=chunk,
                embedding=normalize_embedding_vector(emb),
            )
            self.db.add(record)

        self.db.commit()
        return len(chunks)

    async def search_memory(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """混合检索记忆：BM25 + 向量语义 + RRF 融合 + 时间衰减

        策略：
        1. 始终执行 BM25 关键词检索
        2. 若 Embedding 可用，同时执行向量语义检索
        3. 使用 Reciprocal Rank Fusion (RRF) 融合两路结果
        4. 对结果施加时间衰减（常青文件不衰减）

        Args:
            user_id: 用户 ID
            query: 查询文本
            top_k: 返回结果数
            start_date: 时间范围下界（ISO YYYY-MM-DD），仅返回该日期及之后的记录
            end_date: 时间范围上界（ISO YYYY-MM-DD），仅返回该日期及之前的记录

        Returns:
            匹配的记忆片段列表
        """
        # 解析时间范围
        dt_start, dt_end = self._parse_date_range(start_date, end_date)

        fetch_k = top_k * 3  # 多取一些用于融合

        # 1. BM25 始终执行
        bm25_results = self._search_by_bm25(user_id, query, fetch_k, dt_start=dt_start, dt_end=dt_end)

        # 2. 尝试向量检索
        vec_results: list[dict] = []
        if self._is_embedding_available():
            try:
                vec_results = await self._search_by_embedding(user_id, query, fetch_k, dt_start=dt_start, dt_end=dt_end)
            except Exception as e:
                logger.warning("向量检索失败，降级为纯 BM25: %s", e)

        # 3. 融合
        if vec_results and bm25_results:
            merged = self._rrf_fusion(vec_results, bm25_results, top_k)
        elif vec_results:
            merged = vec_results[:top_k]
        else:
            merged = bm25_results[:top_k]

        # 4. 时间衰减
        return self._apply_time_decay(merged)

    @staticmethod
    def _parse_date_range(
        start_date: str | None, end_date: str | None
    ) -> tuple[datetime | None, datetime | None]:
        """解析 ISO 日期字符串为 datetime 边界"""
        dt_start = None
        dt_end = None
        if start_date:
            try:
                dt_start = datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                logger.warning("无法解析 start_date: %s", start_date)
        if end_date:
            try:
                # end_date 取当天 23:59:59
                dt_end = datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
            except ValueError:
                logger.warning("无法解析 end_date: %s", end_date)
        return dt_start, dt_end

    @staticmethod
    def _is_embedding_available() -> bool:
        """检查 Embedding 模型是否可用（优先 model_registry，fallback settings）"""
        try:
            from src.api.model_registry import get_model_registry
            registry = get_model_registry()
            emb_config = registry.get_embedding_model()
            if emb_config:
                return True
        except Exception:
            pass
        # fallback: 旧版 settings 配置
        return bool(settings.embedding_api_key)

    async def _search_by_embedding(
        self, user_id: str, query: str, top_k: int,
        *, dt_start: datetime | None = None, dt_end: datetime | None = None,
    ) -> list[dict]:
        """向量语义检索：PostgreSQL 使用 pgvector 原生距离算子。"""
        query_embedding = await self._generate_embeddings([query])
        if not query_embedding or not query_embedding[0]:
            return []

        qvec = normalize_embedding_vector(query_embedding[0])

        filters = [
            MemoryEmbedding.user_id == user_id,
            MemoryEmbedding.embedding.isnot(None),
        ]
        if dt_start:
            filters.append(MemoryEmbedding.created_at >= dt_start)
        if dt_end:
            filters.append(MemoryEmbedding.created_at <= dt_end)
        # 指定时间范围时排除常青文件（用户要的是那段时间的对话/事件）
        if dt_start or dt_end:
            for kw in ("MEMORY.md", "USER.md", "SOUL.md", "AGENTS.md"):
                filters.append(or_(MemoryEmbedding.file_path.is_(None), MemoryEmbedding.file_path.notlike(f"%{kw}%")))

        return self._search_by_pgvector(qvec, filters, top_k)

    def _search_by_pgvector(
        self, qvec: list[float], filters: list, top_k: int
    ) -> list[dict]:
        """PostgreSQL pgvector 原生余弦距离检索。"""
        from sqlalchemy import and_, bindparam, cast

        qvec_literal = serialize_pgvector(qvec)

        # 使用参数绑定传递向量值，避免 SQL 拼接风险
        qvec_param = cast(bindparam("qvec", value=qvec_literal), MemoryEmbedding.embedding.type)
        distance_expr = MemoryEmbedding.embedding.op("<=>")(qvec_param)
        score_expr = 1 - distance_expr

        query = (
            self.db.query(MemoryEmbedding, score_expr.label("score"))
            .filter(and_(*filters))
            .order_by(distance_expr)
            .limit(top_k)
        )

        results = []
        for row in query.all():
            item = row[0]
            score = row[1]
            results.append({
                "file_path": item.file_path,
                "chunk_index": item.chunk_index,
                "text": item.chunk_text,
                "score": round(float(score), 4),
                "created_at": item.created_at,
            })
        return results

    def _search_by_bm25(
        self, user_id: str, query: str, top_k: int,
        *, dt_start: datetime | None = None, dt_end: datetime | None = None,
    ) -> list[dict]:
        """BM25 关键词检索"""
        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        filters = [MemoryEmbedding.user_id == user_id]
        if dt_start:
            filters.append(MemoryEmbedding.created_at >= dt_start)
        if dt_end:
            filters.append(MemoryEmbedding.created_at <= dt_end)
        # 指定时间范围时排除常青文件
        if dt_start or dt_end:
            for kw in ("MEMORY.md", "USER.md", "SOUL.md", "AGENTS.md"):
                filters.append(or_(MemoryEmbedding.file_path.is_(None), MemoryEmbedding.file_path.notlike(f"%{kw}%")))

        all_chunks = (
            self.db.query(MemoryEmbedding)
            .filter(*filters)
            .all()
        )
        if not all_chunks:
            return []

        # 构建文档集合的 term 列表
        doc_terms_list = [self._tokenize(c.chunk_text or "") for c in all_chunks]
        N = len(all_chunks)
        avg_dl = sum(len(dt) for dt in doc_terms_list) / max(N, 1)

        # 文档频率
        df: Counter = Counter()
        for dt in doc_terms_list:
            for term in set(dt):
                df[term] += 1

        # BM25 参数
        k1, b = 1.5, 0.75

        scored = []
        for chunk, doc_terms in zip(all_chunks, doc_terms_list):
            if not doc_terms:
                continue
            dl = len(doc_terms)
            tf = Counter(doc_terms)
            score = 0.0
            for term in query_terms:
                if term not in tf:
                    continue
                n = df.get(term, 0)
                idf = math.log((N - n + 0.5) / (n + 0.5) + 1.0)
                tf_norm = (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * dl / avg_dl))
                score += idf * tf_norm
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "file_path": item.file_path,
                "chunk_index": item.chunk_index,
                "text": item.chunk_text,
                "score": round(score, 4),
                "created_at": item.created_at,
            }
            for score, item in scored[:top_k]
        ]

    def _search_by_keyword(
        self, user_id: str, query: str, top_k: int
    ) -> list[dict]:
        """关键词降级检索（向后兼容，内部委托给 BM25）"""
        return self._search_by_bm25(user_id, query, top_k)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中英文混合分词

        英文按单词切分，中文按字切分（零依赖，不需要 jieba）。
        """
        if not text:
            return []
        # 提取英文单词（含数字）和单个中文字符
        return re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", text.lower())

    @staticmethod
    def _rrf_fusion(
        vec_results: list[dict],
        bm25_results: list[dict],
        top_k: int,
        k: int = 60,
    ) -> list[dict]:
        """Reciprocal Rank Fusion（RRF）融合两路检索结果

        RRF 比加权分数更稳健，不受两路分数分布差异影响。
        score = Σ 1 / (k + rank)

        Args:
            vec_results: 向量检索结果（已按 score 降序）
            bm25_results: BM25 检索结果（已按 score 降序）
            top_k: 返回数量
            k: RRF 常数（默认 60）
        """
        fused: dict[str, dict] = {}

        for rank, r in enumerate(vec_results):
            key = f"{r['file_path']}:{r.get('chunk_index', 0)}"
            if key not in fused:
                fused[key] = {"item": r, "score": 0.0}
            fused[key]["score"] += 1.0 / (k + rank + 1)

        for rank, r in enumerate(bm25_results):
            key = f"{r['file_path']}:{r.get('chunk_index', 0)}"
            if key not in fused:
                fused[key] = {"item": r, "score": 0.0}
            fused[key]["score"] += 1.0 / (k + rank + 1)

        ranked = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
        return [
            {**entry["item"], "score": round(entry["score"], 4)}
            for entry in ranked[:top_k]
        ]

    @staticmethod
    def _apply_time_decay(
        results: list[dict],
        half_life_days: float = 30.0,
    ) -> list[dict]:
        """对搜索结果应用时间衰减

        根据 file_path 中的日期信息或 created_at 时间戳计算衰减系数。
        常青文件（MEMORY.md / USER.md 等）不衰减。

        Args:
            results: 搜索结果列表
            half_life_days: 半衰期（天），默认 30 天
        """
        if not results:
            return results

        decay_lambda = 0.693147 / half_life_days  # ln(2) / half_life
        now = datetime.now()

        EVERGREEN_KEYWORDS = ("MEMORY.md", "USER.md", "SOUL.md", "AGENTS.md")

        for r in results:
            fp = r.get("file_path", "")

            # 常青文件不衰减
            if any(kw in fp for kw in EVERGREEN_KEYWORDS):
                continue

            # 尝试从路径提取日期（如 memory/2026-03-26.md）
            file_date = None
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", fp)
            if date_match:
                try:
                    file_date = datetime.strptime(date_match.group(1), "%Y-%m-%d")
                except ValueError:
                    pass

            # fallback: 使用记录的 created_at（对话记录的 file_path 是 UUID 无日期）
            if file_date is None and r.get("created_at"):
                created = r["created_at"]
                if isinstance(created, datetime):
                    file_date = created

            if file_date is not None:
                age_days = max((now - file_date).days, 0)
                decay = math.exp(-decay_lambda * age_days)
                r["score"] = round(r["score"] * decay, 4)

        # 重新排序
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # 沙箱同步
    # ------------------------------------------------------------------

    async def sync_to_sandbox(
        self,
        user_id: str,
        sandbox,
        *,
        force: bool = False,
        file_types: set[str] | None = None,
        include_agents_template: bool = True,
    ) -> int:
        """将记忆文件同步到沙箱。

        USER/MEMORY/SOUL 以用户 DB 为源；AGENTS.md 始终以平台模板为源。

        Args:
            user_id: 用户 ID
            sandbox: OpenSandbox 实例
            force: True = 无条件 DB→沙箱推送（用户前端主动保存）;
                   False = 沙箱优先，沙箱有内容则读回 DB 而非覆盖
            file_types: 可选，仅同步指定 DB-backed file_type（USER/MEMORY/SOUL）
            include_agents_template: 是否同步平台 AGENTS.md 模板

        Returns:
            写入沙箱的文件数量（不含回写 DB 的文件）
        """
        from src.api.services.sandbox_service import get_sandbox_mount_path

        selected_file_types = set(file_types) if file_types is not None else None
        if selected_file_types is not None:
            invalid = selected_file_types - DB_BACKED_FILE_TYPES
            if invalid:
                raise ValueError(f"无效的 DB-backed file_type: {sorted(invalid)}")

        records = self.get_all_memory_files(user_id)
        if selected_file_types is not None:
            records = {
                file_type: content
                for file_type, content in records.items()
                if file_type in selected_file_types
            }
        agents_template = self.get_agents_template_content() if include_agents_template else ""
        if not records and not agents_template.strip():
            return 0

        from src.api.services.sandbox_service import get_sandbox_service

        mount = get_sandbox_service().get_mount_path(user_id)
        synced = 0
        for file_type, db_content in records.items():
            if file_type not in DB_BACKED_FILE_TYPES:
                continue
            filename = FILE_TYPE_TO_FILENAME.get(file_type)
            if not filename:
                continue
            path = f"{mount}/{filename}"
            try:
                # 非 force 模式：沙箱有内容则以沙箱为准，回写 DB
                if not force:
                    sandbox_content = None
                    try:
                        sandbox_content = await sandbox.files.read_file(path)
                    except Exception as read_err:
                        # 只有确定是"文件不存在"时才继续用 DB 推送，其他异常一律跳过
                        status = (
                            getattr(read_err, 'status_code', None)
                            or getattr(getattr(read_err, 'response', None), 'status_code', None)
                            or getattr(read_err, 'status', None)
                        )
                        if status == 404 or isinstance(read_err, FileNotFoundError):
                            pass  # 文件不存在，继续走下方 DB→沙箱推送
                        else:
                            logger.warning("读取沙箱文件失败 (%s)，跳过同步: %s", filename, read_err)
                            continue

                    if sandbox_content and sandbox_content.strip():
                        # 沙箱有实质内容 → 保留沙箱版本并回写 DB
                        if sandbox_content != db_content:
                            self.upsert_memory_file(user_id, file_type, sandbox_content)
                            logger.info(
                                "沙箱优先：%s 已从沙箱回写 DB (%d chars)",
                                filename, len(sandbox_content),
                            )
                        continue

                # force 模式 或 沙箱无内容：DB → 沙箱推送
                await sandbox.files.write_file(path, db_content)
                synced += 1
            except Exception as e:
                logger.warning("同步记忆到沙箱失败 (%s): %s", filename, e)

        if agents_template.strip():
            path = f"{mount}/{_AGENTS_TEMPLATE_FILE}"
            try:
                await sandbox.files.write_file(path, agents_template)
                synced += 1
            except Exception as e:
                logger.warning("同步平台 AGENTS.md 模板到沙箱失败: %s", e)

        return synced

    async def sync_from_sandbox(self, user_id: str, sandbox, file_type: str) -> tuple[str, bool] | None:
        """从沙箱读取指定记忆文件并更新 DB

        Returns:
            (读取到的内容, 内容是否变更)，读取失败返回 None
        """
        if file_type in TEMPLATE_MANAGED_FILE_TYPES:
            return None

        from src.api.services.sandbox_service import get_sandbox_service

        filename = FILE_TYPE_TO_FILENAME.get(file_type)
        if not filename:
            return None

        mount = get_sandbox_service().get_mount_path(user_id)
        path = f"{mount}/{filename}"
        try:
            read_file = getattr(sandbox.files, "read_file", None)
            if callable(read_file):
                content = await read_file(path)
            else:
                content = await sandbox.files.read(path)

            if isinstance(content, bytes):
                content = content.decode("utf-8")
            elif content is None:
                content = ""
            elif not isinstance(content, str):
                content = str(content)

            _record, changed = self.upsert_memory_file_if_changed(user_id, file_type, content)
            return content, changed
        except Exception as e:
            logger.debug("从沙箱读取记忆文件失败 (%s): %s", filename, e)
            return None

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 512) -> list[str]:
        """将文本按段落/字符数分块"""
        if not text or not text.strip():
            return []

        paragraphs = re.split(r"\n{2,}", text.strip())
        chunks: list[str] = []
        current = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(current) + len(para) + 2 > chunk_size and current:
                chunks.append(current.strip())
                current = para
            else:
                current = f"{current}\n\n{para}" if current else para

        if current.strip():
            chunks.append(current.strip())

        return chunks

    @staticmethod
    async def _generate_embeddings(texts: list[str]) -> list[list[float] | None]:
        """Generate vectors through the shared registry-first embedding client."""

        from src.api.services.embedding_service import generate_embeddings

        return await generate_embeddings(texts)

    # ------------------------------------------------------------------
    # 对话内容自动索引
    # ------------------------------------------------------------------

    async def index_conversation_round(
        self,
        user_id: str,
        session_id: str,
        round_id: str,
        user_message: str,
        assistant_response: str,
    ) -> int:
        """将一轮对话的内容索引到 memory_embeddings，使 search_memory 可检索

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            round_id: 轮次 ID
            user_message: 用户消息
            assistant_response: Agent 回复

        Returns:
            创建的 embedding 数量
        """
        # 构建对话摘要文本
        parts = []
        if user_message:
            parts.append(f"用户: {user_message}")
        if assistant_response:
            parts.append(f"助手: {assistant_response}")
        if not parts:
            return 0

        text = "\n\n".join(parts)
        file_path = f"conversation/{session_id}/{round_id}"

        # 删除该轮次的旧索引（幂等）
        self.db.query(MemoryEmbedding).filter(
            MemoryEmbedding.user_id == user_id,
            MemoryEmbedding.file_path == file_path,
        ).delete()

        # 分块
        chunks = self._chunk_text(text, settings.embedding_chunk_size)
        if not chunks:
            self.db.commit()
            return 0

        # 生成 embedding
        embeddings = await self._generate_embeddings(chunks)

        # 写入
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            record = MemoryEmbedding(
                user_id=user_id,
                file_path=file_path,
                chunk_index=i,
                chunk_text=chunk,
                embedding=normalize_embedding_vector(emb),
            )
            self.db.add(record)

        self.db.commit()
        logger.info(
            "对话内容已索引: user=%s, session=%s, round=%s, chunks=%d",
            user_id, session_id, round_id, len(chunks),
        )
        return len(chunks)
