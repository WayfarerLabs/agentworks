"""The ``claude-code`` harness integration: run Claude Code as the session workload,
resuming its transcript when one exists and launching fresh otherwise.

Config vocabulary (all optional): ``goal`` and ``initial_prompt`` seed a fresh
conversation; ``agent`` and ``append_system_prompt`` configure every launched
Claude process, including a real resume;
``permission_mode``, ``model``, and
``reasoning_effort`` map to the ``--permission-mode`` / ``--model`` /
``--effort`` flags verbatim; ``remote_control`` enables Claude Code Remote
Control; ``vim_mode`` and ``terminal_bell`` become session-local settings; and
``extra_args`` is a list of raw argv tokens emitted after generated options
(the operator escape hatch for any flag the harness integration does not model). See
``claude-code-lld.md``.

Addressing uses a stored per-session Claude session id (a v4 uuid) kept in
the harness integration's state namespace under ``session_id``: minted once on the first
``start`` and read back on every ``restart``, because the session manager
persists the blob to the session row after each op. Resume-vs-launch is an
op-time existence probe for that id's transcript on disk (slug-independent,
so it does not reconstruct Claude's brittle cwd-slug directory name): a
transcript present means the session is resumable, so ``--resume``; absent
means launch fresh with ``--session-id``. That file-presence boundary was
empirically confirmed to equal Claude's own resume boundary, so neither a
blind resume of a nonexistent session nor a resume of an unresumable stub
is possible.
"""

from __future__ import annotations

import json
import shlex
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import (
    NativeSessionArtifacts,
    defer_setup_conflicts,
    native_home,
    prepare_session_delivery,
    render_setup_artifacts,
    validate_discovery_paths,
    validate_native_command,
    validate_user_placement,
)
from agentworks.artifacts.native.probe import probe_native
from agentworks.capabilities.harness_integration.base import (
    HarnessIntegration,
    HarnessLaunchIntent,
    HarnessStart,
    require_commands,
)
from agentworks.errors import StateError
from agentworks.plugins._harness_native.native import setup_user, setup_workspace
from agentworks.plugins._harness_native.native_config import NativeUserConfig, NativeWorkspaceConfig
from agentworks.plugins.claude.artifacts import outer_artifacts, session_artifacts
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


class ClaudeCodeUserConfig(NativeUserConfig):
    """Native ClaudeCode setup for one actual user."""


class ClaudeCodeWorkspaceConfig(NativeWorkspaceConfig):
    """Native ClaudeCode project settings."""


class ClaudeCodeSetupConfig(AgwModel):
    """Activate an artifact-only native facet."""


class ClaudeCodeConfig(AgwModel):
    """What a session template tells the ``claude-code`` integration.

    The ``--permission-mode``, ``--model``, and ``--effort`` choice sets are
    Claude's own and drift between releases, so their values are forwarded
    unvalidated rather than mirrored here. The installed Claude version owns
    whether a value fails, falls back, or is interpreted for a specific model.
    The native workload flags, positional parser behavior, and ``/goal``
    composition were rechecked with Claude Code 2.1.231.
    """

    permission_mode: str | None = None
    """Forwarded as ``--permission-mode``."""

    model: str | None = None
    """Forwarded as ``--model``."""

    reasoning_effort: str | None = None
    """Forwarded as ``--effort`` without validating Claude's evolving choice
    set. A child template's declared value replaces its parent's."""

    goal: str | None = None
    """A Claude Code ``/goal`` condition submitted when this integration
    starts a fresh conversation. It is not replayed on resume."""

    initial_prompt: str | None = None
    """The first prompt for a fresh conversation. When ``goal`` is also set,
    it becomes initial guidance inside the native goal directive. It is not
    replayed on resume."""

    agent: str | None = None
    """Forwarded as ``--agent`` on every process launch, including resume.
    Claude Code owns agent discovery and validation."""

    append_system_prompt: str | None = None
    """Forwarded as ``--append-system-prompt`` on every process launch,
    including resume."""

    remote_control: bool = False
    """When true, enable Claude Code Remote Control and use the Agentworks
    session name as its title. Remote Control requires a Claude subscription
    login, does not support API-key authentication, and may require
    organization-level enablement. False (the default) adds no override. A
    child template's value replaces its parent's."""

    vim_mode: bool = False
    """When true, enable Vim-style prompt editing through a session-local
    Claude setting. A raw ``--settings`` in ``extra_args`` comes later and
    replaces this generated setting. False (the default) adds no override. A
    child template's value replaces its parent's."""

    terminal_bell: bool = False
    """When true, ask Claude Code to ring the terminal bell when a task
    finishes or needs permission. A raw ``--settings`` in ``extra_args`` comes
    later and replaces this generated setting. False (the default) adds no
    override. A child template's value replaces its parent's."""

    enabled_workarounds: Annotated[
        list[Literal["session-skill-plugin", "session-agent-definitions"]], MergeStrategy.REPLACE
    ] = Field(default_factory=list)
    """Explicitly enabled session artifact carriers. An authored list replaces inherited opt-ins."""

    extra_args: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)
    """Appended after every generated CLI option, so it can carry
    any flag this integration does not model. Claude uses the last
    ``--settings`` occurrence, so a raw one here replaces all generated
    session settings. On a fresh launch with an initial input, only the
    ``--`` prompt separator and prompt value follow these option tokens. A
    child template's declared list replaces its parent's. Context references such as
    ${agw:workspace_dir} render before each argument is shell-quoted; replacement
    contents are literal data."""


# The transcript's config root. ``CLAUDE_CONFIG_DIR`` is the CLI's own
# override env var (confirmed present in the v2.1.205 binary); the default
# is ``$HOME/.claude``. Expanded by the target-side shell inside the
# find probe, never here.
_PROJECTS_DIR = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"


class ClaudeCodeIntegration(HarnessIntegration):
    """Runs Claude Code, resuming or launching fresh per on-disk state."""

    contract_version: ClassVar[int] = 7
    name: ClassVar[str] = "claude-code"
    description: ClassVar[str] = "Run Claude Code, resuming its session when one exists"
    config_model: ClassVar[type[ClaudeCodeConfig]] = ClaudeCodeConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Claude Code",
        overview="""
        Runs Claude Code as the session's workload. Starting a session that already has
        on-disk state resumes that conversation; starting a fresh one launches fresh, so
        ordinary `agw session start` or `agw session restart` and a reattach after a
        reboot behave the way an operator expects without either being configured.

        Ships as the opt-in `claude` system plugin, and needs the `claude` CLI on the
        session's target.

        Artifact bundles activated at a user or workspace facet publish native rules,
        skills and agent personas. Session artifacts are optional and unhandled by
        default. `session-skill-plugin` delivers rules, hints and skills through a
        private native plugin; `session-agent-definitions` adds personas to that plugin.
        Generated guidance skills request reading before work; publication does not
        guarantee model cooperation or replace old conversation content. Raw arguments
        that disable or replace active artifact carriers are rejected.
        """,
    )

    # Set by _resume_or_launch on each start/restart; drives the ordinary
    # HarnessStart note. None until the op runs (nothing decided yet).
    _resumed: bool | None = None
    _artifact_plan: NativeSessionArtifacts = NativeSessionArtifacts()

    @classmethod
    def config_for(cls, facet: Facet | None = None) -> type[BaseModel] | None:
        """Select independent config for user, workspace, or session setup."""
        if facet == "vm":
            return ClaudeCodeSetupConfig
        if facet == "user":
            return ClaudeCodeUserConfig
        if facet == "workspace":
            return ClaudeCodeWorkspaceConfig
        return super().config_for(facet)

    def vm_init(self, invocation: VMSetupInvocation) -> ArtifactApplication:
        plan = (
            ArtifactApplication()
            if self.retiring
            else render_setup_artifacts(
                invocation,
                lambda inputs: outer_artifacts(inputs, "/etc/claude-code/.claude", guidance_as_skills=True),
            )
        )
        if plan.files:
            plan = defer_setup_conflicts(
                plan,
                invocation,
                lambda files: probe_native(
                    invocation.runner,
                    tool="claude",
                    environment=invocation.environment,
                    redactions=invocation.secrets.values(),
                    files=files,
                    vm_only=True,
                ),
            )
        return plan

    def user_init(self, invocation: UserSetupInvocation) -> ArtifactApplication:
        """Apply native setup and return the user's artifact publication plan."""
        root = (
            probe_native(
                invocation.runner,
                tool="claude",
                environment=invocation.environment,
                redactions=invocation.secrets.values(),
                home=invocation.home,
                check_policy=False,
            )
            if invocation.artifacts and not self.retiring
            else ""
        )
        config = None if self.retiring else self._config_as(ClaudeCodeUserConfig)
        plan = (
            ArtifactApplication()
            if self.retiring
            else render_setup_artifacts(
                invocation,
                lambda inputs: outer_artifacts(
                    inputs,
                    root or native_home(invocation.home, invocation.environment, "CLAUDE_CONFIG_DIR", ".claude"),
                ),
            )
        )
        validate_user_placement(plan, invocation.home)
        setup_invocation = (
            replace(invocation, environment={**invocation.environment, "CLAUDE_CONFIG_DIR": root})
            if root
            else invocation
        )
        setup_user("claude", config, setup_invocation)
        if plan.files:
            plan = defer_setup_conflicts(
                plan,
                invocation,
                lambda files: probe_native(
                    invocation.runner,
                    tool="claude",
                    environment=invocation.environment,
                    redactions=invocation.secrets.values(),
                    home=invocation.home,
                    files=files,
                ),
            )
        return plan

    def workspace_init(self, invocation: WorkspaceSetupInvocation) -> ArtifactApplication:
        """Apply project settings and return workspace-owned artifact files."""
        config = None if self.retiring else self._config_as(ClaudeCodeWorkspaceConfig)
        plan = (
            ArtifactApplication()
            if self.retiring
            else render_setup_artifacts(
                invocation, lambda inputs: outer_artifacts(inputs, f"{invocation.root}/.claude")
            )
        )
        setup_workspace("claude", config, invocation)
        if plan.files:
            plan = defer_setup_conflicts(
                plan,
                invocation,
                lambda files: probe_native(
                    invocation.runner,
                    tool="claude",
                    environment=invocation.environment,
                    redactions=invocation.secrets.values(),
                    workspace=invocation.root,
                    files=files,
                    workspace_only=True,
                ),
            )
        return plan

    @property
    def config(self) -> ClaudeCodeConfig:
        """This session's validated claude-code config."""
        return self._config_as(ClaudeCodeConfig)

    @classmethod
    def hoist_legacy_state(cls, blob: dict[str, object]) -> None:
        """Compatibility (pre-namespacing harness_state): DELETE on the
        next major release, together with the base hook and its seam call.

        Pre-namespacing rows stored this harness integration's ``session_id`` at the
        blob's top level; adopt it into the ``claude-code`` namespace so
        the session resumes with the same id after the seam split. One
        coherent rule: a NON-EMPTY string is the only value this harness integration
        ever wrote, so only that shape is adopted, and it is adopted
        whenever the namespaced ``session_id`` is not itself a non-empty
        string (a valid namespaced id wins; hand-edited garbage there
        does not get to discard a real legacy id). The flat key is
        removed for ANY string (empty-string garbage is swept too),
        which also makes the hoist idempotent; a non-string flat value
        is not this harness integration's legacy shape and is left alone.

        "Namespaced wins" assumes forward-only history, consistent with
        the repo's forward-only migration doctrine: after the namespacing
        release only the namespace is ever written, so the namespaced
        value is the recent one. A downgrade-then-upgrade interleaving
        (where the flat id would be the recent one) is out of scope.
        """
        legacy = blob.get("session_id")
        if not isinstance(legacy, str):
            return
        del blob["session_id"]
        if not legacy:
            return
        namespace = blob.get(cls.name)
        if not isinstance(namespace, dict):
            namespace = {}
            blob[cls.name] = namespace
        namespaced = namespace.get("session_id")
        if not (isinstance(namespaced, str) and namespaced):
            namespace["session_id"] = legacy

    def start(
        self,
        ctx: RunContext,
        *,
        intent: HarnessLaunchIntent = HarnessLaunchIntent.RESUME_OR_NEW,
    ) -> HarnessStart:
        """Choose the requested fresh, strict-resume, or fallback policy."""
        artifact_context = self._session_binding.artifact_context
        runner = ctx.admin_target() if self._admin else ctx.agent_target()
        self._artifact_plan, native_root = prepare_session_delivery(
            runner,
            tool="claude",
            integration=self.name,
            context=artifact_context,
            render=lambda prepared: session_artifacts(
                prepared,
                extra_args=self.config.extra_args,
                enabled_workarounds=self.config.enabled_workarounds,
            ),
            workspace=self._workspace_path,
        )
        if artifact_context is not None and native_root is not None:
            validate_discovery_paths(
                artifact_context,
                (native_root, f"{self._workspace_path}/.claude", "/etc/claude-code/.claude"),
                files=("/etc/claude-code/CLAUDE.md",),
            )
        command = self._resume_or_launch(ctx, intent=intent)
        if intent is HarnessLaunchIntent.FORCE_NEW:
            note = "Fresh Claude Code session requested. Starting a new one without resuming prior state..."
        elif intent is HarnessLaunchIntent.CREATE:
            note = "Starting a new Claude Code session..."
        elif self._resumed:
            note = "Existing Claude Code session found. Resuming..."
        else:
            note = "No existing Claude Code session. Starting a new one..."
        if self._artifact_plan.argv:
            validate_native_command(command)
        return HarnessStart(command, note, self._artifact_plan.application)

    def _resume_or_launch(self, ctx: RunContext, *, intent: HarnessLaunchIntent) -> str:
        """Read (or mint) the stored session id, probe the launch target
        for its transcript, and return the single ``sh -c`` pane command
        that echoes the visible decision and ``exec``s ``claude``."""
        fresh = intent.starts_fresh
        resume_only = intent is HarnessLaunchIntent.RESUME_ONLY
        stored_sid = self._state.get("session_id")
        if resume_only and not (isinstance(stored_sid, str) and stored_sid):
            raise self._resume_only_error()
        sid = self._session_id(fresh=fresh)
        launch_target = ctx.admin_target() if self._admin else ctx.agent_target()
        resume = not fresh and launch_target is not None and self._transcript_exists(launch_target, sid)
        if resume_only and not resume:
            raise self._resume_only_error()
        self._resumed = resume

        if resume:
            identity = ["--resume", sid]
            msg = f"agentworks harness integration (claude-code): resuming session {self._session_name}"
        else:
            identity = ["--session-id", sid]
            msg = f"agentworks harness integration (claude-code): starting new session {self._session_name}"
        parts = [shlex.quote(token) for token in (*identity, "--name", self._session_name, *self._managed_flags())]
        if self.config.agent is not None:
            selected_agent = self.config.agent
            context = self._session_binding.artifact_context
            selected_origins = (
                {
                    item.origin_identity
                    for item in context.inputs.items()
                    if item.content.type is ArtifactType.AGENT and item.content.name == selected_agent
                }
                if context is not None
                else set()
            )
            delivered = {
                file.native_identity.removeprefix("agent:")
                for file in self._artifact_plan.application.files
                if file.native_identity is not None
                and file.native_identity.startswith("agent:")
                and selected_origins.intersection(file.origins)
            }
            if len(delivered) > 1:
                raise StateError(
                    f"Claude persona selection '{selected_agent}' has multiple delivered native identities"
                )
            selected_agent = next(iter(delivered), selected_agent)
            parts += ["--agent", shlex.quote(selected_agent)]
        if self.config.append_system_prompt is not None:
            parts += ["--append-system-prompt", shlex.quote(self.config.append_system_prompt)]
        parts += [shlex.quote(token) for token in self._artifact_plan.argv]
        parts += [self.quote_context_argv(token) for token in self.config.extra_args]
        if not resume and (prompt := self._fresh_prompt()) is not None:
            parts += ["--", shlex.quote(prompt)]
        argv = " ".join(parts)
        # A single ``sh -c`` so the whole thing survives the ``exec``
        # wrapping the tmux pane applies (``exec`` takes one simple
        # command): the login shell execs this sh, which echoes then
        # execs claude, so the pane becomes Claude. The manager consumes
        # this completed command without further interpolation.
        inner = f"echo {shlex.quote(msg)}; exec claude {argv}"
        return f"sh -c {shlex.quote(inner)}"

    def _resume_only_error(self) -> StateError:
        return StateError(
            f"session '{self._session_name}': Claude Code has no resumable conversation on its launch target",
            entity_kind="session",
            entity_name=self._session_name,
            hint="Retry without --resume-only to allow a new Claude Code conversation.",
        )

    def _session_id(self, *, fresh: bool = False) -> str:
        """The stored Claude session id, minted (and recorded in the state
        blob) on first use. A v4 uuid: Claude accepts any valid uuid at
        ``--session-id``, and global uniqueness keeps the transcript probe
        slug-independent. ``self._state`` rides inside the session node's
        full namespaced blob, which the manager persists after the op, so
        a minted id survives to the next restart."""
        sid = None if fresh else self._state.get("session_id")
        if not isinstance(sid, str):
            # If the op raises after this mint but before the manager
            # persists the blob, the id is lost. That window is benign: it
            # can only happen on a pre-migration session's FIRST restart
            # (a create always persists its minted id with the new row);
            # there, neither the old nor a re-minted id has a transcript,
            # so the retry launches fresh either way, no history is lost.
            sid = str(uuid.uuid4())
            self._state["session_id"] = sid
        return sid

    def _managed_flags(self) -> list[str]:
        """Managed argv tokens for this launch.

        Process preferences apply whenever the process is launched. The caller
        adds literal workload controls, then ``extra_args`` and any separated
        fresh prompt.
        """
        tokens: list[str] = []
        if self.config.permission_mode is not None:
            tokens += ["--permission-mode", self.config.permission_mode]
        if self.config.model is not None:
            tokens += ["--model", self.config.model]
        if self.config.reasoning_effort is not None:
            tokens += ["--effort", self.config.reasoning_effort]
        if self.config.remote_control:
            # The flag's value is optional. Supplying the display name keeps a
            # later positional in extra_args from becoming the Remote Control
            # title accidentally.
            tokens += ["--remote-control", self._session_name]
        settings: dict[str, str] = {}
        if self.config.vim_mode:
            settings["editorMode"] = "vim"
        if self.config.terminal_bell:
            settings["preferredNotifChannel"] = "terminal_bell"
        if settings:
            tokens += ["--settings", json.dumps(settings, separators=(",", ":"))]
        return tokens

    def _fresh_prompt(self) -> str | None:
        """The single initial input Claude accepts for a fresh TUI session.

        Claude's native ``/goal`` command starts the first turn itself. When
        both fields are set, the initial prompt is therefore carried as
        guidance in that goal directive rather than submitted as a second
        turn that the CLI has no startup channel for. Without a goal, one
        leading newline is a lexical transport guard: Claude dispatches an
        exact subcommand name even after ``--``, while the prefixed value
        remains prompt data without mirroring its evolving command set.
        """
        if self.config.goal is None:
            return None if self.config.initial_prompt is None else f"\n{self.config.initial_prompt}"
        prompt = f"/goal {self.config.goal}"
        if self.config.initial_prompt is not None:
            prompt += f"\n\nInitial guidance for this goal:\n{self.config.initial_prompt}"
        return prompt

    def _transcript_exists(self, transport: Transport, sid: str) -> bool:
        """True iff the stored session's transcript (``<sid>.jsonl``) exists
        under the projects dir on the launch target. Slug-independent
        (``find`` matches under ANY project directory); shell-neutral (no
        glob reaches the shell). Runs through ``$SHELL -lic`` like the
        readiness probe.

        On restart this probe runs before the orchestrator tears down the old
        runtime, which may still be live. No flush wait is needed: Claude
        writes transcript turns to the ``.jsonl`` incrementally as work
        happens rather than flushing them only on exit.

        The exit code is read, not just ``.ok``, to keep a probe that could
        not EXECUTE from masquerading as "no transcript". The inner command
        keeps find's own failure distinguishable from a clean no-match: a
        missing projects dir (Claude never ran here) exits 1 up front; a
        printed match exits 0 (a found transcript is definitive even if
        find also stumbled elsewhere); a find that FAILED without printing
        one (an unreadable subdir, not a mere no-match) exits 6 rather
        than folding into "no transcript". Anything but {0, 1} (the 6, an
        SSH failure's 255, a shell that could not start) raises: guessing
        "fresh" would launch ``--session-id <reserved-uuid>``, which
        Claude rejects as already-in-use on a real session's restart and
        the pane fails to start."""
        needle = shlex.quote(f"{sid}.jsonl")
        inner = (
            f'[ -d "{_PROJECTS_DIR}" ] || exit 1; '
            f'out=$(find "{_PROJECTS_DIR}" -name {needle} -print -quit 2>/dev/null); rc=$?; '
            f'[ -n "$out" ] && exit 0; [ "$rc" -eq 0 ] || exit 6; exit 1'
        )
        result = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if result.returncode == 0:
            return True  # transcript on disk: resume
        if result.returncode == 1:
            return False  # the probe ran, no match: launch fresh
        raise StateError(
            f"session '{self._session_name}': could not probe for the Claude "
            f"transcript on {self._target_label} (exit {result.returncode}); "
            f"refusing to guess resume-vs-launch.",
            entity_kind="session",
            entity_name=self._session_name,
            hint="Retry once the launch target is reachable.",
        )

    def _probe_target(self, transport: Transport) -> None:
        """Readiness proves only that ``claude`` is installed; it never
        inspects session state (detection is an op-time concern)."""
        require_commands(
            ("claude",),
            transport,
            harness_integration_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
