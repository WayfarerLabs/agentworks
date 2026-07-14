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
    indeterminacy, which never raises -- see
    ``GitCredentialProvider.verify``."""


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


class ConnectivityError(AgentworksError):
    """Network or transport-level failure (SSH, Tailscale, host unreachable)."""


class SecretUnavailableError(AgentworksError):
    """No active secret backend could resolve the requested secret.

    Raised by the resolve loop when every backend in the active chain
    came up empty for at least one needed secret. The ``hint`` field
    carries the list of backends that were tried so the operator can act
    (e.g. set ``AW_SECRET_<NAME>``, configure 1Password, or run
    interactively).
    """


class SecretMappingError(SecretUnavailableError):
    """A backend with a configured mapping reports the mapping doesn't resolve.

    Distinct from a soft miss (where a provider omits the secret from its
    ``batch_get`` result to fall through to the next backend in the
    chain). A provider raises this when the operator has explicitly told
    it where to look and the lookup definitively returns "not present" --
    a 1Password URI pointing at a deleted item, a Vault path with no
    value, etc. The resolve loop halts the chain on this exception so a
    misconfigured persistent store doesn't quietly fall through to a
    prompt.

    Conventional providers (env-var, prompt) keep soft-missing; only
    persistent-store providers raise this. Future per-backend config
    (e.g. a ``strict_on_miss`` field on a ``secret-backend`` manifest)
    could let operators opt persistent stores back into fall-through; not
    wired today since no provider that would honor it ships in this
    surface.

    Transport / authentication failures (vault locked, network down) are
    distinct from a mapping miss and surface as ``ConnectivityError`` or
    ``ExternalError`` per the broader error taxonomy.
    """


class ExternalError(AgentworksError):
    """An external system failed in a non-connectivity way.

    Examples: a platform API rejected a request, tar exited nonzero, a
    catalog file was malformed, a source ref could not be resolved.
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


class UserAbort(AgentworksError):
    """User signaled they want to stop: declined a confirmation, hit Ctrl-C at an
    interactive prompt, or closed stdin (EOF).

    Not really an error -- a control flow signal. Caught separately so the
    renderer can use a neutral phrasing instead of "Error: ...".
    """
