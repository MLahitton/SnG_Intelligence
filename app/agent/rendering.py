"""Conservative renderers of one shared answer; no second factual generation."""

import re
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent.prompt import MANAGEMENT_LABELS_ES

_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
_HOURS = (
    "doce",
    "una",
    "dos",
    "tres",
    "cuatro",
    "cinco",
    "seis",
    "siete",
    "ocho",
    "nueve",
    "diez",
    "once",
)
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b")
# The initial Steel & Glass pronunciation lexicon contains only the justified SG case.
SPEECH_LEXICON = {"SG": "ese ge"}
_DIGITS = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve")


def _outside_names(text: str, names: set[str], transform: Callable[[str], str]) -> str:
    if not names:
        return transform(text)
    pattern = "(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n) + ")"
    parts = re.split(pattern, text)
    return "".join(part if index % 2 else transform(part) for index, part in enumerate(parts))


def human_datetime(value: datetime, *, spoken: bool = False) -> str:
    if value.tzinfo is None:
        return "Fecha sin zona horaria disponible"
    local = value.astimezone(ZoneInfo("America/Bogota"))
    day = f"{local.day} de {_MONTHS[local.month - 1]} de {local.year}"
    if spoken and local.minute == local.second == local.microsecond == 0:
        if local.hour in (0, 12):
            return f"{day}, a {'medianoche' if local.hour == 0 else 'mediodía'}"
        period = "mañana" if local.hour < 12 else "tarde" if local.hour < 19 else "noche"
        hour = local.hour % 12
        article = "a la" if hour == 1 else "a las"
        return f"{day}, {article} {_HOURS[hour]} de la {period}"
    suffix = "a. m." if local.hour < 12 else "p. m."
    seconds = f":{local.second:02}" if local.second or local.microsecond else ""
    fraction = f".{local.microsecond:06}" if local.microsecond else ""
    return f"{day}, {local.hour % 12 or 12}:{local.minute:02}{seconds}{fraction} {suffix}"


def _dates(text: str, *, spoken: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        try:
            return human_datetime(datetime.fromisoformat(match[0]), spoken=spoken)
        except ValueError:
            return match[0]

    return _ISO.sub(replace, text)


def _labels(text: str) -> str:
    pattern = r"\b(" + "|".join(MANAGEMENT_LABELS_ES) + r")\b"
    text = re.sub(pattern, lambda match: MANAGEMENT_LABELS_ES[match[0]], text)
    text = re.sub(r"\b(?:su|el)\s+Lead\s*Time\s+está\b", "La tarea está", text, flags=re.I)
    text = re.sub(r"\bLeadTimeStatus\b", "estado del plazo", text, flags=re.I)
    return re.sub(r"\bLead\s*Time\b", "plazo", text, flags=re.I)


def normalize_speech(text: str, protected_names: set[str] | None = None) -> str:
    """Only spelling of standalone SG codes; names recovered from evidence are protected."""

    def normalize(part: str) -> str:
        return re.sub(
            r"\bSG([0-9]+)\b",
            lambda match: SPEECH_LEXICON["SG"] + " " + " ".join(_DIGITS[int(d)] for d in match[1]),
            part,
        )

    return _outside_names(text, protected_names or set(), normalize)


def render_answer(text: str, names: set[str]) -> tuple[str, str]:
    """Both views use identical source text; only bounded presentation changes differ."""
    visual = _outside_names(text, names, lambda part: _labels(_dates(part, spoken=False)))
    spoken = _outside_names(text, names, lambda part: _labels(_dates(part, spoken=True)))

    def plain(part: str) -> str:
        # Preserve digits, negative signs, proper names and content; remove only visual markers.
        part = re.sub(r"(?m)^\s*(?:[•*] |[-] (?![0-9])|#{1,6} )", "", part)
        return re.sub(r"\*\*([^*\n]+)\*\*", r"\1", part)

    spoken = _outside_names(spoken, names, plain)
    spoken = normalize_speech(spoken, names)
    return visual, spoken
