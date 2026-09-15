"""Run an operator-authored command or a bare login shell as the session workload."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.native.common import defer
from agentworks.artifacts.native.shell import shell_artifacts
from agentworks.capabilities.harness_integration.base import (
    HarnessIntegration,
    HarnessLaunchIntent,
    HarnessStart,
    HarnessStartNotImplemented,
    HarnessStartResult,
    require_commands,
)
from agentworks.schema import AgwModel, MergeStrategy
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.descriptor import Facet
    from agentworks.capabilities.harness_integration.setup import (
        UserSetupInvocation,
        VMSetupInvocation,
        WorkspaceSetupInvocation,
    )
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

    command: str = Field(default="", examples=["htop"])
    """The command the session's pane runs. Empty (the default) is a bare
    login shell."""

    resume_command: str = ""
    """The command a resumed session's pane runs. Empty (the default)
    reruns ``command``."""

    required_commands: list[str] = Field(default_factory=list, examples=[["htop"]])
    """Commands that must exist on the session's target before it starts.
    Inheritance combines parent and child entries."""

    enabled_workarounds: Annotated[list[Literal["session-artifact-files"]], MergeStrategy.REPLACE] = Field(
        default_factory=list
    )
    """Opt into session artifact files and AGENTWORKS_ARTIFACTS_DIR. These files
    do not load themselves into a model's context. An authored list replaces
    inherited choices; an empty list disables them."""


class ShellSetupConfig(AgwModel):
    """Activate shell artifact publication at an outer facet."""


class ShellIntegration(HarnessIntegration):
    """Runs an operator command (or a login shell) as the session."""

    contract_version: ClassVar[int] = 7
    name: ClassVar[str] = "shell"
    description: ClassVar[str] = "Run an operator command or a login shell"
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Shell sessions",
        overview="""
        Runs whatever you tell it to. With no `command`, the session is a bare login
        shell. Select `shell` explicitly or inherit the built-in default session template.

        `resume_command` is what ordinary `agw session start` and `restart` run, falling back to
        `command` when it is empty. That pair is enough to drive a harness with no
        dedicated integration of its own: launch it one way, reattach another. A real
        integration is more robust (it knows whether a session exists to resume), but
        the shell escape hatch is always there.

        `required_commands` are checked on the target before the session starts, which
        turns a missing binary into a clear message instead of a pane that dies
        immediately.

        Artifact bundles publish as files, with no model-context claim. User files
        live in `~/.agentworks-artifacts/user/`; workspace files live in
        `<workspace>/.agentworks-artifacts/`. A session with remaining artifacts
        leaves them unhandled by default. Opt into `session-artifact-files` through
        `enabled_workarounds` to publish a private run directory and expose it through
        `AGENTWORKS_ARTIFACTS_DIR`.
        """,
    )

    config_model: ClassVar[type[ShellConfig]] = ShellConfig

    @classmethod
    def config_for(cls, facet: Facet | None = None) -> type[BaseModel] | None:
        return ShellSetupConfig if facet in ("vm", "user", "workspace") else ShellConfig

    def vm_init(self, invocation: VMSetupInvocation) -> ArtifactApplication:
        return (
            ArtifactApplication()
            if self.retiring
            else defer(
                invocation.artifacts, "session", "Shell publishes VM artifacts in the consuming session directory"
            )
        )

    def user_init(self, invocation: UserSetupInvocation) -> ArtifactApplication:
        return (
            ArtifactApplication()
            if self.retiring
            else shell_artifacts(invocation.artifacts, f"{invocation.home}/.agentworks-artifacts/user")
        )

    def workspace_init(self, invocation: WorkspaceSetupInvocation) -> ArtifactApplication:
        return (
            ArtifactApplication()
            if self.retiring
            else shell_artifacts(invocation.artifacts, f"{invocation.root}/.agentworks-artifacts")
        )

    @property
    def config(self) -> ShellConfig:
        """This session's validated shell config."""
        return self._config_as(ShellConfig)

    def start(
        self,
        ctx: RunContext,
        *,
        intent: HarnessLaunchIntent = HarnessLaunchIntent.RESUME_OR_NEW,
    ) -> HarnessStartResult:
        """Select the configured launch command for a supported intent.

        The remaining ``or`` is the cross-field derivation the model's
        own description states, not a fallback to a literal: an empty
        ``resume_command`` means "rerun ``command``", and ``command`` is
        already resolved by the time it is read."""
        if intent is HarnessLaunchIntent.RESUME_ONLY:
            return HarnessStartNotImplemented()
        command = self.config.command if intent.starts_fresh else self.config.resume_command or self.config.command
        context = self._session_binding.artifact_context
        application = ArtifactApplication()
        if context:
            application = (
                shell_artifacts(context.inputs, context.directory, session=True)
                if "session-artifact-files" in self.config.enabled_workarounds
                else defer(
                    context.inputs,
                    "session",
                    "Session file delivery requires enabled_workarounds: [session-artifact-files] "
                    "in the shell session config",
                )
            )
        return HarnessStart(command, artifacts=application)

    def _probe_target(self, transport: Transport) -> None:
        require_commands(
            tuple(self.config.required_commands),
            transport,
            harness_integration_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
