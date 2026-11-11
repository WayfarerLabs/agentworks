"""Pure, namespaced substitution for explicitly selected runtime context."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.linux_users import managed_home

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.env.identity import ResourceContext

_PREFIX = "${agw:"
_ESCAPE = "$${agw:"
_TOKEN_START = re.compile(r"\$\$\{agw:|\$\{agw:")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_NAMES = frozenset(
    {"user_home_dir", "user_name", "vm_name", "agent_name", "workspace_name", "workspace_dir", "session_name"}
)


def context_values(
    *,
    vm_name: str,
    user: str,
    agent_name: str | None = None,
    workspace_name: str | None = None,
    workspace_dir: str | None = None,
    session_name: str | None = None,
) -> dict[str, str]:
    """Produce the finite context catalogue from an invocation's known facts."""
    values = {"vm_name": vm_name, "user_name": user, "user_home_dir": managed_home(user)}
    for name, value in (
        ("agent_name", agent_name),
        ("workspace_name", workspace_name),
        ("workspace_dir", workspace_dir),
        ("session_name", session_name),
    ):
        if value is not None:
            values[name] = value
    return values


def runtime_values(ctx: ResourceContext) -> dict[str, str]:
    """Project the same runtime facts used for managed identity variables."""
    return context_values(
        vm_name=ctx.vm_name,
        user=ctx.user,
        agent_name=ctx.agent_name,
        workspace_name=ctx.workspace_name,
        workspace_dir=ctx.workspace_dir,
        session_name=ctx.session_name,
    )


def render_template(
    text: str,
    values: Mapping[str, str],
    *,
    field: str,
    legacy_placeholders: Mapping[str, str] | None = None,
) -> str:
    """Render operator-authored text once, interpreting the reserved prefix.

    This is the authored template boundary. Unknown, unavailable, or malformed
    Agentworks references raise a field-aware error without echoing input values.
    A consumer may explicitly supply literal legacy tokens mapped to context
    names during its compatibility window. Other shell expressions, Go templates,
    and replacement contents remain literal data.
    """
    token_start = (
        re.compile("|".join((_TOKEN_START.pattern, *(re.escape(token) for token in legacy_placeholders))))
        if legacy_placeholders
        else _TOKEN_START
    )
    chunks: list[str] = []
    cursor = 0
    while match := token_start.search(text, cursor):
        chunks.append(text[cursor : match.start()])
        if match.group() == _ESCAPE:
            chunks.append(_PREFIX)
            cursor = match.end()
            continue
        name = legacy_placeholders.get(match.group()) if legacy_placeholders else None
        if name is None:
            end = text.find("}", match.end())
            if end == -1 or _IDENTIFIER.fullmatch(text[match.end() : end]) is None:
                raise ValidationError(
                    f"{field}: malformed Agentworks placeholder",
                    entity_kind="template",
                    entity_name=field,
                    hint="Use ${agw:NAME}; write $${agw: to preserve a literal opening prefix.",
                )
            name = text[match.end() : end]
            cursor = end + 1
        else:
            cursor = match.end()
        if name not in _NAMES:
            raise ValidationError(
                f"{field}: unknown context name {name!r}",
                entity_kind="template",
                entity_name=field,
            )
        if name not in values:
            raise ValidationError(
                f"{field}: context name {name!r} is unavailable for this invocation",
                entity_kind="template",
                entity_name=field,
            )
        chunks.append(values[name])
    chunks.append(text[cursor:])
    return "".join(chunks)
