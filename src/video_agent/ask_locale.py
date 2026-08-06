"""Locale parsing for human and agent ask projections."""

from __future__ import annotations

import re
from enum import StrEnum, unique
from typing import Final

from .ask_types import ReplyLocale

_KOREAN_TEXT: Final = re.compile(r"[\u3131-\u318e\uac00-\ud7a3]")
_LANGUAGE_TAG: Final = re.compile(
    r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*"
)


class InvalidReplyLocaleError(ValueError):
    raw: str

    def __init__(self, raw: str) -> None:
        self.raw = raw
        super().__init__(raw)

    def __str__(self) -> str:
        return (
            f"invalid locale {self.raw!r}: expected auto or a BCP-47 "
            "language tag"
        )


class UnsupportedHumanLocaleError(ValueError):
    locale: ReplyLocale

    def __init__(self, locale: ReplyLocale) -> None:
        self.locale = locale
        super().__init__(locale)

    def __str__(self) -> str:
        return (
            f"human output does not support locale {self.locale!r}; "
            "use ko, en, or --format agent-json for host-LLM localization"
        )


@unique
class HumanLocale(StrEnum):
    KOREAN = "ko"
    ENGLISH = "en"


_HUMAN_LOCALES: Final = {
    ReplyLocale(locale.value): locale for locale in HumanLocale
}


def normalize_reply_locale(raw: str, question: str) -> ReplyLocale:
    """Parse an explicit language tag or infer Korean versus English."""
    normalized = raw.strip()
    if normalized.casefold() == "auto":
        return ReplyLocale("ko" if _KOREAN_TEXT.search(question) else "en")
    if _LANGUAGE_TAG.fullmatch(normalized) is None:
        raise InvalidReplyLocaleError(raw=raw)
    return ReplyLocale(normalized.split("-", 1)[0].lower())


def parse_human_locale(locale: ReplyLocale) -> HumanLocale:
    """Parse a reply locale into one of the built-in human renderers."""
    try:
        return _HUMAN_LOCALES[locale]
    except KeyError:
        raise UnsupportedHumanLocaleError(locale=locale) from None
