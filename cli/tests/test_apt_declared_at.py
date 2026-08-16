"""``declared_at`` threading for apt entries (dissolve-catalog SDD, Phase 2).

The apt / install-command per-entry loaders now accept a ``decls``
section-line map and stamp each entry's ``declared_at`` from it. The
manifest decoders pass the document's own location, so manifest-loaded
entries (the optional ``apt`` plugin and operator-declared
``resources/*.yaml`` entries) carry a real source location instead of the
synthesized sentinel.

The operator-TOML apt surface is gone entirely now (config.toml hard-errors on
resource sections, ADR 0022), so every operator apt-source is a YAML manifest
with a real ``declared_at``.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

from agentworks.source_location import synthesized
from tests.conftest import write_manifests

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.apt import AptSourceEntry

# A well-formed operator apt-source manifest, authored as raw YAML (rather than
# a ManifestDoc) so these tests can assert on the declaring file's own name.
_CUSTOM_APT_SOURCE_MANIFEST = dedent(
    """
    apiVersion: agentworks/v1
    kind: apt-source
    metadata:
      name: custom-repo
      description: Custom operator apt repository
    spec:
      key_url: https://example.com/key.gpg
      key_path: /etc/apt/keyrings/custom.gpg
      source: "deb [signed-by=/etc/apt/keyrings/custom.gpg] https://example.com stable main"
      source_file: custom.list
    """
)


def _write_operator_config(
    tmp_path: Path,
    *,
    manifests: dict[str, str] | None = None,
) -> Path:
    """Write a minimal operator config plus optional ``resources/*.yaml``
    manifests (keyed by filename, since these tests assert on the declaring
    file's name) and return the config path.
    """
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[operator]\nssh_public_key = "{pub}"\nssh_private_key = "{priv}"\n')
    for filename, content in (manifests or {}).items():
        write_manifests(tmp_path, content, filename=filename)
    return cfg


def _apt_sources(
    tmp_path: Path,
    *,
    manifests: dict[str, str] | None = None,
) -> dict[str, AptSourceEntry]:
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.resources.access import kind_dict

    cfg = load_config(
        _write_operator_config(tmp_path, manifests=manifests),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    return kind_dict(registry, "apt-source")


def test_plugin_entry_declared_at_points_at_bundled_manifest(tmp_path: Path) -> None:
    """An optional apt plugin entry (resolved from the Registry on a
    no-operator config) carries a real ``declared_at`` pointing at its bundled
    manifest, not the synthesized sentinel.
    """
    src = _apt_sources(tmp_path)["github-cli"]

    assert src.declared_at != synthesized()
    assert src.declared_at.file.name == "apt-sources.yaml"
    assert src.declared_at.line >= 1


def test_operator_yaml_entry_declared_at_points_at_operator_file(
    tmp_path: Path,
) -> None:
    """An operator-declared YAML apt-source entry carries a ``declared_at``
    pointing at that operator ``resources/*.yaml`` file.
    """
    src = _apt_sources(tmp_path, manifests={"custom.yaml": _CUSTOM_APT_SOURCE_MANIFEST})["custom-repo"]

    assert src.declared_at.file.name == "custom.yaml"
    assert src.declared_at.line >= 1


# The former ``test_operator_toml_entry_declared_at_stays_synthesized`` was
# removed here: it pinned the deprecated TOML apt-source surface (loads with a
# synthesized ``declared_at``), which the TOML resource sunset (ADR 0022) made
# structurally impossible, since config.toml now hard-errors on [apt_sources.*].


def test_resource_access_surfaces_origin_for_manifest_entry(tmp_path: Path) -> None:
    """Resource access retains a bundled apt source's origin."""
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.resources.access import ResourceIdentity, resolve_resource

    cfg = load_config(_write_operator_config(tmp_path), warn_issues=False)
    registry = build_registry(cfg)

    resolved = resolve_resource(registry, ResourceIdentity("apt-source", "github-cli"))

    assert resolved.origin is not None
    assert resolved.origin.source == "agentworks.plugins.apt/manifests/apt-sources.yaml"
