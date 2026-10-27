"""Spanish speech for explicit COP amounts; no economic rounding or currency inference."""

import re

_COP = re.compile(
    r"(?<![\w$.,+\-])\$(0|[1-9][0-9]{0,2}(?:\.[0-9]{3})*),([0-9]{2})[ \u00a0]+COP(?!\w)"
)
_SMALL = (
    "cero",
    "uno",
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
    "doce",
    "trece",
    "catorce",
    "quince",
    "dieciséis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
    "veinte",
    "veintiuno",
    "veintidós",
    "veintitrés",
    "veinticuatro",
    "veinticinco",
    "veintiséis",
    "veintisiete",
    "veintiocho",
    "veintinueve",
)
_TENS = ("", "", "", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa")
_HUNDREDS = (
    "",
    "ciento",
    "doscientos",
    "trescientos",
    "cuatrocientos",
    "quinientos",
    "seiscientos",
    "setecientos",
    "ochocientos",
    "novecientos",
)


def _apocopate(text: str) -> str:
    if text.endswith("veintiuno"):
        return text[:-9] + "veintiún"
    if text.endswith("uno"):
        return text[:-3] + "un"
    return text


def _integer(value: int) -> str:
    if value < 30:
        return _SMALL[value]
    if value < 100:
        tens, units = divmod(value, 10)
        return _TENS[tens] + (" y " + _SMALL[units] if units else "")
    if value == 100:
        return "cien"
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        return _HUNDREDS[hundreds] + (" " + _integer(rest) if rest else "")
    for scale, singular, plural in (
        (10**12, "un billón", "billones"),
        (10**6, "un millón", "millones"),
        (1000, "mil", "mil"),
    ):
        if value >= scale:
            count, rest = divmod(value, scale)
            prefix = singular if count == 1 else _apocopate(_integer(count)) + " " + plural
            return prefix + (" " + _integer(rest) if rest else "")
    raise ValueError("Unsupported integer")


def speak_cop(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digits = match[1].replace(".", "")
        if len(digits) > 18:  # numeric(20,2); leave out-of-contract text untouched.
            return match[0]
        pesos, cents = int(digits), int(match[2])
        words = _apocopate(_integer(pesos))
        noun = "peso" if pesos == 1 else "pesos"
        separator = " de " if pesos and pesos % 10**6 == 0 else " "
        result = words + separator + noun
        if cents:
            result += " con " + _apocopate(_integer(cents))
            result += " centavo" if cents == 1 else " centavos"
        return result

    return _COP.sub(replace, text)
