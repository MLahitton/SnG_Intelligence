from app.core.errors import IntelligenceError


class DocumentTooLarge(IntelligenceError):
    status_code = 413
    code = "document_too_large"


class DocumentUnavailable(IntelligenceError):
    status_code = 409
    code = "document_unavailable_or_changed"


class UnsupportedDocument(IntelligenceError):
    status_code = 422
    code = "unsupported_document_type"


class InvalidDocumentResponse(IntelligenceError):
    status_code = 502
    code = "invalid_document_analysis_response"


class DocumentAnalysisFailure(IntelligenceError):
    status_code = 502
    code = "document_analysis_failed"
