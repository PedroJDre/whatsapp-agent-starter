"""Checks that run in code, before and after the model, so they hold no matter what the prompt says."""

import re
import unicodedata

# Any amount written with a currency sign: "$ 45.000", "$45000", "ARS 1.250.000", "45.000 pesos".
AMOUNT = re.compile(
    r"(?:\$|ars|usd)\s?(\d{1,3}(?:[.,]\d{3})+|\d{4,})|(\d{1,3}(?:[.,]\d{3})+|\d{4,})\s?(?:pesos|ars)",
    re.IGNORECASE,
)
NUMBER = re.compile(r"\d{1,3}(?:[.,]\d{3})+|\d+")

HUMAN_REQUEST = re.compile(
    r"\b(humano|una persona|persona real|asesor|asesora|operador|operadora|vendedor|vendedora|"
    r"hablar con alguien|human|real person|talk to someone|agent please|representative)\b",
    re.IGNORECASE,
)


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def wants_human(text: str) -> bool:
    """The customer asked for a person. Handled in code so the model can't talk them out of it."""
    return bool(HUMAN_REQUEST.search(_plain(text)))


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def unverified_amounts(reply: str, trusted_texts: list[str]) -> list[str]:
    """Money amounts in the reply that don't appear in any trusted text (tool outputs, earlier replies).

    The model is told never to do math. This makes sure of it: a total it made up gets blocked.
    """
    trusted = {_digits(n) for text in trusted_texts for n in NUMBER.findall(text)}
    found = []
    for match in AMOUNT.finditer(reply):
        raw = match.group(1) or match.group(2)
        if _digits(raw) not in trusted:
            found.append(match.group(0).strip())
    return found
