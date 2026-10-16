import asyncio
from collections.abc import AsyncGenerator

from fastapi import Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.voice.config import MAX_AUDIO_BYTES, MAX_MULTIPART_BYTES, UPLOAD_TIMEOUT_SECONDS
from app.voice.contracts import AudioInput
from app.voice.errors import AudioInvalid, AudioTooLarge, AudioUnsupported, AudioUploadTimeout

# Filename, MIME and a container signature must agree. This is not codec decoding.
_FORMATS = {
    "webm": ("audio/webm", {"audio/webm", "video/webm"}),
    "wav": ("audio/wav", {"audio/wav", "audio/x-wav", "audio/wave"}),
    "mp3": ("audio/mpeg", {"audio/mpeg", "audio/mp3"}),
    "m4a": ("audio/mp4", {"audio/mp4", "audio/m4a", "audio/x-m4a", "video/mp4"}),
    "ogg": ("audio/ogg", {"audio/ogg", "application/ogg"}),
}


def has_audio_signature(data: bytes, extension: str) -> bool:
    match extension:
        case "webm":
            return data.startswith(b"\x1a\x45\xdf\xa3")
        case "wav":
            return data.startswith(b"RIFF") and data[8:12] == b"WAVE"
        case "mp3":
            return data.startswith(b"ID3") or (
                len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
            )
        case "m4a":
            return len(data) >= 12 and data[4:8] == b"ftyp"
        case "ogg":
            return data.startswith(b"OggS")
        case _:
            return False


class _MemoryMultipartParser(MultiPartParser):
    # The stream limit is lower than the spool threshold: rollover cannot occur.
    spool_max_size = MAX_MULTIPART_BYTES + 1
    complete = False

    def on_end(self) -> None:
        self.complete = True


async def _bounded_stream(request: Request) -> AsyncGenerator[bytes, None]:
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_MULTIPART_BYTES:
            raise AudioTooLarge()
        yield chunk


async def read_audio(request: Request) -> AudioInput:
    """Called after management auth, never via FastAPI's eager File parsing."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "multipart/form-data":
        raise AudioUnsupported()
    lengths = request.headers.getlist("content-length")
    if lengths:
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise AudioInvalid()
        if len(lengths[0]) > 10 or int(lengths[0]) > MAX_MULTIPART_BYTES:
            raise AudioTooLarge()
    parser = _MemoryMultipartParser(
        request.headers, _bounded_stream(request), max_files=1, max_fields=0
    )
    try:
        async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
            form = await parser.parse()
            items = form.multi_items()
            if not parser.complete or len(items) != 1 or items[0][0] != "file":
                raise AudioInvalid()
            file = items[0][1]
            if not isinstance(file, UploadFile):
                raise AudioInvalid()
            extension = (file.filename or "").rsplit(".", 1)[-1].lower()
            if extension not in _FORMATS:
                raise AudioUnsupported()
            mime, allowed = _FORMATS[extension]
            declared = (file.content_type or "").split(";", 1)[0].strip().lower()
            if declared and declared not in allowed:
                raise AudioUnsupported()
            content = await file.read(MAX_AUDIO_BYTES + 1)
            if len(content) > MAX_AUDIO_BYTES:
                raise AudioTooLarge()
            if not content or not has_audio_signature(content, extension):
                raise AudioInvalid()
            request.state.audio_size_bytes = len(content)
            # Discard user-supplied filenames/headers; only canonical metadata crosses STT.
            return AudioInput(content, extension, mime)
    except MultiPartException:
        error: AudioInvalid | AudioUploadTimeout = AudioInvalid()
    except TimeoutError:
        error = AudioUploadTimeout()
    finally:
        # Also close incomplete uploads on success, disconnect, cancellation and parse failures.
        for upload in parser._files_to_close_on_error:
            upload.close()
    raise error
