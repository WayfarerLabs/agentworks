"""Secret declarations, backends, and the resolve loop.

See ADR 0016 for the model: the ``[secret_config].backends`` chain
(config) names registered backend capabilities
(``SECRET_BACKEND_REGISTRY``), mirrored into the resource Registry as
read-only ``secret-backend`` capability resources; the resolution loop
consumes the ``SecretBackend`` API directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.guide.contract import TopicContribution

from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY
from agentworks.secrets.base import (
    SecretConfig,
    SecretDecl,
)
from agentworks.secrets.env_var import env_var_name_for
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
    "SECRET_BACKEND_REGISTRY",
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
