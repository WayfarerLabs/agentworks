"""The ``onepassword`` system plugin: the 1Password ``op`` CLI secret
backend, shipped as a separable, opt-in plugin (R11, R11.1).

The first real migration out of the core (Phase 8): a capability-only
plugin (no bundled manifests) that seats its ``OnePasswordBackend`` class
into ``SECRET_BACKEND_REGISTRY`` and publishes a ``secret-backend`` row with a
``system-plugin`` origin. The row is present-but-disabled until an
operator opts in with ``[plugins] system = ["onepassword"]``; while
disabled it is excluded from the active backend chain and secret
resolution, so a secret whose only mapping targets ``onepassword`` fails
with the "enable plugin `onepassword`" hint (LLD b) rather than a generic
unreachable message.

The plugin NAME is ``onepassword`` (matching the backend's registry name),
not ``1password``: a leading digit is not a legal Python identifier, so
``agentworks.plugins.1password`` would be an invalid module and origin
source.
"""

from __future__ import annotations

from agentworks.plugins.base import Plugin
from agentworks.plugins.onepassword.backend import OnePasswordBackend

PLUGIN = Plugin(
    name="onepassword",
    description="1Password secret backend (op CLI)",
    capabilities={"secret-backend": (OnePasswordBackend,)},
)
