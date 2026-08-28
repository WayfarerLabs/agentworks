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
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.harness_integration.base import (
    HarnessIntegration,
    quote_literal_argv,
    require_commands,
)
from agentworks.errors import StateError
from agentworks.schema import AgwModel
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.transports import Transport


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

    name: Literal["grok-build"]
    """The harness integration this config is for."""

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

    extra_args: list[str] = Field(default_factory=list)
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

    contract_version: ClassVar[int] = 1
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
        """,
    )

    _resumed: bool | None = None

    @property
    def config(self) -> GrokBuildConfig:
        """This session's validated Grok Build config."""
        return self._config_as(GrokBuildConfig)

    def start(self, ctx: RunContext) -> str:
        """Start a new Agentworks session, or resume matching Grok state."""
        return self._resume_or_launch(ctx)

    def resume(self, ctx: RunContext) -> str:
        """Restart the workload against the same persisted Grok UUID."""
        return self._resume_or_launch(ctx)

    def launch_note(self) -> str | None:
        if self._resumed is None:
            return None
        return (
            "Existing Grok Build session found. Resuming..."
            if self._resumed
            else "No existing Grok Build session. Starting a new one..."
        )

    def _resume_or_launch(self, ctx: RunContext) -> str:
        sid = self._session_id()
        launch_target = ctx.admin_target() if self._admin else ctx.agent_target()
        resume = launch_target is not None and self._session_exists(launch_target, sid)
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
        if self.config.rules is not None:
            parts += ["--rules", quote_literal_argv(self.config.rules)]
        parts += [shlex.quote(token) for token in self.config.extra_args]
        if not resume and (prompt := self._fresh_prompt()) is not None:
            parts += ["--", quote_literal_argv(prompt)]
        argv = " ".join(parts)
        inner = f"echo {shlex.quote(message)}; exec grok {argv}"
        return f"sh -c {shlex.quote(inner)}"

    def _session_id(self) -> str:
        """Read or mint the UUID persisted in this integration's namespace.

        The state blob crosses executions and is therefore a validation
        boundary. Grok-owned choice sets stay open, but this UUID is minted and
        owned by Agentworks, so malformed persisted strings fail before they
        can influence the filesystem probe or die opaquely in the pane.
        """
        sid = self._state.get("session_id")
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
