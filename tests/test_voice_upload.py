import asyncio
import tempfile

import pytest
import starlette.formparsers
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_voice import MP3, voice_app

from app.voice import upload
from app.voice.errors import AudioInvalid, AudioTooLarge, AudioUploadTimeout

pytestmark = pytest.mark.anyio


def multipart(data=MP3, *, name="file", final=True):
    start = (
        f'--bound\r\nContent-Disposition: form-data; name="{name}"; filename="test.mp3"\r\n'
        "Content-Type: audio/mpeg\r\n\r\n"
    ).encode()
    return start + data + (b"\r\n--bound--\r\n" if final else b"")


def request_for(chunks, *, content_length=None, content_type="multipart/form-data; boundary=bound"):
    iterator = iter(chunks)

    async def receive():
        try:
            chunk = next(iterator)
            return {"type": "http.request", "body": chunk, "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    headers = [(b"content-type", content_type.encode())]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    return Request({"type": "http", "headers": headers}, receive)


@pytest.fixture
def memory_spools(monkeypatch):
    spools = []
    original = tempfile.SpooledTemporaryFile

    class MemorySpool(original):
        def rollover(self):
            pytest.fail("Audio must never roll over to disk")

    def tracked(*args, **kwargs):
        spool = MemorySpool(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tracked)
    return spools


async def test_large_valid_upload_stays_in_memory_and_closes(memory_spools):
    data = MP3 + b"x" * (2 * 1024 * 1024)  # Beyond Starlette's default 1 MiB spool threshold.
    body = multipart(data)
    result = await upload.read_audio(
        request_for([body[i : i + 65536] for i in range(0, len(body), 65536)])
    )
    assert result.content == data
    assert len(memory_spools) == 1 and memory_spools[0].closed


@pytest.mark.parametrize("length", [None, "1"])
async def test_chunked_or_false_content_length_cannot_bypass_limit(
    monkeypatch, memory_spools, length
):
    monkeypatch.setattr(upload, "MAX_MULTIPART_BYTES", 512)
    body = multipart(MP3 + b"x" * 1024)
    with pytest.raises(AudioTooLarge):
        await upload.read_audio(request_for([body[:256], body[256:]], content_length=length))
    assert memory_spools and all(f.closed for f in memory_spools)


@pytest.mark.parametrize(
    "body", [multipart(final=False), multipart(name="other"), b"--bound--\r\n", b"malformed"]
)
async def test_malformed_uploads_close_memory_and_return_safe_error(memory_spools, body):
    with pytest.raises(AudioInvalid) as error:
        await upload.read_audio(request_for([body]))
    assert all(f.closed for f in memory_spools)
    assert error.value.__context__ is None


async def test_extra_file_and_fields_are_rejected(memory_spools):
    first = multipart()[: -len(b"--bound--\r\n")]
    for second in [
        multipart(),
        b'--bound\r\nContent-Disposition: form-data; name="role"\r\n\r\nmanager\r\n--bound--\r\n',
    ]:
        with pytest.raises(AudioInvalid):
            await upload.read_audio(request_for([first + second]))
    assert all(f.closed for f in memory_spools)


@pytest.mark.parametrize(
    "length,error",
    [("99999999999999", AudioTooLarge), ("-1", AudioInvalid), ("invalid", AudioInvalid)],
)
async def test_invalid_content_length_rejected_before_read(memory_spools, length, error):
    with pytest.raises(error):
        await upload.read_audio(request_for([multipart()], content_length=length))
    assert not memory_spools


@pytest.mark.parametrize("cancel", [True, False])
async def test_cancelled_or_timed_out_upload_closes_spool(monkeypatch, memory_spools, cancel):
    monkeypatch.setattr(upload, "UPLOAD_TIMEOUT_SECONDS", 0.02 if not cancel else 30)
    ready = asyncio.Event()
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": multipart(final=False), "more_body": True}
        ready.set()
        await asyncio.Event().wait()

    req = Request(
        {"type": "http", "headers": [(b"content-type", b"multipart/form-data; boundary=bound")]},
        receive,
    )
    task = asyncio.create_task(upload.read_audio(req))
    await ready.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else AudioUploadTimeout):
        await task
    assert memory_spools and all(f.closed for f in memory_spools)


def test_authentication_precedes_upload_parsing(settings, mock_backend, monkeypatch):
    async def forbidden(_):
        pytest.fail("An unauthenticated request must not be parsed")

    from app.api import voice

    monkeypatch.setattr(voice, "read_audio", forbidden)
    transport, calls = mock_backend
    app, stt, _, _ = voice_app(settings, transport)
    with TestClient(app) as api:
        response = api.post(
            "/api/intelligence/voice",
            content=b"invalid audio",
            headers={"Content-Type": "multipart/form-data; boundary=bound"},
        )
    assert response.status_code == 401
    assert not stt.inputs and not calls
