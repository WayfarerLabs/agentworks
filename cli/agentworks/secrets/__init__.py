"""Secret declarations, backends, and the resolve loop.

The capability contract and registry live in
``agentworks.capabilities.secret_backend``. This package owns declarations,
resolution, inspection, and orchestration.
"""

from __future__ import annotations

from agentworks.secrets.base import (
    SecretConfig,
    SecretDecl,
)
from agentworks.secrets.orchestration import (
    SecretTarget,
    compute_needed_secrets,
    resolve_for_command,
)
from agentworks.secrets.outcomes import (
    ResolutionCategory,
    ResolutionDetail,
    ResolutionOutcome,
    ResolutionRemediation,
)
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolve import validate_chain
from agentworks.secrets.sources import SecretSourceDecl, publish_builtin_secret_sources

__all__ = [
    "TtyInteractionPolicy",
    "ResolutionCategory",
    "ResolutionDetail",
    "ResolutionOutcome",
    "ResolutionRemediation",
    "SecretConfig",
    "SecretDecl",
    "SecretSourceDecl",
    "SecretTarget",
    "compute_needed_secrets",
    "publish_builtin_secret_sources",
    "resolve_for_command",
    "validate_chain",
]
