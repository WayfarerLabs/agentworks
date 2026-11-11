"""Temporary aliases for released Shell commands and session env templates."""

from __future__ import annotations

from agentworks import output

LEGACY_PLACEHOLDER_REMOVAL = "2.0.0"
LEGACY_PLACEHOLDERS = {"{{session_name}}": "session_name", "{{workspace_name}}": "workspace_name"}


def uses_legacy_placeholders(text: str) -> bool:
    """Whether authored text uses one of the two released brace aliases."""
    return any(token in text for token in LEGACY_PLACEHOLDERS)


def warn_legacy_placeholders() -> None:
    """Emit value-free replacement guidance once for this CLI request."""
    output.deprecation(
        "Legacy placeholders {{session_name}} and {{workspace_name}} in Shell commands and session env "
        "are deprecated; use ${agw:session_name} and ${agw:workspace_name}. "
        f"Legacy placeholders will be removed in {LEGACY_PLACEHOLDER_REMOVAL}.",
        key="legacy-context-placeholders",
    )
