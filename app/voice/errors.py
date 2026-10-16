from app.core.errors import IntelligenceError


class VoiceNotConfigured(IntelligenceError):
    status_code = 503
    code = "voice_not_configured"


class AudioInvalid(IntelligenceError):
    status_code = 422
    code = "audio_invalid"


class AudioUnsupported(AudioInvalid):
    status_code = 415
    code = "audio_unsupported"


class AudioTooLarge(AudioInvalid):
    status_code = 413
    code = "audio_too_large"


class AudioUploadTimeout(IntelligenceError):
    status_code = 408
    code = "audio_upload_timeout"


class EmptyTranscription(AudioInvalid):
    code = "empty_transcription"


class TranscriptTooLong(AudioInvalid):
    code = "transcript_too_long"


class InvalidAudioResponse(IntelligenceError):
    status_code = 502
    code = "invalid_audio_response"
