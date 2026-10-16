import re
import ssl
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from urllib.request import Request

import httpx
import truststore

from app.core.errors import OutboundPolicyError

_PROJECT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PATH = re.compile(
    rf"/api/(?:auth/me|management/(?:overview|tasks)|projects/{_PROJECT}"
    r"(?:/tasks|/documents|/notifications)?)"
)


class _RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie: Cookie, request: Request) -> bool:
        return False

    def return_ok(self, cookie: Cookie, request: Request) -> bool:
        return False


class ReadOnlyTransport(httpx.AsyncBaseTransport):
    """Enforce the outbound boundary even if an internal caller builds a bad request."""

    def __init__(self, origin: str, inner: httpx.AsyncBaseTransport) -> None:
        self._origin = httpx.URL(origin)
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (
            request.method != "GET"
            or (url.scheme, url.host, url.port)
            != (self._origin.scheme, self._origin.host, self._origin.port)
            or url.userinfo
            or url.query
            or url.fragment
            or not _PATH.fullmatch(url.raw_path.decode("ascii"))
        ):
            raise OutboundPolicyError()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def create_http_client(
    origin: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    if transport is None:
        # Scope native certificate trust to this transport, without global SSL injection.
        ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        transport = httpx.AsyncHTTPTransport(verify=ssl_context, trust_env=False)
    return httpx.AsyncClient(
        base_url=origin,
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
        cookies=CookieJar(policy=_RejectCookies()),
        transport=ReadOnlyTransport(origin, transport),
    )
