"""Shared internals for the declarable-row loaders in ``apt`` and
``install_commands``.

These helpers were extracted from the former ``catalog`` module when its
four kinds split into the two affinity modules (``agentworks.apt`` and
``agentworks.install_commands``). They are the small pieces both modules'
per-entry loaders need: raw-dict field validation and the ``declared_at``
default shim. They live here (rather than duplicated in each module) so
the two loaders stay byte-for-byte consistent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from agentworks.errors import ConfigError
from agentworks.source_location import synthesized

if TYPE_CHECKING:
    from agentworks.config import _SectionLineMap
    from agentworks.source_location import SourceLocation


def _require_field(data: dict[str, object], key: str, context: str) -> object:
    if key not in data:
        raise ConfigError(f"{context}.{key} is required")
    return data[key]


def _require_list(data: dict[str, object], key: str, context: str) -> list[str]:
    val = data.get(key, [])
    if not isinstance(val, list):
        raise ConfigError(f"{context}.{key} must be a list")
    return [str(item) for item in val]


class _SynthesizedDecls:
    """Default ``decls`` for the per-entry loaders: every lookup resolves to a
    synthesized ``SourceLocation``. Duck-typed stand-in for config's
    ``_SectionLineMap`` (the loaders only call ``lookup``). Used when entries
    are loaded outside the config loader (the operator-TOML publisher, which
    does not carry the section-line map), so ``declared_at`` falls back to the
    synthesized sentinel. Manifest decoders pass a real fixed-location shim
    (``manifests.decode._decls``) instead, so their entries carry the document
    location.
    """

    def lookup(self, *_path: str) -> SourceLocation:
        return synthesized()


# Module-level singleton; the loaders' declared ``decls`` type is config's
# ``_SectionLineMap``, satisfied structurally (loaders only call ``lookup``).
_SYNTHESIZED_DECLS = cast("_SectionLineMap", _SynthesizedDecls())
