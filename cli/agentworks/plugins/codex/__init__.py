"""The ``codex`` system plugin: the Codex session harness integration and its CLI
install-command, shipped as a separable, opt-in plugin.

The second tool harness integration, built on the ``claude`` plugin's paved road: it
seats ``CodexIntegration`` into ``HARNESS_INTEGRATION_REGISTRY`` through the ``harness integration``
adapter AND bundles one declarable, the ``codex`` ``user-install-command``,
in this package's ``manifests/`` subdirectory.

Both rows are present-but-disabled until an operator opts in with
``[plugins] system = ["codex"]``:

- The harness integration row publishes with a ``system-plugin`` origin; a
  ``session-template`` naming ``harness_integration = "codex"`` STAYS ready (it does
  not propagate), and ``ensure_harness_integration_enabled`` refuses it at session
  create/restart with the "enable plugin `codex`" hint until enabled.
- The ``codex`` install-command row publishes weak (add-if-absent) while
  disabled, so a template's ``user_install_commands = ["codex"]``
  finalizes cleanly (no unknown-name error) and is refused at use by the
  recipe gate with the same hint until enabled.

``shell`` remains the default harness integration, so the common session path is
unaffected; enabling this plugin changes nothing until a template selects
``harness_integration = "codex"``.
"""

from __future__ import annotations

from agentworks.plugins.base import Plugin
from agentworks.plugins.codex.harness_integration import CodexIntegration

PLUGIN = Plugin(
    name="codex",
    description="Codex session harness integration and CLI install command",
    capabilities={"harness-integration": (CodexIntegration,)},
    # Anchor at this package so its ``manifests/`` subdir (the codex
    # install-command) is found by the bundled-manifest loader.
    manifests="agentworks.plugins.codex",
)
