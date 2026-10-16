class IntelligenceError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class AuthenticationRequired(IntelligenceError):
    status_code = 401
    code = "authentication_required"


class ManagementRequired(IntelligenceError):
    status_code = 403
    code = "management_required"


class ResourceNotFound(IntelligenceError):
    status_code = 404
    code = "resource_not_found"


class InvalidArguments(IntelligenceError):
    status_code = 422
    code = "invalid_arguments"


class BackendUnavailable(IntelligenceError):
    status_code = 503
    code = "backend_unavailable"


class BackendTimeout(IntelligenceError):
    status_code = 504
    code = "backend_timeout"


class BackendResponseError(IntelligenceError):
    status_code = 502
    code = "invalid_backend_response"


class OutboundPolicyError(IntelligenceError):
    code = "outbound_policy_violation"
