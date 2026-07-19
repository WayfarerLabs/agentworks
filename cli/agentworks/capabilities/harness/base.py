"""Base interface for session harnesses.

A harness is a capability (see ``capabilities/README.md``): it validates
its own ``harness_config`` block (``validate_config``), owns the
session's launch-target readiness (the required-commands probe and the
skip/defer/probe/error fork), and produces the tmux pane command string
that runs the workload as its ops (``start`` / ``restart``). Unlike the
thin-wrapper git-credential capability, a harness is HELD by a rich
consuming node (the session node), which composes its readiness rather
than walking it (``capabilities/README.md``: "Rich (session over
harness)").

The harness addresses the tool through its OWN construction-time
identity (``session_name``, its ``target`` object) and reads only the
LEVEL off the operation scope; the scope's name fields are error framing
and the SESSION-level identity guard (:meth:`_check_identity`), never
addressing.
"""

from __future__ import annotations

import shlex
from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from agentworks.capabilities.base import Capability, ScopeLevel
from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.transports import Transport

    # Structural, TYPE_CHECKING-only: the harness satisfies Readiness and
    # reads a target's ``.realized`` / ``.name``, but capabilities/harness/
    # must not import orchestration/ or sessions/ at runtime (layering
    # rule, FRD R1 / HLA package layout). A Protocol keeps the type
    # without the import edge. The members are read-only properties (the
    # harness only READS them): the real agent nodes expose ``name`` /
    # ``realized`` as read-only ``@property``, which a read-write
    # attribute Protocol would not structurally satisfy.
    class _Target(Protocol):
        @property
        def name(self) -> str: ...
        @property
        def realized(self) -> bool: ...


def require_commands(
    commands: tuple[str, ...],
    transport: Transport,
    *,
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
    back (and, at restart, with the old session still running).

    ``target_label`` is passed in (not recomputed): a shared helper
    takes no ``self``, so the caller derives the label the same way the
    imperative call sites do (``VM '<vm>'`` in admin/no-target mode,
    ``agent '<name>'`` otherwise) and hands it in.
    """
    missing: list[str] = []
    for cmd in commands:
        inner = f"command -v {shlex.quote(cmd)} >/dev/null 2>&1"
        probe = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if not probe.ok:
            missing.append(cmd)
    if not missing:
        return
    joined = ", ".join(repr(c) for c in missing)
    verb = "is" if len(missing) == 1 else "are"
    raise StateError(
        f"template '{template_name}' requires {joined}, which "
        f"{verb} not installed or not on PATH for {target_label}.",
        entity_kind="session",
        entity_name=session_name,
        hint=(
            f"Install the missing command(s) on {target_label}, or "
            "create the session with a different template "
            "(--template)."
        ),
    )


class Harness(Capability):
    """Capability: configures, runs, and manages one session's workload.

    A harness owns the launch-target readiness fork and the
    required-commands probe, and it ADDS the op surface (:meth:`start` /
    :meth:`restart`, the pane command string) the session's service layer
    consumes to build the tmux pane.

    Subclasses (``ShellHarness``, ``ClaudeCodeHarness``) implement the
    two ops and :meth:`_probe_target` (their own required-command set);
    the fork (:meth:`_run_readiness`), the SESSION-level identity guard
    (:meth:`_check_identity`), and the single-fire guard live here so
    every member shares one copy.
    """

    owner_kind: ClassVar[str] = "session-template"

    def __init__(
        self,
        owner_name: str,  # the session-template name (config owner)
        config: Mapping[str, object],  # the merged harness_config blob
        *,
        session_name: str,  # the session's own name (addresses the tool)
        vm_name: str,  # the session's VM ancestor
        workspace_name: str,  # the session's workspace ancestor
        target: _Target | None,  # the agent node it runs as; None in admin mode
        admin: bool,  # admin mode (uses ctx.admin_target())
        state: dict[str, object],  # per-session persisted blob (mutated in place)
    ) -> None:
        super().__init__(owner_name, config)
        self._session_name = session_name
        self._vm_name = vm_name
        self._workspace_name = workspace_name
        self._target = target
        self._admin = admin
        self._state = state  # mutated in place by the ops; the manager persists it
        self._probed = False  # single-fire guard: the probe runs once per operation

    @property
    def state(self) -> dict[str, object]:
        """The harness's per-session state blob. A harness reads and
        mutates it in place during its ops (``claude-code`` mints and
        records its Claude session id on the first ``start``); the session
        manager reads this property after the op and persists it to the
        session row. Empty for a harness that keeps no state (``shell``).
        """
        return self._state

    def secret_refs(self) -> tuple[str, ...]:
        """The secret names this harness declares (the secret-kind
        references :meth:`validate_config` returned, bound at construct
        into ``self._secret_refs``), for the holding session node to fold
        into its own ``secret_refs`` union.

        Mirrors how :class:`GitCredentialProvider` exposes ``secret_name``
        to its holder, so the node consumes a public accessor rather than
        reaching into the base ``Capability._secret_refs`` private field.
        Empty for both built-ins (``shell`` / ``claude-code`` declare no
        secrets); the plumbing is here for a future secret-declaring
        harness.
        """
        return tuple(ref.name for ref in self._secret_refs)

    @classmethod
    def merge_config(
        cls, base: Mapping[str, object], child: Mapping[str, object]
    ) -> dict[str, object]:
        """Inheritance-time blob merge for a same-harness parent/child
        pair (FRD R5). Default: shallow child-wins. Overridden per
        capability where a key needs richer combination (``shell`` unions
        ``required_commands``). Runs classmethod-side from the resolver's
        ``_merge_pair`` walk with no instance yet, exactly as
        :meth:`validate_config` does.
        """
        return {**base, **child}

    @abstractmethod
    def start(self, ctx: RunContext) -> str:
        """The raw pane command string for ``session create`` (empty
        string = login shell only). Template-var substitution and
        ``exec`` wrapping stay OUTSIDE this (the call site applies
        them)."""

    @abstractmethod
    def restart(self, ctx: RunContext) -> str:
        """The raw pane command string for ``session restart``. Assembled
        AFTER the old process is killed, so a state-aware harness decides
        resume-vs-launch with it already dead."""

    @abstractmethod
    def _probe_target(self, transport: Transport) -> None:
        """Run the harness's required-command probe against ``transport``
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

    def _run_readiness(
        self, ctx: RunContext, *, stage: Literal["preflight", "runup"]
    ) -> None:
        """The skip/defer/probe/error readiness fork (including the fifth
        ``scope is None`` loud branch), with the SESSION-level identity
        guard added ahead of the single-fire short-circuit."""
        scope = ctx.operation_scope
        if scope is None:
            # A scope-less context reaching node readiness is an
            # orchestrator bug, not an out-of-scope level: skipping
            # here would silently disable the harness forever.
            raise StateError(
                f"session '{self._session_name}': the harness received a "
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
                    f"the harness readiness (agent mode with no agent "
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
                f"session '{self._session_name}': the harness reached runup "
                f"with no launch target on the context; the orchestrator "
                f"must hand the op-start context the target transport."
            )
        self._probe_target(transport)
        self._probed = True

    def _check_identity(self, scope: OperationScope) -> None:
        """SESSION-level identity guard: the harness's construction-time
        identity must match the operation scope it is handed. A mismatch
        is an orchestrator bug (a context assembled for a different
        session), and the harness runs commands on a VM as a user, so
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
            mismatches.append(
                f"names VM {scope.vm!r} but this harness is wired for VM "
                f"{self._vm_name!r}"
            )
        if scope.workspace != self._workspace_name:
            mismatches.append(
                f"names workspace {scope.workspace!r} but this harness is "
                f"wired for workspace {self._workspace_name!r}"
            )
        if scope.session != self._session_name:
            mismatches.append(
                f"names session {scope.session!r} but this harness is wired "
                f"for session {self._session_name!r}"
            )
        if scope.admin != self._admin:
            mismatches.append(
                f"is admin={scope.admin} but this harness is wired for "
                f"admin={self._admin}"
            )
        elif not self._admin:
            target_name = self._target.name if self._target is not None else None
            if scope.agent != target_name:
                mismatches.append(
                    f"names agent {scope.agent!r} but this harness runs as "
                    f"agent {target_name!r}"
                )
        if mismatches:
            raise StateError(
                f"session '{self._session_name}': the operation scope "
                f"{'; '.join(mismatches)}; the orchestrator handed a context "
                f"assembled for a different session.",
                entity_kind="session",
                entity_name=self._session_name,
            )
