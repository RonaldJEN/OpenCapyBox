"""Session editor baselines and CAS saves, without Workspace history/ledger rows."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import posixpath
import re
import shlex
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from src.api.config import get_settings
from src.api.services.workspace_auto_merge import merge_workspace_bytes
from src.api.utils.sandbox_helpers import extract_command_stdout

logger = logging.getLogger(__name__)


# Every snapshot is an independent immutable copy. Locks cover only one file;
# no DB transaction remains open while these scripts run in OpenSandbox.
_SCRIPT = r'''
import fcntl, hashlib, json, os, stat, sys, time, uuid

def fail(status, code, message):
    print(json.dumps({'error': {'status': status, 'code': code, 'message': message}}))
    sys.exit(0)

def directory(root_fd, path, create=False):
    fd = os.dup(root_fd)
    try:
        for part in path.split('/'):
            if not part:
                continue
            if create:
                try: os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError: pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise

def read_file(parent, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode): fail(400, 'SESSION_EDIT_NOT_FILE', '目标不是普通文件')
        data = source.read(limit + 1)
        if len(data) > limit: fail(413, 'SESSION_EDIT_TOO_LARGE', '文件超过在线编辑大小限制')
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            fail(409, 'SESSION_EDIT_RETRY', '文件正在更新，稍后自动重试')
        return data, before

def metadata(data, st):
    return {'size': len(data), 'mtime_ns': st.st_mtime_ns, 'sha256': hashlib.sha256(data).hexdigest()}

def atomic_write(parent, name, data, mode=0o600):
    temp = '.' + uuid.uuid4().hex + '.tmp'
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try: os.unlink(temp, dir_fd=parent)
        except FileNotFoundError: pass

def cache(data):
    sha = hashlib.sha256(data).hexdigest()
    try:
        existing = os.stat(sha, dir_fd=bases, follow_symlinks=False)
        if not stat.S_ISREG(existing.st_mode) or existing.st_size != len(data):
            fail(409, 'SESSION_EDIT_BASE_INVALID', '编辑基线内容异常')
    except FileNotFoundError:
        atomic_write(bases, sha, data, 0o400)
    os.utime(sha, None, dir_fd=bases, follow_symlinks=False)
    return sha

def prune(protected):
    # Bounded, disposable Session editing cache, not a permanent version history.
    now = time.time()
    for folder, budget in ((bases, P['cache_bytes']), (receipts, 4 * 1024 * 1024)):
        rows = []
        for name in os.listdir(folder):
            if len(name) != 64 or any(c not in '0123456789abcdef' for c in name): continue
            st = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode): rows.append((st.st_mtime, name, st.st_size))
        total = sum(row[2] for row in rows)
        for accessed, name, size in sorted(rows):
            if name in protected: continue
            if now - accessed < P['retention_seconds'] and total <= budget: continue
            try: os.unlink(name, dir_fd=folder)
            except FileNotFoundError: pass
            total -= size

root = os.open(P['root'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
opened = [root]
try:
    edit = directory(root, '.opencapybox-edit', True); opened.append(edit)
    bases = directory(edit, 'bases', True); opened.append(bases)
    receipts = directory(edit, 'receipts', True); opened.append(receipts)
    locks = directory(edit, 'locks', True); opened.append(locks)
    lock = os.open(P['lock_key'], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=locks)
    opened.append(lock)
    fcntl.flock(lock, fcntl.LOCK_EX)
    receipt_key = P.get('receipt_key')
    previous = None
    if receipt_key:
        try:
            previous = json.loads(read_file(receipts, receipt_key, 8192)[0])
        except FileNotFoundError: pass
        if previous and previous.get('fingerprint') != P['fingerprint']:
            fail(409, 'SESSION_EDIT_KEY_REUSED', '保存标识已用于另一份草稿')
    if previous:
        result = previous
        result['replayed'] = True
    elif P['action'] == 'base':
        data, _st = read_file(bases, P['base']['sha256'], P['max_bytes'])
        if len(data) != P['base']['size'] or hashlib.sha256(data).hexdigest() != P['base']['sha256']:
            fail(409, 'SESSION_EDIT_BASE_INVALID', '编辑基线校验失败')
        result = P['base']
    else:
        parent_path, name = os.path.split(P['path'])
        parent = directory(root, parent_path); opened.append(parent)
        current, st = read_file(parent, name, P['max_bytes'])
        result = metadata(current, st)
        if P['action'] == 'install':
            if result != P['expected']:
                fail(409, 'SESSION_EDIT_RETRY', '文件在合并期间更新，稍后自动重试')
            content, _temp_stat = read_file(edit, P['temp'], P['max_bytes'])
            if hashlib.sha256(content).hexdigest() != P['content_sha']:
                fail(409, 'SESSION_EDIT_CONTENT_INVALID', '保存内容校验失败')
            cache(content)
            if content != current:
                os.replace(P['temp'], name, src_dir_fd=edit, dst_dir_fd=parent)
                os.fsync(parent)
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            result = {**metadata(content, st), 'auto_merged': P['auto_merged'], 'fingerprint': P['fingerprint']}
            if receipt_key:
                atomic_write(receipts, receipt_key, json.dumps(result).encode('utf-8'))
        else:
            cache(current)
    try:
        prune({result['sha256'], P.get('protected_base'), receipt_key})
    except OSError:
        pass  # A cache cleanup failure cannot deny an already committed save.
    print(json.dumps(result))
except FileNotFoundError:
    if P['action'] == 'base': fail(409, 'SESSION_EDIT_BASE_UNAVAILABLE', '编辑基线已过期或不可用，草稿仍保留')
    fail(404, 'SESSION_FILE_NOT_FOUND', '会话文件已不存在')
except OSError:
    fail(503, 'SESSION_EDIT_IO_ERROR', '会话文件暂时无法读写')
finally:
    for fd in reversed(opened): os.close(fd)
'''


class SessionFileEditService:
    def __init__(self, sandbox, *, root: str, user_id: str, session_id: str, path: str, max_bytes: int):
        self.sandbox = sandbox
        self.root = root
        self.path = path
        self.max_bytes = max_bytes
        if path.startswith('/') or '\\' in path or '\x00' in path or any(p.startswith('.') or not p for p in path.split('/')):
            raise HTTPException(400, detail={'code': 'SESSION_EDIT_INVALID_PATH', 'message': '该文件路径不可编辑'})
        self.file_key = hashlib.sha256(f'{user_id}\0{session_id}\0{path}'.encode()).hexdigest()
        self.lock_key = hashlib.sha256(posixpath.join(root, path).encode()).hexdigest()
        settings = get_settings()
        self.secret = settings.auth_secret_key.encode()
        self.cache_bytes = int(settings.workspace_preview_cache_bytes)
        self.retention_seconds = int(settings.workspace_draft_base_retention_days) * 86400

    def token(self, value: dict) -> str:
        body = f"{value['size']}.{value['mtime_ns']}.{value['sha256']}"
        signature = hmac.new(self.secret, f'{self.file_key}\0{body}'.encode(), hashlib.sha256).hexdigest()
        return body + '.' + signature

    def decode_token(self, token: str) -> dict:
        match = re.fullmatch(r'(\d+)\.(\d+)\.([0-9a-f]{64})\.([0-9a-f]{64})', token)
        if not match:
            raise HTTPException(409, detail={'code': 'SESSION_EDIT_BASE_INVALID', 'message': '编辑基线无效'})
        value = {'size': int(match[1]), 'mtime_ns': int(match[2]), 'sha256': match[3]}
        if value['size'] > self.max_bytes or not hmac.compare_digest(self.token(value), token):
            raise HTTPException(409, detail={'code': 'SESSION_EDIT_BASE_INVALID', 'message': '编辑基线不属于当前文件'})
        return value

    async def _run(self, action: str, **kwargs) -> dict:
        params = dict(action=action, root=self.root, path=self.path, lock_key=self.lock_key,
                      max_bytes=self.max_bytes, cache_bytes=self.cache_bytes,
                      retention_seconds=self.retention_seconds, **kwargs)
        result = await self.sandbox.commands.run('python3 -c ' + shlex.quote('P=' + repr(params) + '\n' + _SCRIPT))
        if getattr(result, 'exit_code', None) != 0:
            raise HTTPException(503, detail={'code': 'SESSION_EDIT_IO_ERROR', 'message': '会话文件操作失败'})
        value = json.loads(extract_command_stdout(result))
        if 'error' in value:
            error = value['error']
            raise HTTPException(error.pop('status'), detail=error)
        return value

    def cache_path(self, value: dict) -> str:
        return posixpath.join(self.root, '.opencapybox-edit', 'bases', value['sha256'])

    def file_info(self, value: dict) -> dict:
        return dict(name=posixpath.basename(self.path), path=self.path, size=value['size'],
                    modified=datetime.fromtimestamp(value['mtime_ns'] / 1e9, timezone.utc).isoformat(),
                    revision=f"v1:{value['size']}:{value['mtime_ns']}", type=posixpath.splitext(self.path)[1].lstrip('.'),
                    edit_base_token=self.token(value), session_auto_merged=bool(value.get('auto_merged')))

    async def open(self, base_token: str | None = None) -> tuple[dict, str]:
        value = await self._run('base', base=self.decode_token(base_token)) if base_token else await self._run('snapshot')
        return self.file_info(value), self.cache_path(value)

    async def _read(self, value: dict) -> bytes:
        stream = await self.sandbox.files.read_bytes_stream(self.cache_path(value), chunk_size=64 * 1024)
        content = bytearray()
        async for chunk in stream:
            content.extend(chunk)
            if len(content) > self.max_bytes:
                raise HTTPException(413, detail={'code': 'SESSION_EDIT_TOO_LARGE', 'message': '编辑内容超过限制'})
        data = bytes(content)
        if len(data) != value['size'] or hashlib.sha256(data).hexdigest() != value['sha256']:
            raise HTTPException(409, detail={'code': 'SESSION_EDIT_BASE_INVALID', 'message': '编辑基线校验失败'})
        return data

    async def save(self, content: bytes, *, base_token: str, save_id: str) -> dict:
        # A closing browser must not interrupt upload/commit/receipt halfway.
        task = asyncio.create_task(self._save(content, base_token=base_token, save_id=save_id))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    async def _save(self, content: bytes, *, base_token: str, save_id: str) -> dict:
        base = self.decode_token(base_token)
        fingerprint = hashlib.sha256((base_token + '\0').encode() + content).hexdigest()
        receipt_key = hashlib.sha256(f'{self.file_key}\0{save_id}'.encode()).hexdigest()
        common = dict(receipt_key=receipt_key, fingerprint=fingerprint, protected_base=base['sha256'])
        temp = '.' + uuid.uuid4().hex + '.tmp'
        try:
            # Short bounded CAS retries; never rerun an Agent or lock a Session.
            for _attempt in range(3):
                current = await self._run('snapshot', **common)
                if current.get('replayed'):
                    return self.file_info(current)
                merged = content
                auto_merged = current['sha256'] != base['sha256']
                if auto_merged:
                    await self._run('base', base=base, protected_base=current['sha256'])
                    base_bytes, current_bytes = await asyncio.gather(self._read(base), self._read(current))
                    result = await asyncio.to_thread(merge_workspace_bytes, self.path,
                                                     base=base_bytes, current=content, proposal=current_bytes)
                    if result is None:
                        raise HTTPException(409, detail={'code': 'SESSION_EDIT_MERGE_UNSUPPORTED', 'message': '文件结构无法安全合并，草稿仍保留'})
                    merged = result.content
                if len(merged) > self.max_bytes:
                    raise HTTPException(413, detail={'code': 'SESSION_EDIT_TOO_LARGE', 'message': '合并后文件超过在线编辑限制'})
                await self.sandbox.files.write_file(posixpath.join(self.root, '.opencapybox-edit', temp), merged)
                try:
                    installed = await self._run('install', temp=temp, expected=current,
                                                content_sha=hashlib.sha256(merged).hexdigest(),
                                                auto_merged=auto_merged, **common)
                    return self.file_info(installed)
                except HTTPException as exc:
                    if not isinstance(exc.detail, dict) or exc.detail.get('code') != 'SESSION_EDIT_RETRY':
                        raise
            raise HTTPException(409, detail={'code': 'SESSION_EDIT_RETRY', 'message': '文件正在更新，稍后自动重试'})
        finally:
            # Cancellation must not orphan uploaded editor data.
            task = asyncio.create_task(self.sandbox.commands.run('rm -f -- ' + shlex.quote(posixpath.join(self.root, '.opencapybox-edit', temp))))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            except Exception:
                logger.warning('Session editor temp cleanup deferred path=%s', self.path, exc_info=True)
