"""The ``shell`` harness integration: run an operator-authored command (or a bare
login shell) as the session workload.

The plain, default member. Its config vocabulary is exactly
the flat session-template fields the harness integration model replaces: ``command``
(the pane command; empty = login shell), ``resume_command`` (the
command on ordinary session start/restart, falling back to ``command``), and
``required_commands`` (the executables the launch target must have on
PATH). All optional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.harness_integration.base import (
    HarnessIntegration,
    HarnessLaunchIntent,
    HarnessStart,
    require_commands,
)
from agentworks.schema import AgwModel
from agentworks.topics import TopicProse

if TYPE_CHECKING:
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

    command: str = Field(default="", examples=["htop"])
    """The command the session's pane runs. Empty (the default) is a bare
    login shell."""

    resume_command: str = ""
    """The command a resumed session's pane runs. Empty (the default)
    reruns ``command``."""

    required_commands: list[str] = Field(default_factory=list, examples=[["htop"]])
    """Commands that must exist on the session's target before it starts.
    Inheritance combines parent and child entries."""


class ShellIntegration(HarnessIntegration):
    """Runs an operator command (or a login shell) as the session."""

    contract_version: ClassVar[int] = 3
    name: ClassVar[str] = "shell"
    description: ClassVar[str] = "Run an operator command or a login shell"
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Shell sessions",
        overview="""
        Runs whatever you tell it to. With no `command`, the session is a bare login
        shell, which is what a session-template that selects no integration gets.

        `resume_command` is what ordinary `agw session start` and `restart` run, falling back to
        `command` when it is empty. That pair is enough to drive a harness with no
        dedicated integration of its own: launch it one way, reattach another. A real
        integration is more robust (it knows whether a session exists to resume), but
        the shell escape hatch is always there.

        `required_commands` are checked on the target before the session starts, which
        turns a missing binary into a clear message instead of a pane that dies
        immediately.
        """,
    )

    config_model: ClassVar[type[ShellConfig]] = ShellConfig

    @property
    def config(self) -> ShellConfig:
        """This session's validated shell config."""
        return self._config_as(ShellConfig)

    def start(
        self,
        ctx: RunContext,
        *,
        intent: HarnessLaunchIntent = HarnessLaunchIntent.CONTINUE,
    ) -> HarnessStart:
        """Select continuation by default and ``command`` for a fresh launch.

        The remaining ``or`` is the cross-field derivation the model's
        own description states, not a fallback to a literal: an empty
        ``resume_command`` means "rerun ``command``", and ``command`` is
        already resolved by the time it is read."""
        command = self.config.command if intent.starts_fresh else self.config.resume_command or self.config.command
        return HarnessStart(command)

    def _probe_target(self, transport: Transport) -> None:
        require_commands(
            tuple(self.config.required_commands),
            transport,
            harness_integration_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
