"""Base interface for session harness integrations.

A harness integration is a capability (see ``capabilities/README.md``): it DECLARES
the shape of its own config block as a model
(``config_model``, which the core validates against), owns the
session's launch-target readiness (the required-commands probe and the
skip/defer/probe/error fork), and produces the tmux pane command string
that runs the workload as its single op (``start(force_new=...)``). Unlike the
thin-wrapper git-credential capability, a harness integration is HELD by a rich
consuming node (the session node), which composes its readiness rather
than walking it (``capabilities/README.md``: "Rich (session over
harness integration)").

The harness integration addresses the tool through its OWN construction-time
identity (``session_name``, its ``target`` object) and reads only the
LEVEL off the operation scope; the scope's name fields are error framing
and the SESSION-level identity guard (:meth:`_check_identity`), never
addressing.
"""

from __future__ import annotations

import shlex
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from agentworks.capabilities.base import Capability, ScopeLevel
from agentworks.command_checks import check_required_commands
from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.resources.reference import ResourceReference
    from agentworks.transports import Transport

    # Structural, TYPE_CHECKING-only: the harness integration satisfies Readiness and
    # reads a target's ``.realized`` / ``.name``, but capabilities/harness_integration/
    # must not import orchestration/ or sessions/ at runtime (layering
    # rule, FRD R1 / HLA package layout). A Protocol keeps the type
    # without the import edge. The members are read-only properties (the
    # harness integration only READS them): the real agent nodes expose ``name`` /
    # ``realized`` as read-only ``@property``, which a read-write
    # attribute Protocol would not structurally satisfy.
    class _Target(Protocol):
        @property
        def name(self) -> str: ...
        @property
        def realized(self) -> bool: ...


@dataclass(frozen=True)
class HarnessStart:
    """A harness integration's pre-launch decision."""

    command: str
    note: str | None = None


def require_commands(
    commands: tuple[str, ...],
    transport: Transport,
    *,
    harness_integration_name: str,
    template_name: str,
    session_name: str,
    target_label: str,
) -> None:
    """Probe every required command with ``$SHELL -lic 'command -v
    <cmd>'``, the same shell flags the tmux pane command uses. Matters
    because PATH additions can live in any of the dotfiles those flags
    source:

    - ``-l`` (login): /etc/profile, ~/.profile, ~/.bash_profile,
      where mise activation and the agentworks profile fragments
      live.
    - ``-i`` (interactive): ~/.bashrc, ~/.zshrc, and any user PATH
      addition guarded by ``[[ $- == *i* ]]`` or ``[ -n "$PS1" ]``.
    - ``-c``: run the probe and exit.

    The probe runs over the SSH command channel without a PTY, so
    shells may emit a "no job control in this shell" warning when
    started interactive. The warning lands on stderr and doesn't
    change the exit status; the probe uses ``check=False`` so
    stderr is discarded.

    One residual gap: tools that gate PATH on ``[[ -t 0 ]]`` (real
    TTY check) won't be visible to the probe. Closing that would
    require requesting a PTY for the probe, which has its own side
    effects. PATH mutations gated on a real TTY are rare; leaving
    uncovered for now.

    Without this check, a missing binary surfaces only as a cryptic
    downstream failure: the pane command dies instantly, the fresh
    per-session tmux server exits, and the next ``server-access``
    call fails against a now-dead socket. Checking up front turns
    that into an actionable error with no partial state to roll
    back (and, for restart, with the old session still running).

    ``target_label`` is passed in (not recomputed): a shared helper
    takes no ``self``, so the caller derives the label the same way the
    imperative call sites do (``VM '<vm>'`` in admin/no-target mode,
    ``agent '<name>'`` otherwise) and hands it in.
    """
    missing = check_required_commands(commands, transport)
    if not missing:
        return
    joined = ", ".join(repr(c) for c in missing)
    verb = "is" if len(missing) == 1 else "are"
    raise StateError(
        f"the '{harness_integration_name}' harness integration (session-template "
        f"'{template_name}') requires {joined}, which {verb} not "
        f"installed or not on PATH for {target_label}.",
        entity_kind="session",
        entity_name=session_name,
        hint=(
            f"Install the missing command(s) on {target_label}, or "
            "create the session with a different template "
            "(--template)."
        ),
    )


def quote_literal_argv(value: str) -> str:
    """Quote one shell argv value while shielding literal ``{{`` text.

    The session manager applies template-variable substitution to the full
    pane command returned by an integration. Workload fields are literal
    harness input, so split adjacent opening braces across concatenated shell
    quote fragments. The target shell reconstructs the exact value while the
    manager never sees a template placeholder. Callers must continue using
    ordinary :func:`shlex.quote` for ``extra_args``, whose placeholders are
    intentionally expanded by the manager.
    """
    chunks: list[str] = []
    start = 0
    for index in range(1, len(value)):
        if value[index - 1 : index + 1] == "{{":
            chunks.append(value[start:index])
            start = index
    chunks.append(value[start:])
    return "".join(shlex.quote(chunk) for chunk in chunks)


class HarnessIntegration(Capability):
    """Capability: configures, runs, and manages one session's workload.

    A harness integration owns the launch-target readiness fork and the
    required-commands probe, and it ADDS the :meth:`start` operation the
    session's service layer consumes to build the tmux pane.

    Subclasses (``ShellIntegration``, ``ClaudeCodeIntegration``) implement the
    start and :meth:`_probe_target` (their own required-command set);
    the fork (:meth:`_run_readiness`), the SESSION-level identity guard
    (:meth:`_check_identity`), and the single-fire guard live here so
    every member shares one copy.
    """

    owner_kind: ClassVar[str] = "session-template"

    def __init__(
        self,
        owner_name: str,  # the session-template name (config owner)
        config: Mapping[str, object],  # the merged harness config, without the tag
        *,
        session_name: str,  # the session's own name (addresses the tool)
        vm_name: str,  # the session's VM ancestor
        workspace_name: str,  # the session's workspace ancestor
        workspace_path: str,  # the workspace's VM-side directory (the pane cd's here)
        target: _Target | None,  # the agent node it runs as; None in admin mode
        admin: bool,  # admin mode (uses ctx.admin_target())
        state: dict[str, object],  # this harness integration's OWN namespace of the persisted blob (mutated in place)
    ) -> None:
        super().__init__(owner_name, config)
        self._session_name = session_name
        self._vm_name = vm_name
        self._workspace_name = workspace_name
        self._workspace_path = workspace_path
        self._target = target
        self._admin = admin
        self._state = state  # mutated in place by the ops; the manager persists it
        self._probed = False  # single-fire guard: the probe runs once per operation

    @property
    def state(self) -> dict[str, object]:
        """The harness integration's per-session state dict: its OWN namespace of the
        session row's ``harness_integration_state`` blob (the platform seam,
        ``sessions/nodes._harness_integration_for_template``, splits the stored blob
        by harness integration name, so no harness integration ever sees another's keys). A
        harness integration reads and mutates it in place during its ops
        (``claude-code`` mints and records its Claude session id on the
        first ``start``); the dict is shared with the session node's full
        blob, which the manager persists after the op. Empty for a
        harness integration that keeps no state (``shell``).
        """
        return self._state

    def config_secret_refs(self) -> tuple[ResourceReference, ...]:
        """The full config-secret references this harness integration declares
        (the secret-kind references the core extracted from its declared
        model, bound at construct into ``self._secret_refs``), sourced to the owning
        session-template, for the holding session node to expose as its
        own ``config_secret_refs`` (what the preflight sweep predicts
        resolvability over) and to derive its bare-name ``secret_refs``
        union from.

        Full references rather than bare names because a prediction
        failure needs the owner/usage framing every other declared
        config secret gets (issue #305). The usage is the capability's
        own prose plus the declaration site: the sweep frames its error
        with the NODE's key (``session/<name>``), which names the
        session but not the template whose ``harness_integration`` table
        named the secret, so the reference carries that locating info itself. A
        public accessor, so the node never reaches into the base
        ``Capability._secret_refs`` private field. Empty for every
        shipped harness integration (none declares a secret); the
        plumbing is here for a future secret-declaring harness integration.
        """
        from dataclasses import replace

        from agentworks.resources.reference import sourced_references

        enriched = tuple(
            replace(
                ref,
                usage=f"{ref.usage}, from the harness_integration of {self.owner_kind} '{self.owner_name}'",
            )
            for ref in self._secret_refs
        )
        return tuple(sourced_references(enriched, (self.owner_kind, self.owner_name)))

    @classmethod
    def hoist_legacy_state(cls, blob: dict[str, object]) -> None:
        """Adopt a pre-namespacing ``harness_integration_state`` blob in place.

        Compatibility (pre-namespacing ``harness_integration_state``): DELETE on the next
        major release, together with the ``claude-code`` override and the
        seam call in ``sessions/nodes._harness_integration_for_template``.

        The seam calls this once with the session's FULL stored blob,
        BEFORE the harness integration's own namespace is split out of it. Rows
        written before the blob was namespaced by harness integration name carry that
        era's keys at the top level; a harness integration that ever wrote
        unnamespaced state overrides this to move its keys into its own
        namespace, idempotently. Only ``claude-code`` ever did, so the
        default is a no-op; the hook lives on the base so the platform
        seam stays integration-agnostic.
        """

    @abstractmethod
    def start(self, ctx: RunContext, *, force_new: bool = False) -> HarnessStart:
        """Choose the raw pane command for a session launch.

        Empty ``command`` means a login shell. Core owns lifecycle and asks for
        a fresh harness conversation only when ``force_new`` is true.
        """

    @abstractmethod
    def _probe_target(self, transport: Transport) -> None:
        """Run the harness integration's required-command probe against ``transport``
        (the resolved launch target). Called by :meth:`_run_readiness` at
        the probe slot; each member names its own commands (``shell``: the
        merged ``required_commands``; ``claude-code``: ``claude``)."""

    def preflight(self, ctx: RunContext) -> None:
        self._run_readiness(ctx, stage="preflight")

    def runup(self, ctx: RunContext) -> None:
        self._run_readiness(ctx, stage="runup")

    @property
    def _target_label(self) -> str:
        """Error-framing label, parity with the imperative call sites:
        admin mode (or no target) names the VM, agent mode names the
        agent. Both members pass this to :func:`require_commands`."""
        if self._admin or self._target is None:
            return f"VM '{self._vm_name}'"
        return f"agent '{self._target.name}'"

    def _run_readiness(self, ctx: RunContext, *, stage: Literal["preflight", "runup"]) -> None:
        """The skip/defer/probe/error readiness fork (including the fifth
        ``scope is None`` loud branch), with the SESSION-level identity
        guard added ahead of the single-fire short-circuit."""
        scope = ctx.operation_scope
        if scope is None:
            # A scope-less context reaching node readiness is an
            # orchestrator bug, not an out-of-scope level: skipping
            # here would silently disable the harness integration forever.
            raise StateError(
                f"session '{self._session_name}': the harness integration received a "
                f"context with no operation scope; the orchestrator must "
                f"attach one (the skip case is out-of-scope-for-the-LEVEL, "
                f"never scope-less)."
            )
        if scope.level is not ScopeLevel.SESSION:
            # Out of scope for the level (a system-scoped doctor scan):
            # there is legitimately no session target here; skip. The
            # identity guard does NOT run on the skip branch (the broader
            # scope legitimately describes more than this session).
            return
        self._check_identity(scope)
        if self._probed:
            return
        if self._admin:
            transport = ctx.admin_target()
        else:
            if self._target is None:
                # Anti-silent-skip: in scope with no target is a
                # selection bug, never something to skip past.
                raise StateError(
                    f"session '{self._session_name}': no launch target for "
                    f"the harness integration readiness (agent mode with no agent "
                    f"node); refusing to skip it."
                )
            if not self._target.realized:
                return  # pending target: defer to runup
            transport = ctx.agent_target()
        if transport is None:
            if stage == "preflight":
                # The command-start context did not carry the target;
                # the op-start context must.
                return
            raise StateError(
                f"session '{self._session_name}': the harness integration reached runup "
                f"with no launch target on the context; the orchestrator "
                f"must hand the op-start context the target transport."
            )
        self._probe_target(transport)
        self._probed = True

    def _check_identity(self, scope: OperationScope) -> None:
        """SESSION-level identity guard: the harness integration's construction-time
        identity must match the operation scope it is handed. A mismatch
        is an orchestrator bug (a context assembled for a different
        session), and the harness integration runs commands on a VM as a user, so
        this RAISES rather than warns.

        Runs on every non-SKIP readiness call, before the single-fire
        short-circuit, so it validates each context; it is cheap
        value-equality, so re-running it costs nothing. The
        ``self._target is not None`` check is explicit so a mis-wired
        agent-mode context with a ``None`` target raises cleanly here
        rather than an ``AttributeError``.
        """
        mismatches: list[str] = []
        if scope.vm != self._vm_name:
            mismatches.append(f"names VM {scope.vm!r} but this harness integration is wired for VM {self._vm_name!r}")
        if scope.workspace != self._workspace_name:
            mismatches.append(
                f"names workspace {scope.workspace!r} but this harness integration is wired for workspace "
                f"{self._workspace_name!r}"
            )
        if scope.session != self._session_name:
            mismatches.append(
                f"names session {scope.session!r} but this harness integration is wired for session "
                f"{self._session_name!r}"
            )
        if scope.admin != self._admin:
            mismatches.append(f"is admin={scope.admin} but this harness integration is wired for admin={self._admin}")
        elif not self._admin:
            target_name = self._target.name if self._target is not None else None
            if scope.agent != target_name:
                mismatches.append(
                    f"names agent {scope.agent!r} but this harness integration runs as agent {target_name!r}"
                )
        if mismatches:
            raise StateError(
                f"session '{self._session_name}': the operation scope "
                f"{'; '.join(mismatches)}; the orchestrator handed a context "
                f"assembled for a different session.",
                entity_kind="session",
                entity_name=self._session_name,
            )
