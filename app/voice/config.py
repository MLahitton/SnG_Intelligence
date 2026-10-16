import os
from dataclasses import dataclass

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_AUDIO_BYTES + 64 * 1024
MAX_SPEECH_CHARACTERS = 4000
MAX_TRANSCRIPT_CHARACTERS = 4000
MAX_SPEECH_BYTES = 10 * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = 30
TRANSCRIPTION_TIMEOUT_SECONDS = 60
SPEECH_TIMEOUT_SECONDS = 60
SPEECH_INSTRUCTIONS = (
    "Pronuncia exactamente las palabras del texto proporcionado: no añadas, omitas ni "
    "cambies palabras, nombres propios, hechos o cifras. No interpretes hechos. "
    "La interpretación es únicamente vocal: humana y conversacional, no una lectura "
    "mecánica o literal en su entonación. Conserva el idioma del texto. En español, usa "
    "español colombiano natural, con voz cálida y cercana, profesional y conversacional. "
    "Usa pausas naturales, pequeñas variaciones de ritmo, entonación orgánica, énfasis "
    "sutil y cadencia relajada. Evita sonar como asistente virtual, GPS, locutora o "
    "narración institucional. No dramatices ni imites a una persona real. Mantén un "
    "matiz colombiano natural; puede conservar un matiz paisa suave, sin forzar ni "
    "caricaturizar el acento, ni introducir modismos o cambiar el vocabulario."
)
BUILTIN_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "onyx",
        "nova",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    }
)


@dataclass(frozen=True)
class VoiceSettings:
    transcription_model: str
    speech_model: str
    voice: str

    @classmethod
    def from_env(cls) -> "VoiceSettings":
        # Optional until the corresponding endpoint is requested; no startup I/O.
        return cls(
            transcription_model=os.environ.get("SNG_INTELLIGENCE_TRANSCRIPTION_MODEL", "").strip(),
            speech_model=os.environ.get("SNG_INTELLIGENCE_SPEECH_MODEL", "").strip(),
            voice=os.environ.get("SNG_INTELLIGENCE_VOICE", "").strip(),
        )
