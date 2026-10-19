import json
import ssl

import pytest
from conftest import TOKEN
from pydantic import SecretStr, ValidationError
from test_live import KEY, SESSION
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from app.agent.errors import ProviderConfigurationError, ProviderRateLimited, ProviderTimeout
from app.live.errors import LiveAttachError
from app.live.sideband_transport import OpenAISidebandConnector, _NoRedirectConnect


@pytest.mark.anyio
async def test_attach_exact_url_tls_auth_no_start_and_safe_transport(monkeypatch):
    class Socket:
        request = None
        sent = []
        closed = False

        async def send(self, value):
            self.sent.append(value)

        async def recv(self):
            return '{"type":"session.closed"}'

        async def close(self):
            self.closed = True

    socket = Socket()

    async def connect(url, **kwargs):
        assert url == f"wss://api.openai.com/v1/live/sessions/{SESSION}/attach"
        assert kwargs["additional_headers"] == {"Authorization": f"Bearer {KEY}"}
        assert TOKEN not in str(kwargs)
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl"].check_hostname and kwargs["proxy"] is None
        assert kwargs["logger"].disabled
        assert kwargs["max_size"] == 256 * 1024 and kwargs["max_queue"] == 8
        return socket

    monkeypatch.setattr("app.live.sideband_transport._NoRedirectConnect", connect)
    transport = await OpenAISidebandConnector(SecretStr(KEY)).attach(SESSION)
    assert not socket.sent  # No session.start or invented readiness command.
    await transport.send({"type": "session.close"})
    assert json.loads(socket.sent[0]) == {"type": "session.close"}
    assert await transport.receive() == '{"type":"session.closed"}'
    await transport.close()
    assert socket.closed


@pytest.mark.parametrize(
    "status,error",
    [
        (400, ProviderConfigurationError),
        (401, ProviderConfigurationError),
        (403, ProviderConfigurationError),
        (404, ProviderConfigurationError),
        (429, ProviderRateLimited),
        (500, LiveAttachError),
        (503, LiveAttachError),
        (302, LiveAttachError),
    ],
)
@pytest.mark.anyio
async def test_attach_safe_status_mapping(monkeypatch, status, error):
    async def connect(*args, **kwargs):
        raise InvalidStatus(Response(status, KEY + TOKEN, Headers(), body=bytearray(KEY.encode())))

    monkeypatch.setattr("app.live.sideband_transport._NoRedirectConnect", connect)
    with pytest.raises(error) as caught:
        await OpenAISidebandConnector(SecretStr(KEY)).attach(SESSION)
    assert TOKEN not in str(caught.value) and KEY not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "failure,error", [(TimeoutError, ProviderTimeout), (OSError, LiveAttachError)]
)
@pytest.mark.anyio
async def test_attach_timeout_and_unavailable(monkeypatch, failure, error):
    async def connect(*args, **kwargs):
        raise failure(KEY + TOKEN)

    monkeypatch.setattr("app.live.sideband_transport._NoRedirectConnect", connect)
    with pytest.raises(error) as caught:
        await OpenAISidebandConnector(SecretStr(KEY)).attach(SESSION)
    assert caught.value.__context__ is None


@pytest.mark.anyio
async def test_rejects_arbitrary_attach_target():
    with pytest.raises(ValidationError):
        await OpenAISidebandConnector(SecretStr(KEY)).attach("https://evil.test")


def test_redirect_is_never_followed():
    error = InvalidStatus(Response(302, "redirect", Headers({"Location": "wss://evil.test"})))
    assert _NoRedirectConnect.process_redirect(None, error) is error
