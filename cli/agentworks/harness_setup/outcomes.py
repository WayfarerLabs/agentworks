"""Operation-local setup failures reported after independent work completes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.resources.render import sanitize_fact_line

if TYPE_CHECKING:
    from collections.abc import Callable

# Remove complete terminal strings and escape sequences before the shared scalar
# boundary strips remaining controls, including truncated or malformed sequences.
_TERMINAL_SEQUENCES = re.compile(
    r"(?:\x1b[\]PX^_]|[\x90\x98\x9d-\x9f]).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*(?:[@-~]|$)|\x1b[ -/]*[@-~]",
    re.DOTALL,
)
_DIAGNOSTIC_LIMIT = 2048
_TRUNCATED = "... [truncated]"


def normalize_setup_diagnostic(text: str) -> str:
    """Normalize diagnostic text and secret patterns without truncating either."""
    text = _TERMINAL_SEQUENCES.sub("", text)
    return sanitize_fact_line(" ".join(text.split()))


def sanitize_setup_diagnostic(text: str, *, redact: Callable[[str], str] | None = None) -> str:
    """Bound a setup diagnostic to one terminal-safe line and 2 KiB of UTF-8.

    Callers can redact raw text first, then redact normalized representations
    through the callback before truncation. Render persisted older diagnostics
    through the same boundary without rewriting state.
    """
    text = normalize_setup_diagnostic(text)
    if redact is not None:
        text = redact(text)
    encoded = text.encode("utf-8")
    if len(encoded) > _DIAGNOSTIC_LIMIT:
        text = encoded[: _DIAGNOSTIC_LIMIT - len(_TRUNCATED)].decode("utf-8", errors="ignore") + _TRUNCATED
    return text


@dataclass(frozen=True)
class SetupFailure:
    """One failed integration attempt, with sanitized runtime diagnostics."""

    owner_kind: str
    owner_name: str
    facet: str
    integration: str
    reason: str
    hint: str | None = None


def describe_setup_failures(failures: list[SetupFailure]) -> str:
    """Describe ordered failures for terminal errors or prerequisite repair hints."""
    if not failures:
        return ""
    details = []
    for failure in failures:
        scope = " scope=admin" if failure.owner_kind == "vm" and failure.facet == "user" else ""
        detail = (
            f"{sanitize_setup_diagnostic(failure.owner_kind)}/{sanitize_setup_diagnostic(failure.owner_name)} "
            f"facet={sanitize_setup_diagnostic(failure.facet)}{scope} "
            f"integration={sanitize_setup_diagnostic(failure.integration)}: "
            f"{sanitize_setup_diagnostic(failure.reason)}"
        )
        if failure.hint:
            detail += f" Repair: {sanitize_setup_diagnostic(failure.hint)}"
        details.append(detail)
    return "Setup failed:\n" + "\n".join(details)


def raise_setup_failures(failures: list[SetupFailure]) -> None:
    """Fail the command once its independent setup work and state commits finish."""
    if failures:
        raise StateError(describe_setup_failures(failures))
