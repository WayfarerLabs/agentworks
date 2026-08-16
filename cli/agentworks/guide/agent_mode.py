"""Guide presentation mode selection from explicit and trustworthy signals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping


class GuideMode(Enum):
    HUMAN = "human"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class HarnessSignature:
    """One environment variable and exact value that identifies an agent harness."""

    variable: str
    value: str


# Claude Code sets CLAUDECODE=1 in the sessions it starts.
HARNESS_SIGNATURES = (HarnessSignature("CLAUDECODE", "1"),)


def select_guide_mode(
    explicit: Literal["agent", "human"] | None,
    environ: Mapping[str, str],
    stdout_isatty: bool,
) -> GuideMode:
    """Select explicit mode first, then exact signatures, then stdout shape."""
    if explicit is not None:
        return GuideMode(explicit)
    if any(environ.get(signature.variable) == signature.value for signature in HARNESS_SIGNATURES):
        return GuideMode.AGENT
    return GuideMode.HUMAN if stdout_isatty else GuideMode.AGENT
