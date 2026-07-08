"""Phase 0 vocabulary guards (resource-manifests SDD).

Two renames landed in Phase 0 and are pinned here so partial regressions
cannot slip back in:

- The ``code-declared`` Origin variant became ``built-in`` (with
  ``system-plugin`` / ``external-plugin`` reserved for the plugin
  effort). No source file should mention the old variant again.
- Kind identifiers moved to lower-kebab (``vm-template``, not
  ``vm_template``). The registry keys ARE the canonical vocabulary, so
  the invariant is structural: every key is kebab, and every handler's
  ``kind`` attribute matches its key. TOML section names
  (``vm_templates``, ``secret_backends``) are keys, not kind
  identifiers, and legitimately stay snake_case; a source-wide grep for
  snake spellings would false-positive on them, so the structural check
  is the right guard.
"""

from __future__ import annotations

from pathlib import Path

import agentworks
from agentworks.resources import KIND_REGISTRY

_AGENTWORKS_ROOT = Path(agentworks.__file__).parent


def _iter_source_files() -> list[Path]:
    return sorted(_AGENTWORKS_ROOT.rglob("*.py"))


def test_kind_registry_keys_are_lower_kebab() -> None:
    for key in KIND_REGISTRY:
        assert "_" not in key, f"kind {key!r} is not lower-kebab"
        assert key == key.lower(), f"kind {key!r} is not lowercase"


def test_kind_handlers_match_their_registry_key() -> None:
    for key, handler in KIND_REGISTRY.items():
        assert handler.kind == key, (
            f"KIND_REGISTRY[{key!r}] has mismatched handler.kind {handler.kind!r}"
        )


def test_code_declared_vocabulary_is_gone() -> None:
    offenders = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        if "code-declared" in text or "code_declared" in text:
            offenders.append(path.relative_to(_AGENTWORKS_ROOT))
    assert not offenders, f"old origin vocabulary found in: {offenders}"
