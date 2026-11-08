"""The ``grok-build`` harness integration.

Agentworks mints one UUID for each session and stores it in the integration's
state namespace. A fresh launch passes that UUID through Grok Build's
``--session-id`` flag. On restart, a persisted Grok ``summary.json`` for the
UUID selects ``--resume``; when no persisted session exists, the same UUID
starts a new conversation. The probe scans every encoded workspace directory
under Grok's session root, so it does not duplicate Grok's cwd encoding.

Config vocabulary is deliberately small and open. ``goal`` and
``initial_prompt`` seed a fresh conversation; ``agent`` and ``rules`` configure
every Grok process launch, including a real resume. ``permission_mode``,
``model``, ``reasoning_effort``, and ``sandbox`` forward to Grok-owned CLI
choice sets without mirroring their values. ``extra_args`` appends raw argv
tokens after generated options and before any fresh positional prompt, so new
upstream flags do not require an Agentworks release.
"""

from __future__ import annotations

import shlex
import uuid
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.native.common import (
    NativeSessionArtifacts,
    defer,
    delivery_files,
    native_home,
    validate_discovery_paths,
    validate_native_command,
    validate_user_placement,
)
from agentworks.artifacts.native.probe import probe_native
from agentworks.capabilities.harness_integration.base import (
    HarnessIntegration,
    HarnessLaunchIntent,
    HarnessStart,
    quote_literal_argv,
    require_commands,
)
from agentworks.errors import StateError
from agentworks.plugins.grok.artifacts import outer_artifacts, session_artifacts
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


class GrokBuildSetupConfig(AgwModel):
    """Activate an artifact-only native facet."""


class GrokBuildConfig(AgwModel):
    """What a session template tells the ``grok-build`` integration.

    Every string choice belongs to Grok Build and forwards unvalidated. The
    installed CLI owns accepted values, warnings, errors, and model-specific
    interpretation. The top-level ``--agent`` and ``--rules`` flags,
    positional startup prompt, and ``/goal`` claims here were checked against
    official Grok Build 1.0.10 source at commit ``77cd7eb`` and its
    documentation. No 1.0.10 binary was installed for this recheck, so runtime
    observations below remain explicitly pinned to 1.0.4.
    """

    permission_mode: str | None = None
    """Forwarded as ``--permission-mode``. A child template's declared
    value replaces its parent's."""

    model: str | None = None
    """Forwarded as ``--model``. A child template's declared value replaces
    its parent's."""

    reasoning_effort: str | None = None
    """Forwarded as ``--reasoning-effort``. A child template's declared
    value replaces its parent's."""

    sandbox: str | None = None
    """Forwarded as ``--sandbox``. Grok Build 1.0.4 fails startup for an
    unknown profile rather than falling back. A child template's declared
    value replaces its parent's."""

    goal: str | None = None
    """A Grok Build ``/goal`` objective submitted when this integration
    starts a fresh conversation. It is not replayed on resume."""

    initial_prompt: str | None = None
    """The first prompt for a fresh conversation. When ``goal`` is also set,
    it becomes initial guidance inside the native goal directive. It is not
    replayed on resume."""

    agent: str | None = None
    """Forwarded as top-level ``--agent`` on every process launch, including
    resume. Grok Build owns identity lookup and validation."""

    rules: str | None = None
    """Forwarded as ``--rules`` on every process launch, including resume."""

    enabled_workarounds: Annotated[
        list[Literal["session-rules", "session-agent-definitions"]], MergeStrategy.REPLACE
    ] = Field(default_factory=list)
    """Explicitly enabled session artifact carriers. An authored list replaces inherited opt-ins."""

    extra_args: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)
    """Raw argv tokens appended verbatim after every managed flag and before
    any fresh positional prompt. Grok Build 1.0.4 rejects repeated managed
    flags, so use this for unmodeled flags rather than overriding a modeled
    field. A child template's declared list replaces its parent's instead of
    accumulating."""


# Grok Build 1.0.4 resolves its user-state root through the official
# ``xai-grok-home`` crate: a non-empty ``GROK_HOME`` verbatim, otherwise
# ``$HOME/.grok``. Sessions are stored below that same root.
_SESSIONS_DIR = "${GROK_HOME:-$HOME/.grok}/sessions"


class GrokBuildIntegration(HarnessIntegration):
    """Run Grok Build, resuming its persisted session when one exists."""

    contract_version: ClassVar[int] = 7
    name: ClassVar[str] = "grok-build"
    description: ClassVar[str] = "Run Grok Build, resuming its session when one exists"
    config_model: ClassVar[type[GrokBuildConfig]] = GrokBuildConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Grok Build",
        overview="""
        Runs Grok Build as the session workload. Agentworks assigns the
        conversation a stable UUID, resumes it while Grok's local session state
        exists, and starts fresh when that state is absent.

        Ships as the opt-in `grok` system plugin and requires the `grok` CLI on
        the session's launch target.

        User and workspace facets publish native rules, skills and agent personas.
        Session artifacts are optional and unhandled by default. `enabled_workarounds`
        can opt into `session-rules` for rules/hints through `--rules`, and
        `session-agent-definitions` for personas through `--agents`. Private session
        skills have no supported workaround; activate the user or workspace facet
        to publish those skills at their native scope.
        """,
    )

    _resumed: bool | None = None
    _artifact_plan: NativeSessionArtifacts = NativeSessionArtifacts()

    @classmethod
    def config_for(cls, facet: Facet | None = None) -> type[BaseModel] | None:
        return GrokBuildSetupConfig if facet in ("vm", "user", "workspace") else GrokBuildConfig

    def vm_init(self, invocation: VMSetupInvocation) -> ArtifactApplication:
        return (
            ArtifactApplication()
            if self.retiring
            else defer(invocation.artifacts, "user", "Grok Build has no supported machine-wide artifact file location")
        )

    def user_init(self, invocation: UserSetupInvocation) -> ArtifactApplication:
        root = (
            probe_native(
                invocation.runner,
                tool="grok",
                environment=invocation.environment,
                home=invocation.home,
                check_policy=False,
            )
            if invocation.artifacts and not self.retiring
            else ""
        )
        plan = (
            ArtifactApplication()
            if self.retiring
            else outer_artifacts(
                invocation.artifacts, root or native_home(invocation.home, invocation.environment, "GROK_HOME", ".grok")
            )
        )
        validate_user_placement(plan, invocation.home)

        if plan.files:
            probe_native(
                invocation.runner,
                tool="grok",
                environment=invocation.environment,
                home=invocation.home,
                files=plan.files,
            )
        return plan

    def workspace_init(self, invocation: WorkspaceSetupInvocation) -> ArtifactApplication:
        plan = (
            ArtifactApplication()
            if self.retiring
            else outer_artifacts(invocation.artifacts, f"{invocation.root}/.grok")
        )
        if plan.files:
            probe_native(
                invocation.runner,
                tool="grok",
                environment=invocation.environment,
                workspace=invocation.root,
                files=plan.files,
                workspace_only=True,
            )
        return plan

    @property
    def config(self) -> GrokBuildConfig:
        """This session's validated Grok Build config."""
        return self._config_as(GrokBuildConfig)

    def start(
        self,
        ctx: RunContext,
        *,
        intent: HarnessLaunchIntent = HarnessLaunchIntent.RESUME_OR_NEW,
    ) -> HarnessStart:
        """Choose the requested fresh, strict-resume, or fallback policy."""
        self._artifact_plan = session_artifacts(
            self._session_binding.artifact_context,
            configured=self.config.rules,
            extra_args=self.config.extra_args,
            enabled_workarounds=self.config.enabled_workarounds,
        )
        artifact_context = self._session_binding.artifact_context
        if artifact_context is not None and (
            artifact_context.ancestor_files or self._artifact_plan.application.files or self._artifact_plan.argv
        ):
            runner = ctx.admin_target() if self._admin else ctx.agent_target()
            if runner is None:
                raise StateError("artifact delivery requires the actual native launch target")
            files = delivery_files(artifact_context, self._artifact_plan.application)
            native_root = probe_native(
                runner,
                tool="grok",
                environment=artifact_context.environment,
                home=artifact_context.home,
                workspace=self._workspace_path,
                files=files,
                session_plugin="--plugin-dir" in self._artifact_plan.argv,
            )
            validate_discovery_paths(artifact_context, (native_root, f"{self._workspace_path}/.grok"))

        command = self._resume_or_launch(ctx, intent=intent)
        if intent is HarnessLaunchIntent.FORCE_NEW:
            note = "Fresh Grok Build session requested. Starting a new one without resuming prior state..."
        elif intent is HarnessLaunchIntent.CREATE:
            note = "Starting a new Grok Build session..."
        elif self._resumed:
            note = "Existing Grok Build session found. Resuming..."
        else:
            note = "No existing Grok Build session. Starting a new one..."
        if self._artifact_plan.argv:
            validate_native_command(command)
        return HarnessStart(command, note, self._artifact_plan.application)

    def _resume_or_launch(self, ctx: RunContext, *, intent: HarnessLaunchIntent) -> str:
        fresh = intent.starts_fresh
        resume_only = intent is HarnessLaunchIntent.RESUME_ONLY
        stored_sid = self._state.get("session_id")
        if resume_only and not (isinstance(stored_sid, str) and stored_sid):
            raise self._resume_only_error()
        sid = self._session_id(fresh=fresh)
        launch_target = ctx.admin_target() if self._admin else ctx.agent_target()
        resume = not fresh and launch_target is not None and self._session_exists(launch_target, sid)
        if resume_only and not resume:
            raise self._resume_only_error()
        self._resumed = resume

        if resume:
            identity = ["--resume", sid]
            message = f"agentworks harness integration (grok-build): resuming session {self._session_name}"
        else:
            identity = ["--session-id", sid]
            message = f"agentworks harness integration (grok-build): starting new session {self._session_name}"

        parts = [shlex.quote(token) for token in (*identity, *self._managed_flags())]
        if self.config.agent is not None:
            parts += ["--agent", quote_literal_argv(self.config.agent)]
        if self.config.rules is not None and "--rules" not in self._artifact_plan.argv:
            parts += ["--rules", quote_literal_argv(self.config.rules)]
        parts += [quote_literal_argv(token) for token in self._artifact_plan.argv]
        parts += [shlex.quote(token) for token in self.config.extra_args]
        if not resume and (prompt := self._fresh_prompt()) is not None:
            parts += ["--", quote_literal_argv(prompt)]
        argv = " ".join(parts)
        inner = f"echo {shlex.quote(message)}; exec grok {argv}"
        return f"sh -c {shlex.quote(inner)}"

    def _resume_only_error(self) -> StateError:
        return StateError(
            f"session '{self._session_name}': Grok Build has no resumable conversation on its launch target",
            entity_kind="session",
            entity_name=self._session_name,
            hint="Retry without --resume-only to allow a new Grok Build conversation.",
        )

    def _session_id(self, *, fresh: bool = False) -> str:
        """Read or mint the UUID persisted in this integration's namespace.

        The state blob crosses executions and is therefore a validation
        boundary. Grok-owned choice sets stay open, but this UUID is minted and
        owned by Agentworks, so malformed persisted strings fail before they
        can influence the filesystem probe or die opaquely in the pane.
        """
        sid = None if fresh else self._state.get("session_id")
        if not isinstance(sid, str):
            sid = str(uuid.uuid4())
            self._state["session_id"] = sid
            return sid
        try:
            canonical_sid = str(uuid.UUID(sid))
        except ValueError:
            canonical_sid = None
        if canonical_sid != sid:
            raise StateError(
                f"session '{self._session_name}': stored Grok Build session id is not a canonical UUID.",
                entity_kind="session",
                entity_name=self._session_name,
                hint="Repair or recreate the session before starting Grok Build again.",
            ) from None
        return sid

    def _managed_flags(self) -> list[str]:
        tokens: list[str] = []
        if self.config.permission_mode is not None:
            tokens += ["--permission-mode", self.config.permission_mode]
        if self.config.model is not None:
            tokens += ["--model", self.config.model]
        if self.config.reasoning_effort is not None:
            tokens += ["--reasoning-effort", self.config.reasoning_effort]
        if self.config.sandbox is not None:
            tokens += ["--sandbox", self.config.sandbox]
        return tokens

    def _fresh_prompt(self) -> str | None:
        """The single initial input Grok accepts for a fresh TUI session.

        Grok's native ``/goal`` command starts the first turn itself. When
        both fields are set, the initial prompt is therefore carried as
        guidance in that goal directive rather than submitted as a second
        turn that the CLI has no startup channel for.
        """
        if self.config.goal is None:
            return self.config.initial_prompt
        prompt = f"/goal {self.config.goal}"
        if self.config.initial_prompt is not None:
            prompt += f"\n\nInitial guidance for this goal:\n{self.config.initial_prompt}"
        return prompt

    def _session_exists(self, transport: Transport, sid: str) -> bool:
        """Check Grok's own persisted-session boundary for ``sid``.

        Grok treats a UUID directory as resumable only when it contains a
        regular ``summary.json``. Its storage layout is
        ``sessions/<encoded-cwd>/<uuid>/summary.json``. The scan stays
        cwd-independent and uses that same durable boundary without attempting
        to reproduce Grok's cwd encoder. Verified against Grok Build 1.0.4;
        re-verify the boundary after a major upstream update.

        Exit 1 is a clean absence. Exit 6 keeps a failed ``find`` distinct, so
        an unreadable session root or transport failure never becomes an
        accidental fresh launch over a persisted UUID.
        """
        summary_path = shlex.quote(f"*/{sid}/summary.json")
        inner = (
            f'[ -d "{_SESSIONS_DIR}" ] || exit 1; '
            f'out=$(find "{_SESSIONS_DIR}" -mindepth 3 -maxdepth 3 -type f '
            f"-path {summary_path} -print -quit 2>/dev/null); rc=$?; "
            f'[ -n "$out" ] && exit 0; [ "$rc" -eq 0 ] || exit 6; exit 1'
        )
        result = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise StateError(
            f"session '{self._session_name}': could not probe for the Grok Build "
            f"session on {self._target_label} (exit {result.returncode}); "
            "refusing to guess resume-vs-launch.",
            entity_kind="session",
            entity_name=self._session_name,
            hint="Retry once the launch target and its Grok session directory are readable.",
        )

    def _probe_target(self, transport: Transport) -> None:
        """Readiness proves only that the Grok Build CLI is installed."""
        require_commands(
            ("grok",),
            transport,
            harness_integration_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
