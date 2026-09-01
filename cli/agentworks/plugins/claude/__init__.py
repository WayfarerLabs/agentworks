"""The ``claude`` system plugin: the Claude Code session harness integration and its
CLI install-command, shipped as a separable, opt-in plugin (R11, R11.1).

The first manifest-carrying migration (Phase 9): a plugin that seats its
``ClaudeCodeIntegration`` into ``HARNESS_INTEGRATION_REGISTRY`` through the ``harness integration``
adapter AND bundles one declarable, the ``claude`` ``user-install-command``
(moved out of ``manifests/builtin/install-commands.yaml`` into this
package's ``manifests/`` subdirectory). It exercises Phase 7's manifest
present-but-disabled parity end to end with a real plugin.

Both rows are present-but-disabled until an operator opts in with
``[plugins] system = ["claude"]``:

- The harness integration row publishes with a ``system-plugin`` origin; a
  ``session-template`` whose tagged ``spec.harness_integration.name`` is
  ``claude-code`` STAYS ready (it does not propagate), and
  ``ensure_harness_integration_enabled`` refuses it at session create/start/restart with the
  "enable plugin `claude`" hint until enabled.
- The ``claude`` install-command row publishes weak (add-if-absent) while
  disabled, so a template's ``user_install_commands = ["claude"]`` finalizes
  cleanly (no unknown-name error) and is refused at use by the Phase 7 recipe
  gate with the same hint until enabled.

``shell`` remains the default harness integration, so the common session path is
unaffected by this migration.

Not migrated in this effort (a conscious partial migration, per
migration-strategy section 1): the ``claude_marketplaces`` /
``claude_plugins`` agent-template and admin fields and the
``install_claude_plugins`` VM-init step. They are Claude-Code-specific but
are neither a capability impl nor a declarable resource, so the plugin unit
as built (R6) has no seat for them; they keep working from core regardless.
"""

from __future__ import annotations

from agentworks.plugins.base import Plugin
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration

PLUGIN = Plugin(
    name="claude",
    description="Claude Code session harness integration and CLI install command",
    capabilities={"harness-integration": (ClaudeCodeIntegration,)},
    # Anchor at this package so its ``manifests/`` subdir (the claude
    # install-command) is found by the bundled-manifest loader.
    manifests="agentworks.plugins.claude",
)
