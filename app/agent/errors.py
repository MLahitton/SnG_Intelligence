from app.core.errors import IntelligenceError


class AgentUnavailable(IntelligenceError):
    status_code = 503
    code = "agent_not_configured"


class ProviderTimeout(IntelligenceError):
    status_code = 504
    code = "provider_timeout"


class ProviderUnavailable(IntelligenceError):
    status_code = 503
    code = "provider_unavailable"


class ProviderConfigurationError(ProviderUnavailable):
    code = "provider_configuration_error"


class ProviderRateLimited(ProviderUnavailable):
    code = "provider_rate_limited"


class InvalidModelResponse(IntelligenceError):
    status_code = 502
    code = "invalid_model_response"


class InvalidToolCall(InvalidModelResponse):
    code = "invalid_tool_call"


class UngroundedReference(InvalidModelResponse):
    code = "ungrounded_reference"


class AgentLimitExceeded(IntelligenceError):
    status_code = 503
    code = "agent_limit_exceeded"


class ToolResultTooLarge(IntelligenceError):
    status_code = 503
    code = "tool_result_too_large"


class SensitiveContent(IntelligenceError):
    status_code = 422
    code = "sensitive_content_rejected"
