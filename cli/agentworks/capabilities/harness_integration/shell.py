"""The ``shell`` harness integration: run an operator-authored command (or a bare
login shell) as the session workload.

The plain, default member. Its config vocabulary is exactly
the flat session-template fields the harness integration model replaces: ``command``
(the pane command; empty = login shell), ``resume_command`` (the
command on ``session resume``, falling back to ``command``), and
``required_commands`` (the executables the launch target must have on
PATH). All optional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.harness_integration.base import HarnessIntegration, require_commands
from agentworks.schema import AgwModel

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.transports import Transport


class ShellConfig(AgwModel):
    """What a session template tells the ``shell`` integration to run.

    **Every field beyond the tag must stay optional**, and this one model
    is the only place that is true of by obligation rather than by
    accident. ``shell`` is the DEFAULT workload: a session template that
    names no integration at all resolves to it, including the reserved
    auto-declared ``default`` row, which has no config to give. A required
    field here would make every operator's config fail to load with no
    remedy available to them. Pinned by
    ``tests/test_shell_integration.py``; any other integration is free to
    require what it likes, because a template has to opt into it.

    Optional means DEFAULTED, not nullable (FR15): each field declares
    the concrete value an omitted declaration means, so the integration
    reads a string or a list rather than re-inventing "absent means
    empty" at every read.
    """

    name: Literal["shell"]
    """The harness integration this config is for."""

    command: str = ""
    """The command the session's pane runs. Empty (the default) is a bare
    login shell."""

    resume_command: str = ""
    """The command a resumed session's pane runs. Empty (the default)
    reruns ``command``."""

    required_commands: list[str] = Field(default_factory=list)
    """Commands that must exist on the session's target before it starts.
    Inherited templates UNION this list rather than replacing it, so a
    child adding one never silently drops the parent's."""


def _as_str_list(value: object) -> list[str] | None:
    """Narrow a merge-time list field: an ABSENT value is a clean empty
    list; a fully-string list passes through; anything else returns
    ``None`` (unclean). ``merge_config`` runs on raw declared blobs (the
    resolver merges before the final validate), so an unclean side must
    NOT be filtered into a valid-looking union: laundering would hide the
    bad entry from the merged-blob ``validate`` pass. The caller skips
    the union instead, leaving the raw value for ``validate`` to reject.
    """
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


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


class ShellIntegration(HarnessIntegration):
    """Runs an operator command (or a login shell) as the session."""

    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "shell"
    description: ClassVar[str] = "Run an operator command or a login shell"

    config_model: ClassVar[type[ShellConfig]] = ShellConfig

    @property
    def config(self) -> ShellConfig:
        """This session's validated shell config."""
        return self._config_as(ShellConfig)

    @classmethod
    def merge_config(cls, base: Mapping[str, object], child: Mapping[str, object]) -> dict[str, object]:
        """Same-harness integration inheritance merge (FRD R5): scalars child-win via
        the shallow default; ``required_commands`` unions append-dedupe so
        a child overriding only ``command`` never silently drops the
        parent's required commands.

        The union runs only when BOTH sides are clean lists of strings:
        the merge sees raw declared blobs, and filtering a mixed list
        into a valid-looking union would hide the invalid entry from the
        validation of the merged blob. An unclean side falls through to
        the shallow merge, so validation still rejects it."""
        merged = {**base, **child}
        base_cmds = _as_str_list(base.get("required_commands"))
        child_cmds = _as_str_list(child.get("required_commands"))
        if base_cmds is not None and child_cmds is not None:
            union = _append_dedupe(base_cmds, child_cmds)
            if union:
                merged["required_commands"] = union
        return merged

    def start(self, ctx: RunContext) -> str:
        """The pane command for ``session create``: ``command`` verbatim
        (empty = a bare login shell)."""
        return self.config.command

    def resume(self, ctx: RunContext) -> str:
        """The pane command for ``session resume``: ``resume_command``
        when declared, else ``command`` (empty = login shell).

        The remaining ``or`` is the cross-field derivation the model's
        own description states, not a fallback to a literal: an empty
        ``resume_command`` means "rerun ``command``", and ``command`` is
        already resolved by the time it is read."""
        return self.config.resume_command or self.config.command

    def _probe_target(self, transport: Transport) -> None:
        require_commands(
            tuple(self.config.required_commands),
            transport,
            harness_integration_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
