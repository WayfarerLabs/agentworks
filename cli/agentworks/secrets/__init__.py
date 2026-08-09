"""Secret declarations, backends, and the resolve loop.

The capability contract and registry live in
``agentworks.capabilities.secret_backend``. This package owns declarations,
resolution, inspection, and orchestration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.secret_backend.env_var import env_var_name_for

if TYPE_CHECKING:
    from agentworks.guide.contract import TopicContribution

from agentworks.secrets.base import (
    SecretConfig,
    SecretDecl,
)
from agentworks.secrets.orchestration import (
    SecretTarget,
    compute_needed_secrets,
    resolve_for_command,
)
from agentworks.secrets.resolve import (
    ActiveBackend,
    active_backends,
    resolve_secrets,
    validate_chain,
)


def _load_guide_contributions() -> tuple[TopicContribution, ...]:
    """Load secrets teaching without coupling core secrets imports to guide."""
    from agentworks.secrets.guide_contributions import guide_contributions as load_contributions

    # Importing a submodule also binds that module on its parent package. Keep
    # the public package-level contribution hook callable after this lazy load.
    globals()["guide_contributions"] = _load_guide_contributions
    return load_contributions()


guide_contributions = _load_guide_contributions


__all__ = [
    "ActiveBackend",
    "SecretConfig",
    "SecretDecl",
    "SecretTarget",
    "active_backends",
    "compute_needed_secrets",
    "env_var_name_for",
    "guide_contributions",
    "resolve_for_command",
    "resolve_secrets",
    "validate_chain",
]
