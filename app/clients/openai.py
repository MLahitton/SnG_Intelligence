import logging
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import openai
import truststore
from pydantic import SecretStr


@asynccontextmanager
async def openai_client(
    key: SecretStr, request_timeout_seconds: float
) -> AsyncIterator[openai.AsyncOpenAI]:
    """Request-scoped SDK lifecycle; no provider content or headers in logs."""
    sdk_logger = logging.getLogger("openai")
    if not sdk_logger.handlers:
        sdk_logger.addHandler(logging.NullHandler())
    sdk_logger.propagate = False
    sdk_logger.setLevel(logging.CRITICAL)
    tls = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    async with httpx.AsyncClient(
        verify=tls, trust_env=False, follow_redirects=False, timeout=request_timeout_seconds
    ) as http:
        async with openai.AsyncOpenAI(
            api_key=key.get_secret_value(),
            base_url="https://api.openai.com/v1",
            http_client=http,
            timeout=request_timeout_seconds,
            max_retries=0,
        ) as client:
            yield client
