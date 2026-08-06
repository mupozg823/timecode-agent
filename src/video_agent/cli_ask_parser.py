"""Argparse surface for evidence-led questions."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from enum import StrEnum, unique

type AskCommandHandler = Callable[[argparse.Namespace], int]


@unique
class AskOutputFormat(StrEnum):
    HUMAN = "human"
    AGENT_JSON = "agent-json"


def add_ask_parser(
    sub: argparse._SubParsersAction,
    workspace_help: str,
    handler: AskCommandHandler,
) -> None:
    """Register the ask command without growing the shared parser module."""
    parser = sub.add_parser(
        "ask",
        help="answer supported questions from structured checkpoint evidence",
    )
    parser.add_argument("workspace", help=workspace_help)
    parser.add_argument("question", help="question sentence")
    parser.add_argument(
        "--lang",
        default="auto",
        help="reply locale: auto or a BCP-47 language tag",
    )
    parser.add_argument(
        "--format",
        type=AskOutputFormat,
        choices=tuple(AskOutputFormat),
        default=AskOutputFormat.HUMAN,
        help="human-readable text or compact agent JSON",
    )
    parser.set_defaults(func=handler)
