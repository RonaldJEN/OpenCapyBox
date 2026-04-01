"""记忆服务 — 分层记忆的 CRUD、Embedding 索引与混合检索

职责：
- UserMemory 表的读写（USER.md / MEMORY.md / SOUL.md / AGENTS.md / HEARTBEAT.md）
- 乐观锁版本控制（防止并发写冲突）
- Embedding 分块 + 写入 MemoryEmbedding 表
- 混合检索：BM25 关键词 + 向量语义 + RRF 融合 + 时间衰减
- 沙箱文件同步（DB → sandbox）
- 新用户默认注入文件（Bootstrap 机制）
"""

import json
import logging
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.user_memory import UserMemory, MemoryEmbedding
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)
settings = get_settings()

# 有效的 file_type 枚举
VALID_FILE_TYPES = {"user_md", "memory_md", "soul_md", "agents_md", "heartbeat_md"}

# file_type → 沙箱文件名映射
FILE_TYPE_TO_FILENAME = {
    "user_md": "USER.md",
    "memory_md": "MEMORY.md",
    "soul_md": "SOUL.md",
    "agents_md": "AGENTS.md",
    "heartbeat_md": "HEARTBEAT.md",
}

# 默认模板目录（docs/sandbox_template/ 下的同名文件）
_TEMPLATE_DIR = Path(__file__).parent.parent.parent.parent / "docs" / "sandbox_template"

# file_type → 模板文件名映射
_TEMPLATE_FILES: dict[str, str] = {
    "soul_md": "SOUL.md",
    "agents_md": "AGENTS.md",
    "memory_md": "MEMORY.md",
    "heartbeat_md": "HEARTBEAT.md",
    "user_md": "USER.md",
}

# 额外的沙箱独有模板文件（不存 DB，仅写沙箱）
_SANDBOX_ONLY_TEMPLATES: dict[str, str] = {
    "BOOTSTRAP.md": "BOOTSTRAP.md",
}


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
        return (
            self.db.query(UserMemory)
            .filter(UserMemory.user_id == user_id, UserMemory.file_type == file_type)
            .first()
        )

    def get_memory_content(self, user_id: str, file_type: str) -> str:
        """读取记忆内容（不存在则返回空字符串）"""
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

    def get_all_memory_files(self, user_id: str) -> dict[str, str]:
        """获取用户所有记忆文件的内容"""
        records = (
            self.db.query(UserMemory)
            .filter(UserMemory.user_id == user_id)
            .all()
        )
        return {r.file_type: r.content for r in records}

    def is_new_user(self, user_id: str) -> bool:
        """判断用户是否为新用户（DB 中无任何记忆文件）"""
        count = (
            self.db.query(UserMemory)
            .filter(UserMemory.user_id == user_id)
            .count()
        )
        return count == 0

    def provision_default_files(self, user_id: str) -> int:
        """为新用户写入默认注入文件模板（从 docs/ 目录读取）

        仅在用户 DB 中无任何记忆文件时执行（幂等）。
        写入 SOUL.md / AGENTS.md / MEMORY.md / HEARTBEAT.md / USER.md(PROFILE) 到 DB。

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

    async def provision_sandbox_templates(self, user_id: str, sandbox) -> int:
        """将额外的沙箱独有模板文件写入沙箱（如 BOOTSTRAP.md）

        这些文件不存 DB，仅在沙箱中存在。Agent 完成引导后会自行删除。

        Args:
            user_id: 用户 ID
            sandbox: OpenSandbox 实例

        Returns:
            写入的文件数量
        """
        from src.api.services.sandbox_service import get_sandbox_mount_path

        mount = get_sandbox_mount_path()
        count = 0
        for sandbox_filename, template_name in _SANDBOX_ONLY_TEMPLATES.items():
            sandbox_path = f"{mount}/{sandbox_filename}"

            # 检查沙箱中是否已存在（幂等）
            try:
                read_fn = getattr(sandbox.files, "read_file", None)
                if callable(read_fn):
                    existing = await read_fn(sandbox_path)
                else:
                    existing = await sandbox.files.read(sandbox_path)
                if existing:
                    continue  # 已存在，不覆盖
            except Exception:
                pass  # 文件不存在，正常继续

            template_path = _TEMPLATE_DIR / template_name
            if not template_path.exists():
                logger.warning("沙箱模板不存在: %s", template_path)
                continue

            content = template_path.read_text(encoding="utf-8")
            content = self._strip_frontmatter(content)
            if not content.strip():
                continue

            try:
                write_fn = getattr(sandbox.files, "write_file", None)
                if callable(write_fn):
                    await write_fn(sandbox_path, content)
                else:
                    await sandbox.files.write(sandbox_path, content.encode("utf-8"))
                count += 1
                logger.info("已写入沙箱模板: user=%s, file=%s", user_id, sandbox_filename)
            except Exception as e:
                logger.warning("写入沙箱模板失败 (%s): %s", sandbox_filename, e)

        return count

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
                embedding=json.dumps(emb) if emb else None,
            )
            self.db.add(record)

        self.db.commit()
        return len(chunks)

    async def search_memory(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
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

        Returns:
            匹配的记忆片段列表
        """
        fetch_k = top_k * 3  # 多取一些用于融合

        # 1. BM25 始终执行
        bm25_results = self._search_by_bm25(user_id, query, fetch_k)

        # 2. 尝试向量检索
        vec_results: list[dict] = []
        if self._is_embedding_available():
            try:
                vec_results = await self._search_by_embedding(user_id, query, fetch_k)
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
        self, user_id: str, query: str, top_k: int
    ) -> list[dict]:
        """向量语义检索"""
        query_embedding = await self._generate_embeddings([query])
        if not query_embedding or not query_embedding[0]:
            return []

        qvec = query_embedding[0]

        all_chunks = (
            self.db.query(MemoryEmbedding)
            .filter(
                MemoryEmbedding.user_id == user_id,
                MemoryEmbedding.embedding.isnot(None),
            )
            .all()
        )

        scored = []
        for chunk in all_chunks:
            try:
                cvec = json.loads(chunk.embedding)
                score = self._cosine_similarity(qvec, cvec)
                scored.append((score, chunk))
            except (json.JSONDecodeError, TypeError):
                continue

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            {
                "file_path": item.file_path,
                "chunk_index": item.chunk_index,
                "text": item.chunk_text,
                "score": round(score, 4),
            }
            for score, item in scored[:top_k]
        ]

    def _search_by_bm25(
        self, user_id: str, query: str, top_k: int
    ) -> list[dict]:
        """BM25 关键词检索"""
        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        all_chunks = (
            self.db.query(MemoryEmbedding)
            .filter(MemoryEmbedding.user_id == user_id)
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

        根据 file_path 中的日期信息计算衰减系数。
        常青文件（MEMORY.md / USER.md 等）不衰减。

        Args:
            results: 搜索结果列表
            half_life_days: 半衰期（天），默认 30 天
        """
        if not results:
            return results

        decay_lambda = 0.693147 / half_life_days  # ln(2) / half_life
        now = datetime.now()

        EVERGREEN_KEYWORDS = ("MEMORY.md", "USER.md", "SOUL.md", "AGENTS.md", "HEARTBEAT.md")

        for r in results:
            fp = r.get("file_path", "")

            # 常青文件不衰减
            if any(kw in fp for kw in EVERGREEN_KEYWORDS):
                continue

            # 尝试从路径提取日期（如 memory/2026-03-26.md 或 conversation/.../...）
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", fp)
            if date_match:
                try:
                    file_date = datetime.strptime(date_match.group(1), "%Y-%m-%d")
                    age_days = max((now - file_date).days, 0)
                    decay = math.exp(-decay_lambda * age_days)
                    r["score"] = round(r["score"] * decay, 4)
                except ValueError:
                    pass

        # 重新排序
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # 沙箱同步
    # ------------------------------------------------------------------

    async def sync_to_sandbox(self, user_id: str, sandbox) -> int:
        """将 DB 中的记忆文件同步到沙箱

        Args:
            user_id: 用户 ID
            sandbox: OpenSandbox 实例

        Returns:
            同步的文件数量
        """
        from src.api.services.sandbox_service import get_sandbox_mount_path

        records = self.get_all_memory_files(user_id)
        if not records:
            return 0

        mount = get_sandbox_mount_path()
        synced = 0
        for file_type, content in records.items():
            filename = FILE_TYPE_TO_FILENAME.get(file_type)
            if not filename:
                continue
            path = f"{mount}/{filename}"
            try:
                write_file = getattr(sandbox.files, "write_file", None)
                if callable(write_file):
                    await write_file(path, content)
                else:
                    await sandbox.files.write(path, content.encode("utf-8"))
                synced += 1
            except Exception as e:
                logger.warning("同步记忆到沙箱失败 (%s): %s", filename, e)

        return synced

    async def sync_from_sandbox(self, user_id: str, sandbox, file_type: str) -> str | None:
        """从沙箱读取指定记忆文件并更新 DB

        Returns:
            读取到的内容，读取失败返回 None
        """
        from src.api.services.sandbox_service import get_sandbox_mount_path

        filename = FILE_TYPE_TO_FILENAME.get(file_type)
        if not filename:
            return None

        mount = get_sandbox_mount_path()
        path = f"{mount}/{filename}"
        try:
            read_file = getattr(sandbox.files, "read_file", None)
            if callable(read_file):
                content = await read_file(path)
            else:
                content = await sandbox.files.read(path)
                if isinstance(content, bytes):
                    content = content.decode("utf-8")

            if content:
                self.upsert_memory_file(user_id, file_type, content)
            return content
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
        """调用 Embedding API 生成向量

        优先从 model_registry 获取 Embedding 模型配置；
        若 model_registry 无配置，fallback 到 settings 中的旧配置。
        两者均无则对每个文本返回 None。
        """
        api_key: str = ""
        api_base: str = ""
        model_name: str = ""

        # 1. 尝试 model_registry
        try:
            from src.api.model_registry import get_model_registry
            registry = get_model_registry()
            emb_config = registry.get_embedding_model()
            if emb_config:
                api_key = emb_config.resolve_api_key()
                api_base = emb_config.api_base
                model_name = emb_config.model_name
        except Exception:
            pass

        # 2. fallback: settings
        if not api_key and settings.embedding_api_key:
            api_key = settings.embedding_api_key
            api_base = settings.embedding_api_base
            model_name = settings.embedding_model

        if not api_key:
            return [None] * len(texts)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{api_base.rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model_name,
                        "input": texts,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings = [item["embedding"] for item in data["data"]]
                return embeddings
        except Exception as e:
            logger.warning("Embedding API 调用失败: %s", e)
            return [None] * len(texts)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算余弦相似度"""
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

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
                embedding=json.dumps(emb) if emb else None,
            )
            self.db.add(record)

        self.db.commit()
        logger.info(
            "对话内容已索引: user=%s, session=%s, round=%s, chunks=%d",
            user_id, session_id, round_id, len(chunks),
        )
        return len(chunks)
