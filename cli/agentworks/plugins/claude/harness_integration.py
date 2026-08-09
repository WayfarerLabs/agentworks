"""The ``claude-code`` harness integration: run Claude Code as the session workload,
resuming its transcript when one exists and launching fresh otherwise.

Config vocabulary (all optional): ``permission_mode`` and ``model`` map to
the ``--permission-mode`` / ``--model`` flags verbatim, and ``extra_args``
is a list of raw argv tokens appended last (the operator escape hatch for
any flag the harness integration does not model). See ``claude-code-lld.md``.

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

import shlex
import uuid
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.harness_integration.base import HarnessIntegration, require_commands
from agentworks.errors import StateError
from agentworks.schema import AgwModel
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.transports import Transport


class ClaudeCodeConfig(AgwModel):
    """What a session template tells the ``claude-code`` integration.

    The ``--permission-mode`` and ``--model`` CHOICE sets are Claude's own
    and drift between releases, so the values are forwarded unvalidated:
    an invalid one surfaces as Claude's own startup error in the pane,
    which is a better answer than a list of ours going stale.
    """

    name: Literal["claude-code"]
    """The harness integration this config is for."""

    permission_mode: str | None = None
    """Forwarded as ``--permission-mode``."""

    model: str | None = None
    """Forwarded as ``--model``."""

    extra_args: list[str] = Field(default_factory=list)
    """Appended to the command verbatim, last, so it can carry any flag
    this integration does not model."""


# The transcript's config root. ``CLAUDE_CONFIG_DIR`` is the CLI's own
# override env var (confirmed present in the v2.1.205 binary); the default
# is ``$HOME/.claude``. Expanded by the target-side shell inside the
# find probe, never here.
_PROJECTS_DIR = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"


class ClaudeCodeIntegration(HarnessIntegration):
    """Runs Claude Code, resuming or launching fresh per on-disk state."""

    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "claude-code"
    description: ClassVar[str] = "Run Claude Code, resuming its session when one exists"
    config_model: ClassVar[type[ClaudeCodeConfig]] = ClaudeCodeConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Claude Code",
        overview="""
        Runs Claude Code as the session's workload. Starting a session that already has
        on-disk state resumes that conversation; starting a fresh one launches fresh, so
        `agw session resume` and a reattach after a reboot behave the way an operator
        expects without either being configured.

        Ships as the opt-in `claude` system plugin, and needs the `claude` CLI on the
        session's target.
        """,
    )

    # Set by _resume_or_launch on each start/restart; drives launch_note().
    # None until the op runs (nothing decided yet).
    _resumed: bool | None = None

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

    def start(self, ctx: RunContext) -> str:
        """The pane command for ``session create``: resume the stored
        session if its transcript exists, else launch fresh."""
        return self._resume_or_launch(ctx)

    def resume(self, ctx: RunContext) -> str:
        """The pane command for ``session resume``: symmetric with
        :meth:`start`. The orchestrator kills the old tmux BEFORE calling
        this (R7), so the probe decides resume-vs-launch with the old
        process already dead."""
        return self._resume_or_launch(ctx)

    def launch_note(self) -> str | None:
        if self._resumed is None:
            return None
        return (
            "Existing Claude Code session found. Resuming..."
            if self._resumed
            else "No existing Claude Code session. Starting a new one..."
        )

    def _resume_or_launch(self, ctx: RunContext) -> str:
        """Read (or mint) the stored session id, probe the launch target
        for its transcript, and return the single ``sh -c`` pane command
        that echoes the visible decision and ``exec``s ``claude``."""
        sid = self._session_id()
        launch_target = ctx.admin_target() if self._admin else ctx.agent_target()
        resume = launch_target is not None and self._transcript_exists(launch_target, sid)
        self._resumed = resume

        if resume:
            identity = ["--resume", sid]
            msg = f"agentworks harness integration (claude-code): resuming session {self._session_name}"
        else:
            identity = ["--session-id", sid]
            msg = f"agentworks harness integration (claude-code): starting new session {self._session_name}"
        tokens = [*identity, "--name", self._session_name, *self._config_flags()]
        argv = " ".join(shlex.quote(token) for token in tokens)
        # A single ``sh -c`` so the whole thing survives the ``exec``
        # wrapping the tmux pane applies (``exec`` takes one simple
        # command): the login shell execs this sh, which echoes then
        # execs claude, so the pane becomes Claude. The message and the
        # generated argv carry no ``{{word}}`` tokens, so the core
        # template-var substitution does not mangle them.
        inner = f"echo {shlex.quote(msg)}; exec claude {argv}"
        return f"sh -c {shlex.quote(inner)}"

    def _session_id(self) -> str:
        """The stored Claude session id, minted (and recorded in the state
        blob) on first use. A v4 uuid: Claude accepts any valid uuid at
        ``--session-id``, and global uniqueness keeps the transcript probe
        slug-independent. ``self._state`` rides inside the session node's
        full namespaced blob, which the manager persists after the op, so
        a minted id survives to the next restart."""
        sid = self._state.get("session_id")
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

    def _config_flags(self) -> list[str]:
        """The managed flags then ``extra_args``, each an argv token.
        ``extra_args`` is appended verbatim last so it can carry any flag
        the harness integration does not model (FRD R4)."""
        tokens: list[str] = []
        if self.config.permission_mode is not None:
            tokens += ["--permission-mode", self.config.permission_mode]
        if self.config.model is not None:
            tokens += ["--model", self.config.model]
        tokens += self.config.extra_args
        return tokens

    def _transcript_exists(self, transport: Transport, sid: str) -> bool:
        """True iff the stored session's transcript (``<sid>.jsonl``) exists
        under the projects dir on the launch target. Slug-independent
        (``find`` matches under ANY project directory); shell-neutral (no
        glob reaches the shell). Runs through ``$SHELL -lic`` like the
        readiness probe.

        On restart the orchestrator has already killed the old session, but
        no flush wait is needed: Claude writes transcript turns to the
        ``.jsonl`` incrementally as work happens (not flushed on exit), so a
        killed session's history is already on disk when this probe runs.

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
