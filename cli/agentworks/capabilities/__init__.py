"""Capabilities: code that abstracts different backends and providers
behind uniform interfaces, so agentworks extends without core changes.

Capabilities form a clean layer: framework (``resources/``, ``secrets/``),
then capabilities, then the consuming domains. A capability depends only
on the framework (it returns framework references and constructs from
config) and never imports a consuming domain; consuming
domains depend on capabilities. This subtree makes that layering
physical: one subdir per capability kind (``vm_platform/`` today; the
already-merged secret-backend capability moves in under its own change),
with the shared :class:`Capability` base at the top.
"""

from agentworks.capabilities.base import (
    Capability,
    idempotent_op,
    is_idempotent_op,
)

__all__ = ["Capability", "idempotent_op", "is_idempotent_op"]
