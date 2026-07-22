"""Core types for the agentworks secret system.

Secrets are declarations (``SecretDecl``); values come from the
registered backend capabilities (``agentworks.secrets.backends``)
through the resolution loop (ADR 0016, YAML resource manifests and the
config/resource/capability split). See
``docs/adrs/0013-cli-side-secret-injection.md`` for why values never
persist on the VM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agentworks.declared_resource import DeclaredResource
from agentworks.source_location import SourceLocation, synthesized

MappingValue = str | dict[str, object] | Literal[False]
"""One entry in ``SecretDecl.backend_mappings``: an identifier override
(string or structured), or ``False`` for an explicit opt-out."""


@dataclass(frozen=True, kw_only=True)
class SecretDecl(DeclaredResource):
    """A declared secret. Values are never stored here; only the existence,
    description, and per-backend identifier overrides.

    ``backend_mappings`` is keyed by backend (capability) name
    (``"env-var"``, ``"prompt"``; later ``"onepassword"``, ...). Value
    forms per the env-and-secrets SDD:

    - ``str``: backend's identifier for this secret (env var name, op:// URI, etc.).
    - ``dict[str, object]``: structured identifier (for backends whose ID
      carries more than the bare reference, e.g. 1Password's
      ``{account, reference}`` for pinning a specific account).
    - ``False``: opt out; skip this backend for this secret regardless of any
      default convention the backend would otherwise apply.
    - key absent: use the backend's default convention if it has one, else
      soft-skip (backend reports as "no mapping" via ``would_attempt``).
    """

    # Override the base's optional ``description``: a secret must carry one
    # (it is the operator-facing prompt/hint text), so it is required here.
    # ``= field()`` is load-bearing, NOT decoration: a bare ``description:
    # str`` would inherit the base's ``description = None`` class attribute as
    # its default (dataclass reads the default via ``getattr`` up the MRO), so
    # the field would silently stay optional. ``field()`` with no default
    # forces MISSING, making the argument required as intended.
    description: str = field()
    hint: str | None = None
    backend_mappings: dict[str, MappingValue] = field(default_factory=dict)


DEFAULT_BACKEND_CHAIN: tuple[str, ...] = ("env-var", "prompt")
"""Default backend chain when ``[secret_config].backends`` is absent.

Resolves declared secrets from operator-side env (``AW_SECRET_<NAME>``) first,
then prompts interactively. The chain is operator-overridable via an explicit
``[secret_config]`` block; an explicit empty list ``backends = []`` disables
resolution entirely (operators who don't use secrets pay nothing either way).
"""


@dataclass(frozen=True)
class SecretConfig:
    """Top-level [secret_config] table. Pure config, never published to
    the resource Registry: the chain is a SETTING that names resources
    (like a future active-plugins list would), consumed by the secrets
    subsystem when it validates (``validate_chain``, at
    ``build_registry``) and when it resolves (``resolve_secrets``).

    ``backends`` is dual-role: presence activates the backend, list
    order is the resolution precedence. A declared backend absent from
    this list is dormant (never consulted).

    Default value is ``DEFAULT_BACKEND_CHAIN`` (``env-var``, then ``prompt``).
    The default applies when the operator's TOML has no ``[secret_config]``
    table OR has the table without a ``backends`` key. An explicit
    ``backends = []`` disables resolution entirely.
    """

    backends: tuple[str, ...] = DEFAULT_BACKEND_CHAIN
    declared_at: SourceLocation = field(default_factory=synthesized)
