import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from opensandbox.adapters.filesystem_adapter import FilesystemAdapter
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint

from src.api.routes import sessions
from src.api.services import session_directory_download as archive


@pytest.mark.asyncio
async def test_archive_authorization_and_path_rejection_precede_sandbox_io():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with patch.object(sessions, '_ensure_sandbox', new_callable=AsyncMock) as ensure:
        with pytest.raises(HTTPException) as missing:
            await sessions.download_session_directory('foreign-session', path='', user_id='u1', db=db)
        assert missing.value.status_code == 404
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(user_id='u1')
        for path in ('../other', '/etc', 'notes/../../other', '.workspace-snapshots/x', 'notes/.opencapybox-edit'):
            with pytest.raises(HTTPException) as forbidden:
                await sessions.download_session_directory('s1', path=path, user_id='u1', db=db)
            assert forbidden.value.status_code == 403
        ensure.assert_not_called()
    assert archive.normalize_archive_path('资料/.config') == '资料/.config'
    compile(archive._ARCHIVE_SCRIPT, '<sandbox-zip>', 'exec')


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt', ['none', 'body', 'start', 'cancel'])
async def test_archive_stream_cleanup_on_success_and_connection_failure(interrupt):
    closed = []
    content = b'x' * (64 * 1024) + b'zip tail'

    class DownloadStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield content[:64 * 1024]
            yield content[64 * 1024:]

        async def aclose(self):
            closed.append(True)

    upstream = httpx.Response(200, stream=DownloadStream())

    def download(request):
        assert request.url.path == '/sandboxes/s1/proxy/44772/files/download'
        assert request.url.params['path'].startswith('/tmp/ocb-directory-zip-')
        assert request.headers['OPEN-SANDBOX-API-KEY'] == 'test-key'
        return upstream

    files = FilesystemAdapter(
        ConnectionConfig(protocol='http', transport=httpx.MockTransport(download)),
        SandboxEndpoint(endpoint='sandbox.test/sandboxes/s1/proxy/44772', headers={'OPEN-SANDBOX-API-KEY': 'test-key'}),
    )
    sandbox = SimpleNamespace(
        commands=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout='{"size":65544}', exit_code=0))),
        files=files,
    )
    response = await archive.create_directory_zip_response(sandbox, root='/home/user/sessions/s1', path='资料', disposition=sessions.encode_filename_header('资料.zip'))
    assert response.headers['content-type'] == 'application/zip'
    assert "filename*=UTF-8''%E8%B5%84%E6%96%99.zip" in response.headers['content-disposition']
    sent = []

    async def send(message):
        if message['type'] == 'http.response.body' and interrupt == 'cancel':
            raise asyncio.CancelledError()
        if message['type'] == f'http.response.{interrupt}':
            raise OSError('connection closed')
        sent.append(message)

    async def receive():
        await asyncio.Event().wait()

    try:
        if interrupt != 'none':
            expected = asyncio.CancelledError if interrupt == 'cancel' else Exception
            with pytest.raises(expected):
                await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        else:
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
            assert b''.join(item.get('body', b'') for item in sent) == content
        assert sandbox.commands.run.await_count == 2
        assert upstream.is_closed
        assert closed == [True]
        assert 'os.unlink' in sandbox.commands.run.call_args.args[0]
        assert response.temp_dir in sandbox.commands.run.call_args.args[0]
    finally:
        await upstream.aclose()
        await (await files._get_httpx_client()).aclose()


@pytest.mark.asyncio
async def test_cancel_during_pack_waits_for_command_before_cleanup():
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def command(script, **kwargs):
        if not calls:
            calls.append('packing')
            started.set()
            await finish.wait()
            calls.append('packed')
        else:
            calls.append('cleaned')
        return SimpleNamespace(stdout='{"size":9}', exit_code=0)

    sandbox = SimpleNamespace(commands=SimpleNamespace(run=command), files=SimpleNamespace(read_bytes_stream=AsyncMock()))
    task = asyncio.create_task(archive.create_directory_zip_response(sandbox, root='/home/user/sessions/s1', path='', disposition='attachment'))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert calls == ['packing']
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ['packing', 'packed', 'cleaned']
    sandbox.files.read_bytes_stream.assert_not_called()
