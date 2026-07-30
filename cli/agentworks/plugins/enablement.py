"""The plugin opt-in enablement source (R9 capability side, R13).

The one enablement source this effort builds: it marks a not-opted-in system
plugin's contributions ``disabled`` with a remediation reason, so a dependent
that references one is not-ready with the enable hint (via the existing fold)
rather than hitting an unknown-name hard error.

It is a builder/finalize INPUT: ``build_registry`` (LLD c) constructs it bound
to ``config`` and threads it into ``Registry.finalize(enablement_sources=...)``,
the same whitelisted builder path the fold's ``build_context`` uses. It reads
only the frozen rows' origins and the bound enabled set: no new registry probe,
no impl construction, so it sits cleanly inside the R11 guard rather than
against it. Phase 4 exercises it via a test that publishes fixture rows and
calls ``finalize`` directly; Phase 5 wires it into ``build_registry``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.resources.graph import DisabledMark

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.config import Config
    from agentworks.resources.graph import EnablementSource


def plugin_enablement_source(config: Config) -> EnablementSource:
    """Build the plugin opt-in source bound to ``config.plugins_enabled``.

    The returned source disables every ``system-plugin``-origin row whose
    ``origin.plugin`` is not in the enabled set, with the remediation reason
    ``enable plugin `<name>``` (the clause the dependent's hint appends; the
    doctor roster renders the "not enabled in [plugins]" STATE phrasing
    separately, off ``SYSTEM_PLUGINS`` vs config, so no mark carries it). A row
    with any other origin (built-in, operator, auto-declared) is untouched.
    """
    enabled = frozenset(config.plugins_enabled)

    def _source(resources: Mapping[str, Mapping[str, object]]) -> dict[tuple[str, str], DisabledMark]:
        marks: dict[tuple[str, str], DisabledMark] = {}
        for kind, kind_dict in resources.items():
            for name, row in kind_dict.items():
                origin = getattr(row, "origin", None)
                if origin is not None and origin.variant == "system-plugin" and origin.plugin not in enabled:
                    marks[(kind, name)] = DisabledMark(
                        reason=f"enable plugin `{origin.plugin}`",
                        source="plugin-opt-in",
                    )
        return marks

    return _source
