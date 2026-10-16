from app.contracts.base import BackendContract


class CurrentUser(BackendContract):
    subject: str
    email: str | None
    name: str | None
    is_management: bool
