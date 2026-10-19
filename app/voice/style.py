from app.contracts.presentation import ResponseType
from app.voice.config import SPEECH_INSTRUCTIONS

_STYLES: dict[ResponseType, str] = {
    "document_analysis": (
        "Atribuye los hechos al documento, con pausas breves y sin recitar metadata."
    ),
    "direct": "Entrega breve, natural y segura.",
    "management_brief": "Entrega ejecutiva y clara, con ritmo ligeramente pausado.",
    "project_status": "Entrega informativa y natural.",
    "list": "Separa oralmente los elementos con pausas breves.",
    "comparison": "Separa claramente los elementos comparados con pausas breves.",
    "clarification": "Entrega conversacional, ligeramente interrogativa en las preguntas.",
    "unavailable": "Entrega neutra y clara, sin sonar como un error técnico.",
}


def speech_instructions(response_type: ResponseType) -> str:
    return f"{SPEECH_INSTRUCTIONS} {_STYLES[response_type]}"
