import ssl
from uuid import uuid4

import httpx
import pytest
import truststore
from conftest import AUTH, TOKEN
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.core.errors import BackendUnavailable
from app.main import create_app


@pytest.mark.anyio
async def test_real_transport_receives_local_verified_native_context(monkeypatch):
    contexts = []
    original_ssl_context = ssl.SSLContext

    def transport_factory(*, verify, trust_env):
        assert isinstance(verify, truststore.SSLContext)
        assert verify.protocol == ssl.PROTOCOL_TLS_CLIENT
        assert verify.verify_mode == ssl.CERT_REQUIRED
        assert verify.check_hostname is True
        assert trust_env is False
        contexts.append(verify)
        return httpx.MockTransport(lambda _: httpx.Response(200, json=AUTH))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    for _ in range(2):
        async with create_http_client("https://control.test", 7) as http:
            client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
            try:
                assert (await client.get_current_user()).is_management is True
            finally:
                client.clear_credentials()
    assert len(contexts) == 2
    assert contexts[0] is not contexts[1]
    assert ssl.SSLContext is original_ssl_context


@pytest.mark.parametrize("reason", ["self signed certificate", "hostname mismatch", "expired"])
def test_tls_errors_remain_safe_at_api_boundary(settings, monkeypatch, caplog, reason):
    requests = []

    def reject_certificate(request):
        requests.append(request)
        try:
            raise ssl.SSLCertVerificationError(f"{reason}: {TOKEN}")
        except ssl.SSLCertVerificationError as exc:
            raise httpx.ConnectError(f"TLS failure: {TOKEN}", request=request) from exc

    def transport_factory(*, verify, trust_env):
        assert isinstance(verify, truststore.SSLContext)
        assert verify.check_hostname and verify.verify_mode == ssl.CERT_REQUIRED
        return httpx.MockTransport(reject_certificate)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    caplog.set_level("DEBUG")
    with TestClient(create_app(settings)) as api:
        response = api.get(
            "/api/intelligence/session", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 503
    assert response.json()["error"] == BackendUnavailable.code
    assert TOKEN not in response.text + repr(response.headers) + caplog.text
    assert "authorization" not in requests[0].headers
