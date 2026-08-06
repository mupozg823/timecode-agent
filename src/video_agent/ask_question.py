"""Conservative routing for the supported deterministic question."""

from __future__ import annotations

import re
from typing import Final

SUPPORTED_QUESTION_EN: Final = (
    "how many times did the seated man leave the screen?"
)
SUPPORTED_SUBJECT_EN: Final = "seated man"
SUPPORTED_QUESTION_KO: Final = (
    "앉아 있던 남자가 화면 밖으로 몇 번 나갔나요?"
)


class UnsupportedAskQuestionError(ValueError):
    """The narrow deterministic question router has no matching model."""


_SEATED_MAN_CUE: Final = re.compile(
    r"(?:앉아\s*(?:있던|있는)|앉은)\s*"
    r"(?:남자|남성)(?:이|가|은|는|을|를|의)?"
    r"(?=\s|[,.!?]|$)"
)
_MULTIPLE_SUBJECT_CUE: Final = re.compile(
    r"(?:남자|남성)\s*(?:들|와|과|랑|하고|및)|(?:여자|여성)"
)
_COUNT_CUE: Final = re.compile(r"몇\s*(?:번|차례)|횟수")
_COUNT_BETWEEN_ACTION: Final = r"(?:(?:총\s*)?몇\s*(?:번|차례)\s*)?"
_DEPARTURE_CUE: Final = (
    r"(?:나(?:가|간|갔)|사라(?:지|진|졌)|"
    r"벗어(?:나|난|났)|떠(?:나|난|났)|이탈)"
)
_SCREEN_DEPARTURE_CUE: Final = re.compile(
    rf"화면\s*(?:"
    rf"밖(?:으로|에)?\s*{_COUNT_BETWEEN_ACTION}{_DEPARTURE_CUE}"
    rf"|에서\s*{_COUNT_BETWEEN_ACTION}{_DEPARTURE_CUE}"
    rf"|을\s*{_COUNT_BETWEEN_ACTION}{_DEPARTURE_CUE}"
    rf"|이탈"
    rf")"
)
_COUNT_BEFORE_ACTION_GAP: Final = re.compile(r"\s*(?:총\s*)?")
_COUNT_AFTER_ACTION_GAP: Final = re.compile(
    r"\s*(?:(?:건|것은|게)\s*)?(?:총\s*)?"
)


def is_supported_subject(subject: str) -> bool:
    """Return whether an observation names the one supported subject."""
    normalized = " ".join(subject.strip().casefold().split())
    return (
        normalized == SUPPORTED_SUBJECT_EN
        or _SEATED_MAN_CUE.fullmatch(normalized) is not None
    )


def _count_is_bound_to_departure(
    question: str,
    departure: re.Match[str],
    count: re.Match[str],
) -> bool:
    if departure.start() <= count.start() and count.end() <= departure.end():
        return True
    if count.end() <= departure.start():
        gap = question[count.end() : departure.start()]
        return _COUNT_BEFORE_ACTION_GAP.fullmatch(gap) is not None
    if departure.end() <= count.start():
        gap = question[departure.end() : count.start()]
        return _COUNT_AFTER_ACTION_GAP.fullmatch(gap) is not None
    return False


def question_support_error(question: str) -> str | None:
    """Return a Korean diagnostic unless the narrow count intent is explicit."""
    normalized = " ".join(question.strip().casefold().split())
    if normalized == SUPPORTED_QUESTION_EN:
        return None

    departure = _SCREEN_DEPARTURE_CUE.search(normalized)
    count = _COUNT_CUE.search(normalized)
    issues: list[str] = []
    if (
        _MULTIPLE_SUBJECT_CUE.search(normalized) is not None
        or _SEATED_MAN_CUE.search(normalized) is None
    ):
        issues.append("대상이 '앉아 있던 한 명의 남자'인지 분명하지 않습니다.")
    if departure is None:
        issues.append("'화면 밖으로 나감' 표현이 분명하지 않습니다.")
    if count is None:
        issues.append("횟수를 묻는 표현이 없습니다.")
    elif departure is not None and not _count_is_bound_to_departure(
        normalized,
        departure,
        count,
    ):
        issues.append("횟수 표현이 화면 이탈과 직접 연결되지 않았습니다.")
    if not issues:
        return None
    return (
        "지원하지 않는 질문이거나 의미가 모호한 질문입니다. "
        f"{' '.join(issues)} 지원 예: {SUPPORTED_QUESTION_KO} / "
        f"{SUPPORTED_QUESTION_EN}"
    )
