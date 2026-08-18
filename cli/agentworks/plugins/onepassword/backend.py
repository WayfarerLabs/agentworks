"""The ``onepassword`` secret backend: resolves values through the
1Password ``op`` CLI (v2). A capability implementation, consumed by the
resolution loop through the ``SecretBackend`` API.

Transport is a subprocess shell-out to
``op read --no-newline [--account <acct>] op://<vault>/<item>/<field>``
(explicit argv, never a shell string; resolved values are never logged).
1Password Connect and any Python SDK are deliberately out of scope: the
backend depends only on the operator's own ``op`` read access (whether from
``op signin`` or the 1Password app's CLI integration) and its ambient env.
Source config carries the optional account selector and operation timeout.

There is no separate sign-in pre-check: the actual ``op read`` is the only
liveness probe. ``op whoami`` is not reliable for this, because under the
1Password app's CLI integration it can report "not signed in" even when
``op read`` works (the app holds auth and there is no CLI session token for
whoami to report). A signed-out state is therefore detected from a failing
``op read`` (see the marker classification below).

The ``--account`` selection path (the flag name, and that it may precede the
positional reference) matches 1Password CLI v2 docs. Tests deliberately use
a closed fake-provider boundary so they neither depend on nor authenticate
through an operator's credentials; real multi-account parsing therefore
remains externally unexercised.

Mapping is required (there is no derive-from-name convention). Each mapping
is one native ``op://vault/item/field`` reference string; an optional
``[section/]`` segment is allowed. Account selection belongs to source
config, not the per-secret mapping.
"""

from __future__ import annotations

import subprocess
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, NoReturn

from pydantic import AfterValidator, BaseModel, Field

from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretClientFailure,
    SecretClientFailureKind,
    SecretClientRemediation,
    SecretClientTimeout,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.graph import Readiness

_OP_BINARY = "op"
"""The 1Password CLI executable, resolved on PATH."""

# ``op`` exposes a flat exit status: 0 on success, 1 for essentially every
# failure (auth, missing item, transport). It does NOT give distinct exit
# codes for "not signed in" vs "no such item", so we classify a failing
# ``op read`` by matching stderr substrings. There is NO sign-in pre-check:
# ``op whoami`` is not a reliable liveness probe, because under the 1Password
# app's CLI integration it reports "not signed in" even when ``op read``
# works, so a whoami gate would abort a working setup. Classification of a
# failing read: signed-out markers -> authentication failure; the narrow
# not-found markers -> hard mapping failure; anything else -> external failure
# (the fail-safe halt). The not-found markers are deliberately NARROW and
# item/field-specific:
# a broad marker like "no such" would also match a Go-style transport error
# ("dial tcp: lookup ...: no such host") and mislabel connectivity as a hard
# mapping error with misleading remediation. Anything not matched by the
# signed-out or not-found markers falls through to the fixed external failure
# category. Raw stderr never leaves this boundary.
_SIGNED_OUT_MARKERS = (
    "not currently signed in",
    "no account found",
    "session expired",
    "account is not signed in",
)
_NOT_FOUND_MARKERS = (
    "isn't an item",
    "isn't a field",
    "no such item",
    "no such field",
)


@dataclass(frozen=True, slots=True)
class _BoundedRead:
    """A native-result-free projection of one final-client subprocess."""

    value: str | None = None
    failure: SecretClientFailureKind | None = None
    timed_out: bool = False

    def __repr__(self) -> str:
        return "_BoundedRead(value=<redacted>)"


def _bounded_read(args: list[str], *, timeout: float) -> _BoundedRead:
    """Run one bounded read and project native failures to fixed categories."""
    try:
        completed = subprocess.run(  # noqa: S603 - explicit argv, no shell
            [_OP_BINARY, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _BoundedRead(timed_out=True)
    except OSError:
        return _BoundedRead(failure=SecretClientFailureKind.CONNECTIVITY)

    if completed.returncode == 0:
        return _BoundedRead(value=completed.stdout)

    stderr = completed.stderr.lower()
    if any(marker in stderr for marker in _SIGNED_OUT_MARKERS):
        failure = SecretClientFailureKind.AUTHENTICATION
    elif any(marker in stderr for marker in _NOT_FOUND_MARKERS):
        failure = SecretClientFailureKind.HARD_MAPPING
    else:
        failure = SecretClientFailureKind.EXTERNAL
    return _BoundedRead(failure=failure)


def _raise_client_failure(kind: SecretClientFailureKind) -> NoReturn:
    remediation = {
        SecretClientFailureKind.HARD_MAPPING: SecretClientRemediation.CHECK_MAPPING,
        SecretClientFailureKind.AUTHENTICATION: SecretClientRemediation.SIGN_IN,
        SecretClientFailureKind.CONNECTIVITY: SecretClientRemediation.CHECK_CONNECTIVITY,
        SecretClientFailureKind.EXTERNAL: SecretClientRemediation.RETRY,
    }[kind]
    raise SecretClientFailure(kind=kind, remediation=remediation) from None


_TIMEOUT_GUIDANCE = (
    "a pending approval prompt in the 1Password desktop app is a common "
    "cause; approve or dismiss it and retry. `op whoami` can report signed "
    "out even when desktop-app integration still works, so it is not a "
    "reliable way to rule this out"
)
"""Fixed, backend-authored prose surfaced on a timeout (never derived from
``op``'s own output, which this backend keeps out of rendered diagnostics
entirely). Named after field evidence: a pending desktop-app approval
prompt is the most common real cause of an ``op`` timeout, and the
adjacent ``op whoami`` trap ("not signed in" even when reads work) sends
operators chasing the wrong fix."""


def _raise_client_timeout() -> NoReturn:
    raise SecretClientTimeout(guidance=_TIMEOUT_GUIDANCE) from None


def _check_op_uri(uri: str) -> str:
    """Reject an ``op://`` reference that is clearly malformed. A valid
    reference is ``op://`` followed by at least three non-empty path
    segments (vault, item, field; an optional section segment may add a
    fourth). Query attributes (``?attribute=otp``) are left to ``op``
    itself."""
    prefix = "op://"
    if not uri.startswith(prefix):
        raise ValueError(
            f"onepassword reference {uri!r} must start with 'op://' "
            f"(an 'op://vault/item/field' reference, optionally with a "
            f"section: 'op://vault/item/section/field')"
        )
    path = uri[len(prefix) :].split("?", 1)[0]
    segments = path.split("/")
    if len(segments) < 3 or not all(segments[:3]):
        raise ValueError(
            f"onepassword reference {uri!r} is malformed; expected "
            f"'op://vault/item/field' with non-empty vault, item, and field"
        )
    return uri


OpUri = Annotated[NonEmptyStr, AfterValidator(_check_op_uri)]
"""A native 1Password reference, ``op://vault/item/field``."""


class OnePasswordSourceConfig(AgwModel):
    """Shared config for one 1Password source."""

    name: Literal["onepassword"]
    account: NonEmptyStr | None = None
    timeout: float = Field(default=30.0, gt=0, allow_inf_nan=False)


class OnePasswordMapping(AgwRootModel[OpUri]):
    """One native ``op://`` lookup address."""


class _OnePasswordClient:
    def __init__(self, config: OnePasswordSourceConfig) -> None:
        self._config = config

    def prepare(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> None:
        return None

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> Mapping[str, str]:
        resolved: dict[str, str] = {}
        for request in requests:
            mapping = request.mapping
            if not isinstance(mapping, OnePasswordMapping):
                continue
            args = ["read", "--no-newline"]
            if self._config.account is not None:
                args += ["--account", self._config.account]
            args.append(mapping.root)
            budget_remaining = remaining_time()
            timeout = self._config.timeout if budget_remaining is None else min(self._config.timeout, budget_remaining)
            if timeout <= 0:
                _raise_client_timeout()
            read = _bounded_read(args, timeout=timeout)
            if read.timed_out:
                _raise_client_timeout()
            if read.failure is not None:
                _raise_client_failure(read.failure)
            assert read.value is not None
            resolved[request.name] = read.value
        return resolved


class _OnePasswordContext(AbstractContextManager[SecretSourceClient]):
    def __init__(self, config: OnePasswordSourceConfig) -> None:
        self._config = config

    def __enter__(self) -> SecretSourceClient:
        return _OnePasswordClient(self._config)

    def __exit__(self, *args: object) -> None:
        return None


class OnePasswordBackend(SecretBackend):
    """Resolves secret values from 1Password via the ``op`` CLI.

    Mapping-required: ``would_attempt`` is True only for a secret that
    carries a mapping for the configured source name; unmapped secrets
    soft-skip (fall through to the next source). There is no
    derive-from-name convention: 1Password addressing is
    vault/item/field, which cannot be inferred from a bare secret name.

    Its final mapping is a bare ``op://`` string. A source's optional
    account selector lives in ``OnePasswordSourceConfig``.

    Native failures are reduced to fixed provider categories before they
    leave the subprocess boundary. Raw output never reaches the typed core.
    """

    contract_version: ClassVar[int] = 2
    config_model: ClassVar[type[AgwModel]] = OnePasswordSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = OnePasswordMapping
    name: ClassVar[str] = "onepassword"
    description: ClassVar[str] = "resolves via the 1Password CLI (op read op://vault/item/field)"
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="1Password",
        overview="""
        Reads a secret's value through the `op` CLI. There is no naming convention to
        fall back on, so a secret is resolvable here only if it declares an explicit
        mapping under `backend_mappings.<source-name>`: the `op://vault/item/field`
        reference 1Password's "Copy Secret Reference" produces (a
        `.../section/field` segment is allowed too).

        The mapping is always a scalar reference. Account selection and timeout belong to
        the configured secret source so several named sources can use this backend with
        different accounts or budgets.

        Declare a `secret-source` that selects the opt-in `onepassword` system plugin,
        then put that source name in the active chain. The backend needs the `op` CLI on
        this host. Resolution may trigger a biometric or re-auth prompt, so inspection
        surfaces report it optimistically rather than probing it.
        """,
    )

    # interactive = True: resolving a onepassword secret may involve
    # operator interaction, because `op read` can trigger a biometric or
    # re-auth prompt. That is the same property the prompt backend has (it
    # asks the operator for the value), so onepassword carries the flag for
    # the same reason, not as a special case.
    #
    # The practical effect: preview_resolution never probes this backend
    # (probing would fire the biometric at every preflight and once per
    # secret in `agw doctor`); it reports onepassword optimistically on
    # would_attempt alone. A non-interactive transport that authenticates
    # without a human (1Password Connect, a service account; not built
    # here) would not be interactive.
    interactive: ClassVar[bool] = True

    @classmethod
    def backend_readiness(cls) -> Readiness:
        """Not-ready when the ``op`` CLI is not on PATH: a pure presence test
        (``shutil.which``), never a store probe, biometric, or re-auth (those
        are resolution-time interactivity, kept optimistically previewed).
        This is the offline readiness R9.6 gives backends: a configured
        onepassword with no ``op`` installed now shows not-ready rather than
        only failing at first resolution."""
        import shutil

        from agentworks.resources.graph import Readiness

        if shutil.which("op") is None:
            return Readiness.blocked("op CLI not installed")
        return Readiness.ready()

    @classmethod
    def external_operation_timeout(cls, config: AgwModel) -> float | None:
        if not isinstance(config, OnePasswordSourceConfig):
            raise ConfigError("onepassword received the wrong source config model")
        return config.timeout

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        if not isinstance(config, OnePasswordSourceConfig):
            raise ConfigError("onepassword received the wrong source config model")
        return _OnePasswordContext(config)

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        # Mapping-required: only mapped secrets are attempted. (The generic
        # ``False`` opt-out is stripped by the resolve loop before it gets
        # here, so ``mapping`` is either a real value or ``None``.)
        return mapping_present

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
        # The op:// reference is an operator-authored safe identifier, never a
        # resolved value. Account selection belongs to the source and is not
        # repeated in every per-secret identifier.
        return mapping.root if isinstance(mapping, OnePasswordMapping) else None
