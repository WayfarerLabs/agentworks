"""The ``grok`` system plugin: Grok Build's session harness integration and
official CLI installer, shipped as one opt-in bundle.

The harness integration and installer publish present-but-disabled until an
operator enables ``[plugins] system = ["grok"]``. Enabling the plugin changes
nothing until a template selects ``spec.harness_integration.name: grok-build``
or an agent-template references the ``grok`` user install command.
"""

from __future__ import annotations

from agentworks.plugins.base import Plugin
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration

PLUGIN = Plugin(
    name="grok",
    description="Grok Build session harness integration and CLI install command",
    capabilities={"harness-integration": (GrokBuildIntegration,)},
    manifests="agentworks.plugins.grok",
)
