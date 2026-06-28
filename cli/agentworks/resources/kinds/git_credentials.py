"""``GitCredentialKind``: framework strategy for the ``"git_credentials"`` kind.

Miss policy ``error`` -- the kind does NOT synthesize. Operators must
explicitly declare every ``[git_credentials.<name>]`` they reference
(from ``admin.git_credentials`` / ``agent_templates.*.git_credentials``).
A typo'd reference like ``admin.git_credentials = ["githb-prod"]``
errors at config load via the framework's miss-policy dispatch, with
the requirement source surfaced in the error message.

The kind is intentionally minimal: validating "the name is published"
is the whole job. Token resolution happens through the secret kind
(each ``GitCredentialConfig`` emits a ``SecretRequirement`` for its
token at finalize time).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.resources.kind import KIND_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.requirement import ResourceRequirement


@dataclass(frozen=True)
class _GitCredentialKind:
    """Implementation of ``ResourceKind`` for ``"git_credentials"``."""

    kind: str = "git_credentials"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None  # ignored under "error"

    def synthesize(
        self,
        requirements: Sequence[ResourceRequirement],
    ) -> None:
        # Unreachable under the "error" miss policy. The Registry's
        # finalize pass raises ConfigError before calling synthesize on
        # error-policy kinds, so this method exists only to satisfy the
        # Protocol; never called in practice.
        raise NotImplementedError(
            "git_credentials kind uses error miss policy; synthesize "
            "is never invoked"
        )


KIND_REGISTRY["git_credentials"] = _GitCredentialKind()
