"""Core types, protocol, and base class for the agentworks secret system.

See ``docs/sdd/2026-06-05-env-and-secrets/`` and
``docs/adrs/00NN-cli-side-secret-injection.md`` (numbered at SDD lock) for
background on why values never persist on the VM and why prompt is just
another SecretSource in the chain.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class SecretDecl:
    """A declared secret. Values are never stored here; only the existence,
    description, and per-backend identifier overrides.

    ``backend_mappings`` is keyed by backend kind (e.g. ``"env_var"``,
    ``"onepassword"``). Value forms per FRD R4:

    - ``str``: backend's identifier for this secret (env var name, op:// URI, etc.).
    - ``dict[str, object]``: structured identifier (for backends whose ID has
      multiple fields, e.g. 1Password ``{vault, item, field}``).
    - ``False``: opt out; skip this backend for this secret regardless of any
      default convention the backend would otherwise apply.
    - key absent: use the backend's default convention if it has one, else
      soft-skip (backend reports as "no mapping" via ``would_attempt``).
    """

    name: str
    description: str
    hint: str | None = None
    backend_mappings: dict[str, str | dict[str, object] | Literal[False]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class SecretBackendConfig:
    """Connection / global config for one secret backend.

    Concrete backends carry their own dataclass subclasses with additional
    fields (account, vault, etc.). The ``kind`` field matches the
    ``[secret_backends.<kind>]`` key.
    """

    kind: str


@dataclass(frozen=True)
class SecretConfig:
    """Top-level [secret_config] table.

    ``backends`` is dual-role: presence enables the backend, list order is the
    resolution precedence. A backend declared in ``[secret_backends.*]`` but
    absent from this list is dormant (its source is never instantiated).
    """

    backends: tuple[str, ...] = ()


class SecretSource(Protocol):
    """Structural type contract for backends that produce secret values.

    Every backend implements this protocol; ``PromptSource`` is just one
    instance whose ``get`` happens to interact with the operator instead of
    reading from a vault. The resolver iterates a configured chain of sources
    in precedence order; first to return a non-None value wins.

    This is a type-only protocol: implementations do not need to inherit from
    it. Most concrete sources inherit from ``SecretSourceBase`` to pick up the
    default ``batch_get``, but a class that structurally implements the four
    members below satisfies the protocol regardless.
    """

    kind: str

    def would_attempt(self, secret: SecretDecl) -> bool:
        """Does this source's CONFIG apply to this secret?

        Determined from config alone (the secret's ``backend_mappings`` plus
        this source's default-convention behavior). Does NOT verify that
        resolution will succeed; ``EnvVarSource.would_attempt(s)`` is True
        even when the env var isn't set. Used at config-load time to surface
        unreachable secrets and by ``agw doctor`` for soft-skip diagnostics.
        """
        ...

    def get(self, secret: SecretDecl) -> str | None:
        """Resolve the secret if this source can. Return None to fall through
        to the next source in the chain. Raises only on hard failures (e.g.
        vault unreachable mid-batch); "not configured for this secret" is
        signaled with None and surfaces upstream via ``would_attempt``.
        """
        ...

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Batch resolve. Backends that authenticate (1Password, Vault)
        override to amortize that cost across the batch. ``PromptSource``
        overrides to emit all prompts in one operator interaction.
        """
        ...


class SecretSourceBase(ABC):
    """Default base class for SecretSource implementations.

    Provides a default ``batch_get`` that loops ``get``. Concrete sources
    inherit from this for the shared default and implement ``would_attempt``
    and ``get`` (plus override ``batch_get`` when the backend benefits from
    amortizing per-batch cost).

    ``SecretSource`` remains the type contract; this base class is purely a
    sharing convenience. Code that does not need the default ``batch_get`` can
    implement ``SecretSource`` structurally without inheriting from this base.
    """

    kind: str

    @abstractmethod
    def would_attempt(self, secret: SecretDecl) -> bool:
        ...

    @abstractmethod
    def get(self, secret: SecretDecl) -> str | None:
        ...

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in secrets:
            value = self.get(s)
            if value is not None:
                out[s.name] = value
        return out
