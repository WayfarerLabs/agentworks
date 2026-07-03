"""``GitCredentialKind``: framework strategy for the ``"git-credential"`` kind.

Miss policy ``error`` -- the kind does NOT synthesize. Operators must
explicitly declare every ``[git_credentials.<name>]`` they reference
(from ``admin.git_credentials`` / ``agent_templates.*.git_credentials``).
A typo'd reference like ``admin.git_credentials = ["githb-prod"]``
errors at config load via the framework's miss-policy dispatch, with
the reference source surfaced in the error message.

The kind is intentionally minimal: validating "the name is published"
is the whole job. Token resolution happens through the secret kind
(each ``GitCredentialConfig`` emits a ``SecretReference`` for its
token at finalize time).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class _GitCredentialKind:
    """Implementation of ``ResourceKind`` for ``"git-credential"``."""

    kind: str = "git-credential"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None  # ignored under "error"
    manifest_declarable: bool = True
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(
        self,
        references: Sequence[ResourceReference],
    ) -> None:
        # Unreachable under the "error" miss policy: the Registry's
        # finalize pass raises ConfigError before dispatching to
        # synthesize for error-policy kinds. The method exists to
        # satisfy the Protocol; honors the Phase 2a empty-references
        # contract (FRD R3) by raising the typed framework error so a
        # hypothetical future change that gives the kind a reserved
        # default has an obvious landing pad.
        raise NoUnreferencedDefaultError(
            "the git_credentials kind has no reserved default name; "
            "synthesize is never invoked under the error miss policy"
        )


KIND_REGISTRY["git-credential"] = _GitCredentialKind()
