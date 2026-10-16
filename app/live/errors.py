from app.core.errors import IntelligenceError


class LiveNotConfigured(IntelligenceError):
    status_code = 503
    code = "live_not_configured"


class LiveProviderError(IntelligenceError):
    status_code = 502
    code = "live_session_creation_failed"


class LiveSessionUnavailable(IntelligenceError):
    status_code = 404
    code = "live_session_unknown_or_expired"


class LiveSessionForbidden(IntelligenceError):
    status_code = 403
    code = "live_session_forbidden"


class LiveCapacityExceeded(IntelligenceError):
    status_code = 429
    code = "live_capacity_exceeded"


class LiveCallConflict(IntelligenceError):
    status_code = 409
    code = "live_call_conflict"


class LiveBodyTooLarge(IntelligenceError):
    status_code = 413
    code = "live_request_too_large"


class LiveMediaTypeError(IntelligenceError):
    status_code = 415
    code = "live_json_required"
