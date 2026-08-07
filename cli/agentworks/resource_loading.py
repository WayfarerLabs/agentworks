"""Shared internals for the declarable-row loaders in ``apt`` and
``install_commands``.

These helpers were extracted from the former ``catalog`` module when its
four kinds split into the two affinity modules (``agentworks.apt`` and
``agentworks.install_commands``). They are the raw-dict field validation
both modules' per-entry loaders need, kept here rather than duplicated in
each so the two loaders stay byte-for-byte consistent.

The ``declared_at`` shim that used to live here is gone with the TOML
resource surface. Those loaders are reached from the manifest decoders
now, which stamp ``declared_at`` from the document, so the rows take the
model's own synthesized default on the way through.
"""

from __future__ import annotations

from agentworks.errors import ConfigError


def _require_field(data: dict[str, object], key: str, context: str) -> object:
    if key not in data:
        raise ConfigError(f"{context}.{key} is required")
    return data[key]


def _require_list(data: dict[str, object], key: str, context: str) -> list[str]:
    val = data.get(key, [])
    if not isinstance(val, list):
        raise ConfigError(f"{context}.{key} must be a list")
    return [str(item) for item in val]
