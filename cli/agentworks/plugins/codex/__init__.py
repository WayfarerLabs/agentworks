"""The ``codex`` system plugin: the Codex session harness and its CLI
install-command, shipped as a separable, opt-in plugin.

The second tool harness, built on the ``claude`` plugin's paved road: it
seats ``CodexHarness`` into ``HARNESS_REGISTRY`` through the ``harness``
adapter AND bundles one declarable, the ``codex`` ``user-install-command``,
in this package's ``manifests/`` subdirectory.

Both rows are present-but-disabled until an operator opts in with
``[plugins] system = ["codex"]``:

- The harness row publishes with a ``system-plugin`` origin; a
  ``session-template`` naming ``harness = "codex"`` STAYS ready (it does
  not propagate), and ``ensure_harness_enabled`` refuses it at session
  create/restart with the "enable plugin `codex`" hint until enabled.
- The ``codex`` install-command row publishes weak (add-if-absent) while
  disabled, so a template's ``user_install_commands = ["codex"]``
  finalizes cleanly (no unknown-name error) and is refused at use by the
  recipe gate with the same hint until enabled.

``shell`` remains the default harness, so the common session path is
unaffected; enabling this plugin changes nothing until a template selects
``harness = "codex"``.
"""

from __future__ import annotations

from agentworks.plugins.base import Plugin
from agentworks.plugins.codex.harness import CodexHarness

PLUGIN = Plugin(
    name="codex",
    description="Codex session harness and CLI install command",
    capabilities={"harness": (CodexHarness,)},
    # Anchor at this package so its ``manifests/`` subdir (the codex
    # install-command) is found by the bundled-manifest loader.
    manifests="agentworks.plugins.codex",
)
