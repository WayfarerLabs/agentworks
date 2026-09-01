"""Exception hierarchy for agentworks business logic.

Errors are categorized by *kind* (what went wrong) rather than by source module:

- NotFoundError, AlreadyExistsError, ValidationError, StateError,
  AuthorizationError: clean domain errors that render as a one-liner with no
  traceback.
- ConnectivityError, ExternalError: failures in external systems where the
  full traceback is preserved to the error log for diagnosis.
- ConfigError: config file validation; rendered cleanly.
- UserAbort: control flow signal when the user declines a confirmation.

The optional entity_kind and entity_name attributes carry the "which entity"
dimension (vm, workspace, agent, session, console, ...) without making it part
of the type. The optional hint attribute provides remediation text rendered
on a second line.

The presentation layer (cli.py:_main) catches these and decides how to render.
Business logic must never import typer, call sys.exit, or format output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class AgentworksError(Exception):
    """Base exception for all agentworks business logic errors."""

    def __init__(
        self,
        message: str,
        *,
        entity_kind: str | None = None,
        entity_name: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.entity_kind = entity_kind
        self.entity_name = entity_name
        self.hint = hint


class TokenRejectedError(AgentworksError):
    """An external service definitively rejected a credential token
    (e.g. GitHub answered 401 for a PAT). Distinct from network
    indeterminacy, which never raises; see
    ``GitCredentialProvider.runup``."""


class NotFoundError(AgentworksError):
    """A named entity does not exist (e.g. workspace, vm, session)."""


class AlreadyExistsError(AgentworksError):
    """A create operation collided with an existing entity of the same name."""


class ValidationError(AgentworksError):
    """Invalid user input (bad name, bad spec, value out of range, etc.)."""


class StateError(AgentworksError):
    """Entity exists but is not in a state that supports this operation.

    Examples: VM not running when attaching a session, session not running
    when sending input, console requires --force because a pane is locked.

    Also covers violated internal runtime contracts surfaced across module
    boundaries (a secret read before the resolve pass, a mis-leveled
    operation scope): the code, not the operator, put things in the
    unsupported state.
    """


class MigrationBlockedError(StateError):
    """A migration refused an unmet precondition before changing schema.

    The safe database opener preserves this error's specific remediation
    instead of replacing it with the generic partial-migration recovery
    message. Migration steps must only raise this before changing schema or data.
    """


class AuthorizationError(AgentworksError):
    """Operation refused because the actor lacks permission for the target.

    Distinct from NotFoundError (the target exists and is reachable) and
    StateError (the target's state is fine; the relationship between actor
    and target is what's missing). Example: an agent that hasn't been
    granted access to a workspace.
    """


class BrokenStateError(StateError):
    """Entity is in an irrecoverable state that requires explicit --force.

    Today's sole user is the session manager: a session whose PID is alive
    but whose tmux server is unreachable. Catch separately from StateError
    to surface the --force hint.
    """


class BusyStateError(StateError):
    """The state database is busy: another process holds a lock on the
    database (for example another process inside BEGIN EXCLUSIVE).

    Distinct from other StateErrors because it is transient: retrying once
    the other connection finishes is expected to succeed, unlike a durable
    state problem such as a malformed schema. Named for the kind, not the
    entity, per this module's taxonomy; ``entity_kind`` carries "database".
    Takes no arguments: the message and hint are fixed by the type itself,
    so no caller can inject remediation prose into the message field. One
    consequence: instances are not picklable, since the default exception
    reduction replays construction with the original positional message.
    Nothing serializes an exception today; if that ever becomes a real
    contract, solve it once across the whole AgentworksError hierarchy
    rather than special-casing this subtype.

    Covers every state-database busy surface: inspect_schema's
    classification (the writable path and Database.check_schema), the
    read-only constructor's own connect (used directly by
    open_completion_database, doctor, and the guide service), and the
    migration lock. ``BackupError`` remains separately scoped to the
    backup and restore operation boundary (``_raise_sqlite_error``); this
    type does not cover it.
    """

    def __init__(self) -> None:
        super().__init__(
            "state database is busy",
            entity_kind="database",
            hint="Retry after the other database user finishes.",
        )


class ConnectivityError(AgentworksError):
    """Network or transport-level failure (SSH, Tailscale, host unreachable)."""


class SecretUnavailableError(AgentworksError):
    """A secret outcome was unavailable or interaction was refused.

    Complete resolution selects this type when its first failed outcome is
    unavailable for any value-free reason, or when the only eligible source
    required interaction that the operation's exact policy refused. The hint
    retains every failed outcome in request order without values.
    """


class SecretMappingError(SecretUnavailableError):
    """A source with a configured mapping reports a definitive hard miss.

    This differs from a soft unavailable outcome, which permits the next
    configured source to try. Connectivity alone maps to
    ``ConnectivityError``. Authentication, deadlines, provider failures,
    malformed values, protocol violations, and unexpected failures map to
    ``ExternalError``.
    """


class ExternalError(AgentworksError):
    """An external system failed in a non-connectivity way.

    Examples: a platform API rejected a request, tar exited nonzero, a
    manifest file was malformed, a source ref could not be resolved.
    """


class ProvisioningError(ExternalError):
    """VM provisioning against a platform backend (Azure, Proxmox, Lima)
    failed. Named for the activity: "provisioner" as a noun is retired
    (the class concept is the VM platform).
    """


class BackupError(ExternalError):
    """A backup operation failed (tar, scp, snapshot)."""


class ConfigError(AgentworksError):
    """Config file is missing, malformed, or contains invalid values.

    Named for its source rather than its kind: it carries a distinct "Configuration
    error:" rendering at the top level, which is why it survives as its own type
    rather than collapsing into ValidationError. Treat as a special case of the
    kind-based taxonomy, not a parallel "by source" axis.
    """


class ConfigFileNotFoundError(ConfigError):
    """The selected Agentworks configuration file does not exist."""


class InheritanceCycleError(ConfigError):
    """An ``inherits`` chain that loops back on itself.

    A distinct type because the finalize build walk has to tell this one
    failure apart from every other ``ConfigError`` a merge can raise: a
    cyclic chain has no effective declaration, so the walk degrades to the
    row's own declaration rather than raising (its ``dependencies`` is
    total by contract), and the canonical cycle pass reports the loop a
    moment later, before anything reads the graph. Catching plain
    ``ConfigError`` there would swallow real errors as well, which is how
    a graph ends up quietly missing an edge.
    """


class UserAbort(AgentworksError):
    """User signaled they want to stop: declined a confirmation, hit Ctrl-C at an
    interactive prompt, or closed stdin (EOF).

    Not really an error -- a control flow signal. Caught separately so the
    renderer can use a neutral phrasing instead of "Error: ...".
    """


def inheritance_cycle_error(kind: str, chain: Sequence[str]) -> InheritanceCycleError:
    """The error for an ``inherits`` loop, given the chain that closed it.

    Shared by the four template resolvers so the four safety-net guards
    cannot drift in shape from each other or from the framework's own
    cycle pass (``resources/registry.py``), which renders the same
    ``a -> b -> a`` path.
    """
    return InheritanceCycleError(f"{kind} inheritance cycle detected: {' -> '.join(chain)}")


def unknown_template_error(
    *,
    kind: str,
    label: str,
    name: str,
    available: Iterable[str],
) -> NotFoundError:
    """Build the ``NotFoundError`` for a template name that isn't declared.

    ``kind`` is the registry kind (e.g. ``"workspace-template"``) carried as
    ``entity_kind``; ``label`` is its human form (e.g. ``"workspace
    template"``) used in the message and hint. The hint lists the live
    declared names for that kind so the operator can correct the name in
    place; when none are declared it says so plainly rather than offering an
    empty list. It deliberately never points at config.toml, which is
    deprecated for resources. Shared by the four template resolvers and the
    ``require_declared_template`` re-point validator so the hint shapes stay
    uniform.
    """
    names = sorted(available)
    hint = f"available {label}s: {', '.join(names)}" if names else f"no {label}s are declared"
    return NotFoundError(
        f"Unknown {label}: {name}",
        entity_kind=kind,
        entity_name=name,
        hint=hint,
    )
