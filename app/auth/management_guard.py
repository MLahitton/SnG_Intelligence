import re

from pydantic import SecretStr

from app.clients.sng_control import SnGControlClient
from app.contracts.auth import CurrentUser
from app.core.errors import AuthenticationRequired, ManagementRequired

_BEARER = re.compile(r"(?i:Bearer) ([A-Za-z0-9._~+/-]+=*)", re.ASCII)


def extract_bearer(headers: list[str]) -> SecretStr:
    if len(headers) != 1:
        raise AuthenticationRequired()
    match = _BEARER.fullmatch(headers[0])
    if match is None:
        raise AuthenticationRequired()
    return SecretStr(match.group(1))


async def require_management(client: SnGControlClient) -> CurrentUser:
    user = await client.get_current_user()
    if user.is_management is not True:
        raise ManagementRequired()
    return user
