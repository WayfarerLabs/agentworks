"""The ``shell`` harness: run an operator-authored command (or a bare
login shell) as the session workload.

The plain, default member. Its ``harness_config`` vocabulary is exactly
the flat session-template fields the harness model replaces: ``command``
(the pane command; empty = login shell), ``restart_command`` (the
command on ``session restart``, falling back to ``command``), and
``required_commands`` (the executables the launch target must have on
PATH). All optional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from agentworks.capabilities.harness.base import Harness, require_commands
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.resources.reference import ConfigReference
    from agentworks.transports import Transport

_SHELL_FIELDS = {"command", "restart_command", "required_commands"}


def _as_str_list(value: object) -> list[str]:
    """Narrow a merged-blob field to a list of strings. ``validate_config``
    has already enforced the shape at load, so a non-list is treated as
    absent (empty) rather than re-raising here."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _append_dedupe(target: list[str], source: list[str]) -> list[str]:
    """Append source items to target, skipping dupes. Preserves order.

    A copy of the per-domain merge helper (``sessions/templates.py``,
    ``agents/templates.py`` each carry their own): the capability layer
    may not import a consuming domain (FRD R1), so the trivial utility
    is copied here as it is elsewhere rather than shared across the
    boundary.
    """
    seen = set(target)
    result = list(target)
    for item in source:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


class ShellHarness(Harness):
    """Runs an operator command (or a login shell) as the session."""

    name: ClassVar[str] = "shell"
    description: ClassVar[str] = "Run an operator command or a login shell"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        """Shape-and-vocabulary only (FRD R2/R4): unknown fields raise;
        each present field is type-checked. Implies no resource
        reference, so it returns ``()``. Completeness (there is none for
        ``shell``) would run on the merged blob at resolve; this call
        fires per declared blob, where a restating child may be partial.
        """
        unknown = sorted(set(config) - _SHELL_FIELDS)
        if unknown:
            raise ConfigError(
                f"{owner}: unknown shell harness field(s): "
                f"{', '.join(unknown)}"
            )
        for field_name in ("command", "restart_command"):
            value = config.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ConfigError(
                    f"{owner}.{field_name} must be a string"
                )
        required = config.get("required_commands")
        if required is not None and (
            not isinstance(required, list)
            or not all(isinstance(item, str) for item in required)
        ):
            raise ConfigError(
                f"{owner}.required_commands must be a list of strings"
            )
        return ()

    @classmethod
    def merge_config(
        cls, base: Mapping[str, object], child: Mapping[str, object]
    ) -> dict[str, object]:
        """Same-harness inheritance merge (FRD R5): scalars child-win via
        the shallow default; ``required_commands`` unions append-dedupe so
        a child overriding only ``command`` never silently drops the
        parent's required commands."""
        merged = {**base, **child}
        union = _append_dedupe(
            _as_str_list(base.get("required_commands")),
            _as_str_list(child.get("required_commands")),
        )
        if union:
            merged["required_commands"] = union
        return merged

    def start(self, ctx: RunContext) -> str:
        """The pane command for ``session create``: ``command`` verbatim,
        empty string when undeclared (a bare login shell)."""
        return self._command_field("command")

    def restart(self, ctx: RunContext) -> str:
        """The pane command for ``session restart``: ``restart_command``
        when declared, else ``command`` (empty = login shell)."""
        restart_command = self._command_field("restart_command")
        return restart_command or self._command_field("command")

    def _command_field(self, field_name: str) -> str:
        value = self.config.get(field_name, "")
        return value if isinstance(value, str) else ""

    def _probe_target(self, transport: Transport) -> None:
        require_commands(
            tuple(_as_str_list(self.config.get("required_commands"))),
            transport,
            harness_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
