"""Build disposable directory ZIPs inside the owning Session's Sandbox."""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import shlex
import uuid
from datetime import timedelta

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from opensandbox.models.execd import RunCommandOpts

from src.api.utils.sandbox_helpers import extract_command_stdout

logger = logging.getLogger(__name__)

# Explicit platform/cache names, not a blanket ban on user dotfiles.
INTERNAL_DIRECTORIES = frozenset({
    '.opencapybox', '.opencapybox-edit', '.opencapybox-preview',
    '.workspace-snapshots', '.workspace-change-sets', '.assistant-artifacts',
    '.git', '.venv', '__pycache__', 'node_modules', 'skills',
})
INTERNAL_FILES = frozenset({'.agent_memory.json'})

_ARCHIVE_SCRIPT = r'''
import errno, json, os, signal, stat, sys, zipfile

class ArchiveError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def open_directory(parent, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)

def walk(archive, directory, prefix):
    before_dir = os.fstat(directory)
    archive.writestr(prefix + '/', b'')
    for name in sorted(os.listdir(directory)):
        st = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            continue
        if name in P['internal_files'] or (stat.S_ISDIR(st.st_mode) and name in P['internal_directories']):
            continue
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            continue
        if '\\' in name:
            raise ArchiveError(400, '文件名包含不支持的路径分隔符')
        member = prefix + '/' + name
        if stat.S_ISDIR(st.st_mode):
            child = open_directory(directory, name)
            try:
                walk(archive, child, member)
            finally:
                os.close(child)
        else:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'rb') as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (st.st_dev, st.st_ino):
                    raise ArchiveError(409, '文件夹内容正在变化，请稍后重试')
                with archive.open(member, 'w', force_zip64=True) as output:
                    remaining = before.st_size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ArchiveError(409, '文件夹内容正在变化，请稍后重试')
                        output.write(chunk)
                        remaining -= len(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ArchiveError(409, '文件夹内容正在变化，请稍后重试')
    if os.fstat(directory).st_mtime_ns != before_dir.st_mtime_ns:
        raise ArchiveError(409, '文件夹内容正在变化，请稍后重试')

def timeout(*args):
    raise ArchiveError(504, '文件夹打包超时，请选择较小的子文件夹重试')

signal.signal(signal.SIGALRM, timeout)
signal.alarm(120)
directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
created = False
try:
    # Anchor every source component, including ancestors of the Session root.
    for part in (P['root'] + '/' + P['relative']).split('/'):
        if not part:
            continue
        child = open_directory(directory, part)
        os.close(directory)
        directory = child
    os.mkdir(P['temp_dir'], mode=0o700)
    created = True
    with zipfile.ZipFile(P['temp_dir'] + '/archive.zip', 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        walk(archive, directory, P['name'])
    print(json.dumps({'size': os.stat(P['temp_dir'] + '/archive.zip').st_size}))
except BaseException as error:
    signal.alarm(0)
    if created:
        try: os.unlink(P['temp_dir'] + '/archive.zip')
        except FileNotFoundError: pass
        os.rmdir(P['temp_dir'])
    if isinstance(error, ArchiveError):
        status, message = error.status, error.message
    elif isinstance(error, FileNotFoundError):
        status, message = 404, '文件夹或其中的文件已不存在，请刷新后重试'
    elif isinstance(error, OSError) and error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES):
        status, message = 403, '无法下载该目录：路径包含链接或不可访问的条目'
    else:
        status, message = 500, '文件夹打包失败，请重试'
    print(json.dumps({'error': {'status': status, 'message': message}}))
finally:
    signal.alarm(0)
    os.close(directory)
'''


def normalize_archive_path(path: str) -> str:
    path = path.replace('\\', '/')
    parts = path.split('/')
    if path.startswith('/') or '..' in parts or '\x00' in path:
        raise HTTPException(403, '目录路径越界')
    if any(part in INTERNAL_DIRECTORIES or part in INTERNAL_FILES for part in parts):
        raise HTTPException(403, '内部运行目录不能打包下载')
    return '/'.join(part for part in parts if part and part != '.')


async def _finish_before_cancel(awaitable):
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def _cleanup(sandbox, temp_dir: str):
    # This directory contains exactly our ZIP; never recursively delete a source.
    script = (
        'import os\n'
        f'p={temp_dir!r}\n'
        'try: os.unlink(p + "/archive.zip")\n'
        'except FileNotFoundError: pass\n'
        'try: os.rmdir(p)\n'
        'except FileNotFoundError: pass\n'
    )
    try:
        result = await sandbox.commands.run('python3 -c ' + shlex.quote(script))
        if getattr(result, 'error', None) or getattr(result, 'exit_code', 0):
            raise RuntimeError('ZIP cleanup command failed')
    except Exception:
        logger.warning('Could not clean directory ZIP %s', temp_dir, exc_info=True)


class DirectoryZipResponse(StreamingResponse):
    def __init__(self, upstream: httpx.Response, *, sandbox, temp_dir, headers):
        super().__init__(upstream.aiter_bytes(chunk_size=64 * 1024), media_type='application/zip', headers=headers)
        self.upstream = upstream
        self.sandbox = sandbox
        self.temp_dir = temp_dir

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _finish_before_cancel(self.close())

    async def close(self):
        try:
            close_stream = getattr(self.body_iterator, 'aclose', None)
            if close_stream is not None:
                await close_stream()
        finally:
            try:
                await self.upstream.aclose()
            finally:
                await _cleanup(self.sandbox, self.temp_dir)


async def create_directory_zip_response(sandbox, *, root: str, path: str, disposition: str):
    relative = normalize_archive_path(path)
    temp_dir = '/tmp/ocb-directory-zip-' + uuid.uuid4().hex
    parameters = {
        'root': root, 'relative': relative,
        'name': posixpath.basename(relative) or '会话文件', 'temp_dir': temp_dir,
        'internal_directories': sorted(INTERNAL_DIRECTORIES),
        'internal_files': sorted(INTERNAL_FILES),
    }
    script = 'P = ' + repr(parameters) + '\n' + _ARCHIVE_SCRIPT
    upstream = None
    try:
        result = await _finish_before_cancel(sandbox.commands.run(
            'python3 -c ' + shlex.quote(script),
            opts=RunCommandOpts(timeout=timedelta(seconds=135)),
        ))
        if getattr(result, 'error', None) or getattr(result, 'exit_code', 0):
            raise HTTPException(503, '文件夹打包失败，请重试')
        payload = json.loads(extract_command_stdout(result))
        if 'error' in payload:
            raise HTTPException(payload['error']['status'], payload['error']['message'])
        # SDK read_bytes_stream hides the response; closing its iterator does not
        # release an interrupted HTTP stream. Reuse its request/client helpers
        # (proxy URL, encoding and auth) but retain ownership of the response.
        request_data = sandbox.files._build_download_request(temp_dir + '/archive.zip', None)
        client = await sandbox.files._get_httpx_client()
        request = client.build_request('GET', **request_data)
        upstream = await client.send(request, stream=True)
        upstream.raise_for_status()
        return DirectoryZipResponse(upstream, sandbox=sandbox, temp_dir=temp_dir, headers={
            'Content-Disposition': disposition,
            'Content-Length': str(payload['size']),
            'Cache-Control': 'no-store',
        })
    except BaseException:
        try:
            if upstream is not None:
                await _finish_before_cancel(upstream.aclose())
        finally:
            await _finish_before_cancel(_cleanup(sandbox, temp_dir))
        raise
