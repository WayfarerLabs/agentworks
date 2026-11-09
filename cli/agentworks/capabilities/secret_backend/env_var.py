"""The ``env-var`` secret backend.

Its conventional variables, ``AW_SECRET_<NAME>``, are carriers: they exist
only to bring a value to this backend. Agentworks hands each resolved secret
to what needs it explicitly, so no program it starts needs a carrier, and a
long-lived one (Lima's host agent, a WSL2 keepalive, an editor) would keep it
for as long as it runs. The entry point therefore moves them out of the
process environment with :func:`withhold_carriers` before anything starts,
and every child inherits an environment without them. A variable that a
secret's ``env-var`` mapping names outside the ``AW_SECRET_`` prefix is the
operator's own, there for other programs too, and stays where it is.

Only children are covered: the environment block this process started with
stays readable to the same user and to root (``/proc/<pid>/environ``,
``ps eww``) for as long as it runs.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.capabilities.secret_backend.client import (
    BackendFailed,
    BackendMissing,
    BackendPreview,
    BackendResolution,
    BackendResolved,
    FailureReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    PreviewAvailable,
    PreviewFailed,
    PreviewMissing,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pydantic import BaseModel

    from agentworks.resources.graph import Readiness


CARRIER_PREFIX = "AW_SECRET_"

_withheld: dict[str, str] = {}
"""The carriers :func:`withhold_carriers` took out of ``os.environ``, by variable name."""


def env_var_name_for(secret_name: str) -> str:
    """Return the default ``AW_SECRET_<NAME>`` environment variable."""
    return CARRIER_PREFIX + secret_name.upper().replace("-", "_")


def _key(name: str) -> str:
    # Windows environment names are case-insensitive, and os.environ upper-cases them there.
    return name.upper() if os.name == "nt" else name


def withhold_carriers() -> None:
    """Move every ``AW_SECRET_`` variable out of ``os.environ``, where only this backend still reads it.

    Called once by the entry point, before anything starts a child process:
    each child inherits ``os.environ``, so none inherits a carrier.
    """
    for name in [name for name in os.environ if _key(name).startswith(CARRIER_PREFIX)]:
        _withheld[_key(name)] = os.environ.pop(name)


def read_variable(name: str) -> str | None:
    """The value the operator's environment gave ``name``, a withheld carrier included."""
    value = os.environ.get(name)
    return _withheld.get(_key(name)) if value is None else value


@contextmanager
def masking(names: list[str]) -> Iterator[None]:
    """Hide ``names`` from this backend for the duration, restoring them afterwards, even on an exception."""
    saved_environ = {name: os.environ.pop(name) for name in names if name in os.environ}
    saved_withheld = {_key(name): _withheld.pop(_key(name)) for name in names if _key(name) in _withheld}
    try:
        yield
    finally:
        os.environ.update(saved_environ)
        _withheld.update(saved_withheld)


class EnvVarSourceConfig(AgwModel):
    """The tag-only config for an env-var source."""

    name: Literal["env-var"]


class EnvVarMapping(AgwRootModel[NonEmptyStr]):
    """An explicit environment-variable name for one secret lookup."""


def _read(request: SecretLookupRequest) -> str | None:
    mapping = request.mapping
    env_name = mapping.root if isinstance(mapping, EnvVarMapping) else env_var_name_for(request.name)
    return read_variable(env_name)


class _EnvVarClient:
    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        results: dict[str, BackendPreview] = {}
        for request in requests:
            value = _read(request)
            if value is None:
                results[request.name] = PreviewMissing()
            elif "\0" in value:
                results[request.name] = PreviewFailed(FailureReason.MALFORMED_VALUE)
            else:
                results[request.name] = PreviewAvailable()
        return results

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        results: dict[str, BackendResolution] = {}
        for request in requests:
            value = _read(request)
            if value is None:
                results[request.name] = BackendMissing()
            elif "\0" in value:
                results[request.name] = BackendFailed(FailureReason.MALFORMED_VALUE)
            else:
                results[request.name] = BackendResolved(value)
        return results


class _EnvVarContext(AbstractContextManager[SecretSourceClient]):
    def __enter__(self) -> SecretSourceClient:
        return _EnvVarClient()

    def __exit__(self, *args: object) -> None:
        return None


class EnvVarBackend(SecretBackend):
    """Resolve secrets from operator-side environment variables."""

    contract_version: ClassVar[int] = 1
    config_model: ClassVar[type[AgwModel]] = EnvVarSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = EnvVarMapping
    name: ClassVar[str] = "env-var"
    description: ClassVar[str] = "resolves from AW_SECRET_<NAME> environment variables"
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Environment variables",
        overview="""
        Reads a secret's value from an environment variable. Every secret has one by
        convention, `AW_SECRET_` plus its name upper-cased with hyphens as underscores,
        so a secret needs no mapping at all to be resolvable this way.

        An explicit mapping overrides that name with the variable you actually have. An
        unset variable is an ordinary miss, so resolution can continue to a later source.
        """,
    )
    supports_tty_interaction: ClassVar[bool] = False

    @classmethod
    def backend_readiness(cls) -> Readiness:
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        if mapping is None:
            identifier = env_var_name_for(secret_name)
        elif isinstance(mapping, EnvVarMapping):
            identifier = mapping.root
        else:
            raise ConfigError(f"env-var received {type(mapping).__name__}, not EnvVarMapping")
        return LookupDescription(LookupDisposition.CANDIDATE, identifier)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        return _EnvVarContext()
