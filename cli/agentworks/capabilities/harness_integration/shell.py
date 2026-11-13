"""Run an operator-authored command or a bare login shell as the session workload."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import Field

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.native.shell import filesystem_artifacts
from agentworks.capabilities.base import ScopeLevel
from agentworks.capabilities.harness_integration.base import (
    HarnessIntegration,
    HarnessLaunchIntent,
    HarnessStart,
    HarnessStartNotImplemented,
    HarnessStartResult,
    require_commands,
)
from agentworks.interpolation import render_template
from agentworks.interpolation_compat import (
    LEGACY_PLACEHOLDERS,
    uses_legacy_placeholders,
    warn_legacy_placeholders,
)
from agentworks.schema import AgwModel
from agentworks.topics import TopicProse
from agentworks.vms.home_layout import AGENTWORKS_ARTIFACTS

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.descriptor import Facet
    from agentworks.capabilities.harness_integration.setup import (
        UserSetupInvocation,
        VMSetupInvocation,
        WorkspaceSetupInvocation,
    )
    from agentworks.execution.access import VMAccess


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
    login shell. Supports ${agw:NAME} context substitution; ordinary shell
    expressions and Go templates retain their own syntax. The legacy
    {{session_name}} and {{workspace_name}} placeholders remain accepted
    with a deprecation warning until removal in 2.0.0."""

    resume_command: str = ""
    """The command a resumed session's pane runs. Empty (the default)
    reruns ``command``. Supports ${agw:NAME} context substitution. Legacy
    {{session_name}} and {{workspace_name}} are deprecated for removal in 2.0.0."""

    required_commands: list[str] = Field(default_factory=list, examples=[["htop"]])
    """Commands that must exist on the session's target before it starts.
    Inheritance combines parent and child entries."""


class ShellSetupConfig(AgwModel):
    """Activate shell artifact publication at an outer facet."""


class ShellIntegration(HarnessIntegration):
    """Runs an operator command (or a login shell) as the session."""

    contract_version: ClassVar[int] = 8
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

        Artifact bundles publish as files, with no model-context claim. VM files
        live in `/opt/agentworks/artifacts/`; user files live in
        `~/.agentworks-artifacts/user/`; workspace files live in
        `<workspace>/.agentworks-artifacts/`. Session artifacts publish in a private
        run directory exposed through `AGENTWORKS_ARTIFACTS_DIR`.
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
            else filesystem_artifacts(invocation.artifacts, "/opt/agentworks/artifacts")
        )

    def user_init(self, invocation: UserSetupInvocation) -> ArtifactApplication:
        return (
            ArtifactApplication()
            if self.retiring
            else filesystem_artifacts(invocation.artifacts, f"{invocation.home}/{AGENTWORKS_ARTIFACTS}/user")
        )

    def workspace_init(self, invocation: WorkspaceSetupInvocation) -> ArtifactApplication:
        return (
            ArtifactApplication()
            if self.retiring
            else filesystem_artifacts(invocation.artifacts, f"{invocation.root}/.agentworks-artifacts")
        )

    @property
    def config(self) -> ShellConfig:
        """This session's validated shell config."""
        return self._config_as(ShellConfig)

    def preflight(self, ctx: RunContext) -> None:
        """Check the selected template even when its target probe defers."""
        super().preflight(ctx)
        scope = ctx.operation_scope
        if scope is not None and scope.level is ScopeLevel.SESSION:
            intent = self._session_binding.launch_intent
            if intent is not HarnessLaunchIntent.RESUME_ONLY:
                self._render_selected_command(intent)

    def _render_selected_command(self, intent: HarnessLaunchIntent) -> str:
        config = self.config
        if not intent.starts_fresh and config.resume_command:
            field, command = "resume_command", config.resume_command
        else:
            field, command = "command", config.command
        rendered = render_template(
            command,
            self._session_binding.context_values,
            field=f"session={self._session_name}.{field}",
            legacy_placeholders=LEGACY_PLACEHOLDERS,
        )
        if uses_legacy_placeholders(command):
            warn_legacy_placeholders()
        return rendered

    def start(
        self,
        ctx: RunContext,
        *,
        intent: HarnessLaunchIntent = HarnessLaunchIntent.RESUME_OR_NEW,
    ) -> HarnessStartResult:
        """Select the configured launch command for a supported intent.

        An empty ``resume_command`` selects ``command`` before rendering,
        so template errors identify the declaration that supplied the text.
        """
        if intent is HarnessLaunchIntent.RESUME_ONLY:
            return HarnessStartNotImplemented()
        command = self._render_selected_command(intent)
        context = self._session_binding.artifact_context
        application = ArtifactApplication()
        if context:
            application = filesystem_artifacts(context.inputs, context.directory, session=True)
        return HarnessStart(command, artifacts=application)

    def _probe_target(self, access: VMAccess) -> None:
        require_commands(
            tuple(self.config.required_commands),
            access,
            harness_integration_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
