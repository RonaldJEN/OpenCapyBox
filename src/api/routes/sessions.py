"""会话管理 API"""
import logging
import base64 as b64_mod
import inspect
import json
import mimetypes
import os
import posixpath
import re as _re
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session as DBSession
from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.models.session import Session
from src.api.models.round import Round
from src.api.models.agui_event import AGUIEventLog
from src.api.schemas.session import CreateSessionResponse, SessionResponse, SessionListResponse, SessionPollResponse, FileListResponse, FileInfo, UpdateSessionTitleRequest
from src.api.schemas.chat import HistoryResponseV2
from src.api.services.sandbox_service import (
    get_sandbox_service,
    resolve_sandbox_path,
    to_sandbox_relative_path,
    is_within_sandbox_root,
)
from src.api.services.history_service import HistoryService
from src.api.services.agent_service import AgentService
from src.api.model_registry import get_model_registry
from src.api.models.user_sandbox import UserSandbox
from src.api.models.conversation_message import ConversationMessage
from datetime import datetime
from src.api.utils.timezone import now_naive
from src.api.config import get_settings
from opensandbox.models.filesystem import SearchEntry
import shlex

logger = logging.getLogger(__name__)
import uuid
from urllib.parse import quote

router = APIRouter()

# 使用 AgentPoolService 管理 Agent 實例
from src.api.services.agent_pool_service import get_agent_pool


def encode_filename_header(filename: str, disposition: str = "attachment") -> str:
    """
    生成符合 RFC 5987 标准的 Content-Disposition header
    支持中文等非 ASCII 字符

    Args:
        filename: 文件名
        disposition: "attachment" 或 "inline"

    Returns:
        编码后的 Content-Disposition header 值
    """
    # 对文件名进行 URL 编码
    encoded_filename = quote(filename, safe='')

    # 使用 RFC 5987 格式：filename*=UTF-8''encoded_name
    # 同时提供 ASCII fallback
    ascii_filename = filename.encode('ascii', 'ignore').decode('ascii') or 'download'

    return f'{disposition}; filename="{ascii_filename}"; filename*=UTF-8\'\'{encoded_filename}'


def _command_stdout_text(execution) -> str:
    logs = getattr(execution, "logs", None)
    stdout_lines = getattr(logs, "stdout", None)
    if stdout_lines:
        chunks = []
        for line in stdout_lines:
            chunks.append(getattr(line, "text", str(line)))
        return "\n".join(chunks).strip()
    direct_stdout = getattr(execution, "stdout", None)
    if isinstance(direct_stdout, str):
        return direct_stdout.strip()
    return ""


def _extract_exit_code(execution) -> int:
    """安全地從 Execution 對象提取 exit_code（兼容不同 SDK 版本）"""
    exit_code = getattr(execution, "exit_code", None)
    if isinstance(exit_code, int):
        return exit_code
    return 1 if getattr(execution, "error", None) else 0


def _contains_non_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


async def _read_bytes_via_command(sandbox, sandbox_path: str) -> bytes | None:
    """通過沙箱命令（base64）讀取文件，完全繞過 files API proxy。"""
    try:
        cmd_result = await sandbox.commands.run(
            f"base64 -w0 {shlex.quote(sandbox_path)}"
        )
        stdout_text = _command_stdout_text(cmd_result)
        exit_code = _extract_exit_code(cmd_result)
        if exit_code != 0 or not stdout_text:
            logger.warning("命令 base64 讀取失敗 (exit=%s): %s", exit_code, sandbox_path)
            return None
        return b64_mod.b64decode(stdout_text)
    except Exception as e:
        logger.warning("命令 base64 讀取異常: %s — %s", sandbox_path, e)
        return None


async def _read_bytes_via_ascii_alias(sandbox, sandbox_path: str) -> bytes | None:
    """當 SDK files API 對非 ASCII 路徑不穩時，先複製到 ASCII 臨時路徑再讀取。

    回退順序：cp → read_bytes → base64 命令讀取。
    """
    alias_path = f"/tmp/agent_download_{uuid.uuid4().hex}"
    try:
        copy_result = await sandbox.commands.run(
            f"cp {shlex.quote(sandbox_path)} {shlex.quote(alias_path)}"
        )
        if _extract_exit_code(copy_result) != 0:
            logger.warning("ASCII 別名 cp 失敗: %s -> %s", sandbox_path, alias_path)
            return None

        # 嘗試 SDK read_bytes
        try:
            return await sandbox.files.read_bytes(alias_path)
        except Exception as e:
            logger.warning("ASCII 別名 read_bytes 也失敗，改用命令讀取: %s — %s", alias_path, e)

        # SDK 也不行時，用命令讀取
        return await _read_bytes_via_command(sandbox, alias_path)
    except Exception as e:
        logger.warning("ASCII 別名回退失敗: %s -> %s — %s", sandbox_path, alias_path, e)
        return None
    finally:
        try:
            await sandbox.commands.run(f"rm -f {shlex.quote(alias_path)}")
        except Exception:
            pass


def _sanitize_filename(raw: str) -> str:
    """清洗文件名：去除空格、括號等 sandbox API 不相容的字符。

    保留 Unicode 字母/數字（含中文）、底線、連字號和點號。
    連續底線會被合併為一個；前後底線會被去掉。
    """
    raw = raw.strip()
    if not raw:
        return "uploaded_file"

    base, ext = posixpath.splitext(raw)
    # 非 word / 非連字號 / 非點號 → 底線
    cleaned = _re.sub(r"[^\w\-.]", "_", base)
    # 連續底線合併
    cleaned = _re.sub(r"_+", "_", cleaned)
    cleaned = cleaned.strip("_")

    if not cleaned:
        return "uploaded_file" + ext if ext else "uploaded_file"
    return cleaned + ext


def _should_skip_sandbox_path(path: str) -> bool:
    """判斷是否應跳過該沙箱路徑（系統/依賴/記憶檔）。"""
    if not path:
        return True
    skip_tokens = (
        "/node_modules/",
        "/__pycache__/",
        "/.git/",
        "/skills/",
        "/.venv/",
    )
    if any(token in path for token in skip_tokens):
        return True
    return path.endswith("/.agent_memory.json")


def _build_fileinfo_from_path(path: str, root_path: str) -> FileInfo | None:
    if _should_skip_sandbox_path(path):
        return None

    rel_path = to_sandbox_relative_path(path, root_path)
    if not rel_path or rel_path.endswith("/"):
        return None

    name = rel_path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1] if "." in name else "unknown"
    return FileInfo(
        name=name,
        path=rel_path,
        size=0,
        modified=datetime.utcnow().isoformat(),
        type=ext,
    )


def _get_user_sandbox_id(db: DBSession, user_id: str) -> str | None:
    """從 UserSandbox 表查詢用戶的 sandbox_id。"""
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    return user_sandbox.sandbox_id if user_sandbox else None


async def _sandbox_list_files(sandbox, session_root: str) -> list[FileInfo]:
    """從沙箱獲取文件列表。

    當 sandbox_use_server_proxy=True 時直接使用 find 命令（避免 proxy
    丟棄 GET query params 導致 files.search 400 的已知問題）；否則先嘗試
    SDK files.search，失敗再回退到 find。
    """
    settings = get_settings()

    # --- 嘗試 SDK files.search（僅非 proxy 模式） ---
    if not settings.sandbox_use_server_proxy:
        try:
            entries = await sandbox.files.search(
                SearchEntry(path=session_root, pattern="**")
            )
            files: list[FileInfo] = []
            for entry in entries:
                full_path = getattr(entry, "path", "")
                if _should_skip_sandbox_path(full_path):
                    continue
                rel_path = to_sandbox_relative_path(full_path, session_root)
                if not rel_path or rel_path.endswith("/"):
                    continue
                name = rel_path.rsplit("/", 1)[-1]
                ext = name.rsplit(".", 1)[-1] if "." in name else "unknown"
                modified_at = getattr(entry, "modified_at", None)
                if hasattr(modified_at, "isoformat"):
                    modified = modified_at.isoformat()
                elif isinstance(modified_at, str):
                    modified = modified_at
                else:
                    modified = datetime.utcnow().isoformat()
                files.append(
                    FileInfo(
                        name=name,
                        path=rel_path,
                        size=int(getattr(entry, "size", 0) or 0),
                        modified=modified,
                        type=ext,
                    )
                )
            files.sort(key=lambda f: f.modified, reverse=True)
            return files
        except Exception as e:
            logger.debug("files.search 不可用，回退到 find 命令: %s", e)

    # --- 命令回退（proxy 模式直接走這裡） ---
    # 優先用 python 輸出 JSON（含 size / mtime），避免文件列表永遠顯示 0KB
    py_cmd = f"""python3 - <<'PY'\nimport os, json\nroot = {session_root!r}\nout = []\nfor cur, _, names in os.walk(root):\n    for name in names:\n        path = os.path.join(cur, name)\n        try:\n            st = os.stat(path)\n        except OSError:\n            continue\n        out.append({{\"path\": path, \"size\": int(st.st_size), \"mtime\": float(st.st_mtime)}})\nprint(json.dumps(out, ensure_ascii=False))\nPY"""
    result = await sandbox.commands.run(py_cmd)
    stdout_text = _command_stdout_text(result)
    files: list[FileInfo] = []

    try:
        rows = json.loads(stdout_text) if stdout_text else []
        if isinstance(rows, list):
            for row in rows:
                full_path = str(row.get("path", ""))
                if _should_skip_sandbox_path(full_path):
                    continue
                rel_path = to_sandbox_relative_path(full_path, session_root)
                if not rel_path or rel_path.endswith("/"):
                    continue
                name = rel_path.rsplit("/", 1)[-1]
                ext = name.rsplit(".", 1)[-1] if "." in name else "unknown"
                mtime = row.get("mtime")
                try:
                    modified = datetime.utcfromtimestamp(float(mtime)).isoformat()
                except Exception:
                    modified = datetime.utcnow().isoformat()
                files.append(
                    FileInfo(
                        name=name,
                        path=rel_path,
                        size=int(row.get("size", 0) or 0),
                        modified=modified,
                        type=ext,
                    )
                )
            files.sort(key=lambda f: f.modified, reverse=True)
            return files
    except Exception as e:
        logger.debug("JSON 文件列表回退失敗，改用 find 純路徑模式: %s", e)

    # 最終回退：find 純路徑（舊行為）
    result = await sandbox.commands.run(
        f"find {shlex.quote(session_root)} -type f 2>/dev/null"
    )
    lines = [
        line.strip()
        for line in _command_stdout_text(result).splitlines()
        if line.strip()
    ]
    for line in lines:
        info = _build_fileinfo_from_path(line, session_root)
        if info:
            files.append(info)
    files.sort(key=lambda f: f.modified, reverse=True)
    return files


@router.post("/create", response_model=CreateSessionResponse)
async def create_session(
    user_id: str = Depends(get_current_user),
    model_id: str = Query(None, description="Model ID from Model Registry (optional, uses default if not specified)"),
    db: DBSession = Depends(get_db),
):
    """创建新会话

    Args:
        user_id: 用戶 ID
        model_id: 模型 ID（可選，不傳則使用 models.yaml 中的 default_model）
    """
    # 解析 model_id：驗證存在且啟用
    resolved_model_id = None
    try:
        registry = get_model_registry()
        if model_id:
            # 前端指定了模型 → 驗證
            config = registry.get_or_raise(model_id)
            resolved_model_id = config.id
        else:
            # 未指定 → 使用默認模型
            config = registry.get_default()
            resolved_model_id = config.id
    except (FileNotFoundError, ValueError) as e:
        # Registry 不可用：允許 session 建立（向後兼容），但不記錄 model_id
        logger.warning("Model Registry 不可用 (%s)，使用 .env 全局配置", e)

    # 创建会话
    chat_session_id = str(uuid.uuid4())
    session = Session(
        id=chat_session_id, user_id=user_id, title="新会话",
        model_id=resolved_model_id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # 🔥 創建沙箱 + 初始化 Agent
    try:
        logger.info("正在為新會話創建沙箱和初始化 Agent (session=%s, model=%s)", chat_session_id, resolved_model_id)

        # 從 UserSandbox 查找該用戶現有 sandbox_id（用於 resume）
        existing_sandbox_id = _get_user_sandbox_id(db, user_id)

        # 使用 AgentPoolService 管理 Agent 實例（內含沙箱創建）
        agent_pool = get_agent_pool()
        await agent_pool.get_or_create(
            user_id=user_id,
            session_id=user_id,
            chat_session_id=chat_session_id,
            db=db,
            model_id=resolved_model_id,
            sandbox_id=existing_sandbox_id,
        )
        
        logger.info("沙箱和 Agent 初始化成功 (session=%s)", chat_session_id)
    except Exception as e:
        # 即使沙箱創建失敗，允許會話創建成功
        logger.error(
            "沙箱/Agent 初始化失敗 (session=%s): %s: %s",
            chat_session_id, type(e).__name__, e,
            exc_info=True,
        )
        logger.warning("沙箱/Agent 初始化失敗，但會話已創建。將在第一次發消息時重試。")

    return CreateSessionResponse(
        session_id=chat_session_id,
        model_id=resolved_model_id,
        message="会话创建成功"
    )


@router.get("/list", response_model=SessionListResponse)
async def list_sessions(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """获取用户的会话列表"""
    sessions = (
        db.query(Session)
        .filter(Session.user_id == user_id)
        .order_by(Session.updated_at.desc())
        .all()
    )

    return SessionListResponse(sessions=sessions)


@router.get("/{chat_session_id}/poll", response_model=SessionPollResponse)
async def poll_session(
    chat_session_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """轻量级轮询：返回会话的轮次数量，供前端检测新消息"""
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    round_count = db.query(Round).filter(Round.session_id == chat_session_id).count()
    return SessionPollResponse(round_count=round_count)


@router.get("/{chat_session_id}/history/v2", response_model=HistoryResponseV2)
async def get_session_history_v2(
    chat_session_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """获取会话的轮次历史（V2 版本，基于 Round/Step）"""
    # 验证会话属于该用户
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 获取轮次历史
    history_service = HistoryService(db)
    rounds = history_service.get_session_rounds(chat_session_id)

    return HistoryResponseV2(
        session_id=chat_session_id,
        rounds=rounds,
        total=len(rounds),
    )


@router.patch("/{chat_session_id}/title", response_model=SessionResponse)
async def update_session_title(
    chat_session_id: str,
    request: UpdateSessionTitleRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """更新会话标题"""
    # 验证会话属于该用户
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 更新标题
    session.title = request.title
    session.updated_at = now_naive()
    db.commit()
    db.refresh(session)

    logger.info("会话标题已更新: %s -> %s", chat_session_id, request.title)

    return session


@router.delete("/{chat_session_id}")
async def delete_session(
    chat_session_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """删除会话"""
    # 验证会话属于该用户
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 使用 AgentPoolService 清理 agent 缓存
    agent_pool = get_agent_pool()
    agent_pool.remove(chat_session_id)
    logger.info("已清理 Agent 缓存: %s", chat_session_id)

    # 沙箱屬於用戶，刪除 session 時不 kill 沙箱，只清理 session 子目錄
    user_id = session.user_id
    sandbox_service = get_sandbox_service()
    sandbox = sandbox_service.get_cached(user_id)
    if sandbox:
        from src.api.services.sandbox_service import get_sandbox_mount_path
        import shlex as _shlex
        session_dir = f"{get_sandbox_mount_path()}/sessions/{chat_session_id}"
        try:
            await sandbox.commands.run(
                f"rm -rf {_shlex.quote(session_dir)} 2>/dev/null || true"
            )
            logger.info("已清理 session 子目錄: %s", session_dir)
        except Exception as e:
            logger.warning("清理 session 子目錄失敗: %s, 錯誤: %s", session_dir, e)

    # 刪除會話相關數據（Round -> AGUIEventLog -> ConversationMessage）
    round_ids = [r.id for r in db.query(Round.id).filter(Round.session_id == chat_session_id).all()]
    if round_ids:
        db.query(AGUIEventLog).filter(AGUIEventLog.run_id.in_(round_ids)).delete(synchronize_session=False)
    db.query(Round).filter(Round.session_id == chat_session_id).delete(synchronize_session=False)
    db.query(ConversationMessage).filter(ConversationMessage.session_id == chat_session_id).delete(synchronize_session=False)

    # 刪除会话
    db.delete(session)
    db.commit()

    return {"message": "会话已删除"}


@router.get("/{chat_session_id}/files", response_model=FileListResponse)
async def get_session_files(
    chat_session_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """获取会话的文件列表（from 沙箱）"""
    # 验证会话属于该用户
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 從用戶沙箱獲取 session 子目錄的文件列表
    user_id = session.user_id
    sandbox_service = get_sandbox_service()
    mount_path = sandbox_service.get_mount_path()
    session_root = f"{mount_path}/sessions/{chat_session_id}"
    sandbox = sandbox_service.get_cached(user_id)

    if not sandbox:
        # 嘗試從 UserSandbox 表恢復沙箱
        try:
            sandbox = await sandbox_service.get_or_resume(user_id, _get_user_sandbox_id(db, user_id))
        except Exception as e:
            logger.warning("無法連接沙箱獲取文件列表: %s", e)
            return FileListResponse(files=[], total=0)

    try:
        files = await _sandbox_list_files(sandbox, session_root)
        return FileListResponse(files=files, total=len(files))
    except Exception as e:
        logger.warning("從沙箱獲取文件列表失敗: %s", e)
        return FileListResponse(files=[], total=0)


@router.get("/{chat_session_id}/files/{file_path:path}")
async def download_file(
    chat_session_id: str,
    file_path: str,
    user_id: str = Depends(get_current_user),
    preview: bool = Query(False, description="是否预览模式（inline）"),
    db: DBSession = Depends(get_db),
):
    """下载或预览沙箱中的文件（代理模式）

    Args:
        preview: True 表示内联预览，False 表示强制下载
    """
    logger.debug(f"文件{'\u9884\u89c8' if preview else '\u4e0b\u8f7d'}请求: session={chat_session_id}, path={file_path}")

    # 验证会话属于该用户
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 獲取用戶沙箱，文件路徑基於 session 子目錄
    user_id = session.user_id
    sandbox_service = get_sandbox_service()
    mount_path = sandbox_service.get_mount_path()
    session_root = f"{mount_path}/sessions/{chat_session_id}"
    sandbox = sandbox_service.get_cached(user_id)

    if not sandbox:
        try:
            sandbox = await sandbox_service.get_or_resume(user_id, _get_user_sandbox_id(db, user_id))
        except Exception as e:
            logger.warning("無法連接沙箱下載文件: %s", e)
            raise HTTPException(status_code=503, detail="沙箱不可用")

    # 構建並校驗沙箱中的完整路徑
    sandbox_path = resolve_sandbox_path(file_path, session_root)
    if not is_within_sandbox_root(sandbox_path, session_root):
        raise HTTPException(status_code=400, detail="文件路径不合法")

    # 確定文件名和 MIME 類型
    filename = posixpath.basename(sandbox_path)
    mime_type, _ = mimetypes.guess_type(filename)

    # 可預覽的類型
    previewable_types = {'text/', 'image/', 'application/pdf', 'application/json', 'application/xml'}
    can_preview = preview and mime_type and any(
        mime_type.startswith(prefix) for prefix in previewable_types
    )

    disposition = "inline" if can_preview else "attachment"
    cd_header = encode_filename_header(filename, disposition)

    headers = {"Content-Disposition": cd_header}

    # --- 嘗試讀取文件 ---
    file_bytes: bytes | None = None
    has_non_ascii = _contains_non_ascii(sandbox_path)

    # 非 ASCII 路徑（中文等）：proxy 必定 500，跳過 SDK API 直接走別名/命令回退
    if has_non_ascii:
        logger.debug("非 ASCII 路徑，跳過 SDK API 直接走回退: %s", sandbox_path)
        file_bytes = await _read_bytes_via_ascii_alias(sandbox, sandbox_path)
        if file_bytes is None:
            file_bytes = await _read_bytes_via_command(sandbox, sandbox_path)
    else:
        # ASCII 路徑：正常嘗試 SDK API
        # 1) 流式讀取（SDK read_bytes_stream）
        read_bytes_stream = getattr(sandbox.files, "read_bytes_stream", None)
        if callable(read_bytes_stream):
            try:
                if inspect.iscoroutinefunction(read_bytes_stream):
                    stream = await read_bytes_stream(sandbox_path, chunk_size=64 * 1024)
                else:
                    stream = read_bytes_stream(sandbox_path, chunk_size=64 * 1024)
                return StreamingResponse(
                    stream,
                    media_type=mime_type or "application/octet-stream",
                    headers=headers,
                )
            except Exception as e:
                logger.warning("流式讀取失敗: %s — %s", sandbox_path, e)

        # 2) 一次性讀取（SDK read_bytes）
        try:
            file_bytes = await sandbox.files.read_bytes(sandbox_path)
        except Exception as e:
            logger.warning("files API 讀取失敗: %s — %s", sandbox_path, e)

        # 3) 命令回退：直接用命令讀取（繞過 files API proxy）
        if file_bytes is None:
            file_bytes = await _read_bytes_via_command(sandbox, sandbox_path)

    if file_bytes is None:
        logger.warning("所有回退方式均失敗: %s", sandbox_path)
        raise HTTPException(status_code=404, detail="文件不存在或無法讀取")

    return Response(
        content=file_bytes,
        media_type=mime_type or "application/octet-stream",
        headers=headers,
    )


@router.get("/running-session")
async def get_running_session(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """检查用户是否有运行中的会话（单次 API 调用，避免 N+1 查询）

    Returns:
        running_session_id: 运行中会话的 ID，如果没有则为 null
        round_id: 运行中轮次的 ID
    """
    from src.api.models.round import Round

    # 获取用户所有会话
    sessions = (
        db.query(Session)
        .filter(Session.user_id == user_id)
        .all()
    )

    session_ids = [s.id for s in sessions]
    if not session_ids:
        return {"running_session_id": None, "round_id": None}

    # 查找运行中的轮次（单次查询）
    running_round = (
        db.query(Round)
        .filter(Round.session_id.in_(session_ids), Round.status == "running")
        .first()
    )

    if running_round:
        return {
            "running_session_id": running_round.session_id,
            "round_id": running_round.id,
        }

    return {"running_session_id": None, "round_id": None}


@router.post("/{chat_session_id}/upload")
async def upload_file(
    chat_session_id: str,
    file: UploadFile | None = File(None),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """上传文件到沙箱工作空间

    Args:
        chat_session_id: 会话ID
        file: 上传的文件
        user_id: 用户ID

    Returns:
        文件信息 (名称、路径、大小等)
    """
    if file is None:
        raise HTTPException(status_code=400, detail="未选择文件")

    logger.info(f"文件上传: session={chat_session_id}, file={file.filename}, user={user_id}")

    # 验证会话属于该用户
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 獲取用戶沙箱，文件上傳到 session 子目錄
    user_id = session.user_id
    sandbox_service = get_sandbox_service()
    mount_path = sandbox_service.get_mount_path()
    session_root = f"{mount_path}/sessions/{chat_session_id}"
    sandbox = sandbox_service.get_cached(user_id)

    if not sandbox:
        try:
            sandbox = await sandbox_service.get_or_resume(user_id, _get_user_sandbox_id(db, user_id))
        except Exception as e:
            logger.warning("無法連接沙箱上傳文件: %s", e)
            raise HTTPException(status_code=503, detail="沙箱不可用")

    # 確保 session 子目錄存在
    try:
        await sandbox.commands.run(f"mkdir -p {shlex.quote(session_root)}")
    except Exception:
        pass

    # 安全的文件名處理（防止路径遍历 + 清洗特殊字符）
    raw_filename = os.path.basename(file.filename or "uploaded_file")
    safe_filename = _sanitize_filename(raw_filename)
    sandbox_path = resolve_sandbox_path(safe_filename, session_root)

    # 檢查是否已存在同名文件，若存在則加序號
    try:
        check_result = await sandbox.commands.run(
            f"test -f {shlex.quote(sandbox_path)} && echo 'EXISTS' || echo 'NOT_EXISTS'"
        )
        if _command_stdout_text(check_result) == "EXISTS":
            base_name, ext = posixpath.splitext(safe_filename)
            counter = 1
            while True:
                new_name = f"{base_name}_{counter}{ext}"
                sandbox_path = resolve_sandbox_path(new_name, session_root)
                check_result = await sandbox.commands.run(
                    f"test -f {shlex.quote(sandbox_path)} && echo 'EXISTS' || echo 'NOT_EXISTS'"
                )
                if _command_stdout_text(check_result) != "EXISTS":
                    safe_filename = new_name
                    break
                counter += 1
    except Exception:
        pass  # 如果檢查失敗，直接覆蓋

    # 讀取上傳文件內容並寫入沙箱
    try:
        content = await file.read()
        write = getattr(sandbox.files, "write", None)
        if callable(write):
            await write(sandbox_path, content)
        else:
            await sandbox.files.write_file(sandbox_path, content)

        file_info = FileInfo(
            name=safe_filename,
            path=safe_filename,  # 相對路徑
            size=len(content),
            modified=datetime.utcnow().isoformat(),
            type=file.content_type or "application/octet-stream",
        )

        logger.info(f"文件上傳至沙箱成功: {safe_filename} ({len(content)} bytes)")
        return file_info

    except Exception as e:
        logger.error(f"文件上傳至沙箱失敗: {str(e)}")
        raise HTTPException(status_code=500, detail=f"文件保存失敗: {str(e)}")
