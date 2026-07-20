"""``declared_at`` threading for catalog entries (dissolve-catalog SDD, Phase 2).

The catalog per-entry loaders now accept a ``decls`` section-line map and
stamp each entry's ``declared_at`` from it. The manifest decoders pass the
document's own location, so manifest-loaded catalog entries (the migrated
built-ins under ``manifests/builtin/`` and operator-declared ``resources/*.yaml``
entries) carry a real source location instead of the synthesized sentinel.

The operator-TOML surface stays on the loaders' default synthesized shim (the
real section-line map is local to ``load_config`` and not carried on ``Config``);
that is the deprecated surface and an acceptable gap, asserted here for clarity.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

from agentworks.source_location import synthesized

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.catalog import AptSourceEntry

# A well-formed operator apt-source, expressed once as YAML manifest spec and
# once as a TOML section, so the two operator paths assert against the same
# resource shape.
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

_CUSTOM_APT_SOURCE_TOML = dedent(
    """
    [apt_sources.custom-repo]
    description = "Custom operator apt repository"
    key_url = "https://example.com/key.gpg"
    key_path = "/etc/apt/keyrings/custom.gpg"
    source = "deb [signed-by=/etc/apt/keyrings/custom.gpg] https://example.com stable main"
    source_file = "custom.list"
    """
)


def _write_operator_config(
    tmp_path: Path,
    *,
    toml_body: str = "",
    manifests: dict[str, str] | None = None,
) -> Path:
    """Write a minimal operator config (plus optional TOML catalog entries
    and ``resources/*.yaml`` manifests) and return the config path.
    """
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[operator]\nssh_public_key = "{pub}"\nssh_private_key = "{priv}"\n'
        + toml_body
    )
    if manifests:
        resources = tmp_path / "resources"
        resources.mkdir()
        for filename, content in manifests.items():
            (resources / filename).write_text(content)
    return cfg


def _apt_sources(
    tmp_path: Path,
    *,
    toml_body: str = "",
    manifests: dict[str, str] | None = None,
) -> dict[str, AptSourceEntry]:
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.resources.access import kind_dict

    cfg = load_config(
        _write_operator_config(tmp_path, toml_body=toml_body, manifests=manifests),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    return kind_dict(registry, "apt-source")


def test_builtin_entry_declared_at_points_at_bundled_manifest(tmp_path: Path) -> None:
    """A built-in catalog entry (resolved from the Registry on a no-operator
    config) carries a real ``declared_at`` pointing at its bundled
    ``manifests/builtin/*.yaml`` file, not the synthesized sentinel.
    """
    src = _apt_sources(tmp_path)["github-cli"]

    assert src.declared_at != synthesized()
    assert src.declared_at.file.name == "apt-sources.yaml"
    assert src.declared_at.line >= 1


def test_operator_yaml_entry_declared_at_points_at_operator_file(
    tmp_path: Path,
) -> None:
    """An operator-declared YAML catalog entry carries a ``declared_at``
    pointing at that operator ``resources/*.yaml`` file.
    """
    src = _apt_sources(
        tmp_path, manifests={"custom.yaml": _CUSTOM_APT_SOURCE_MANIFEST}
    )["custom-repo"]

    assert src.declared_at.file.name == "custom.yaml"
    assert src.declared_at.line >= 1


def test_operator_toml_entry_declared_at_stays_synthesized(tmp_path: Path) -> None:
    """Sanity: an operator-TOML catalog entry still loads; its ``declared_at``
    stays synthesized (the deprecated TOML surface does not carry the
    section-line map), which is acceptable.
    """
    src = _apt_sources(tmp_path, toml_body=_CUSTOM_APT_SOURCE_TOML)["custom-repo"]

    assert src.source_file == "custom.list"
    assert src.declared_at == synthesized()


def test_describe_surfaces_location_for_manifest_entry(tmp_path: Path) -> None:
    """The describe path surfaces the location for a manifest catalog entry:
    a built-in apt-source's origin points at its bundled file.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.resources.inspect import describe_resource

    cfg = load_config(_write_operator_config(tmp_path), warn_issues=False)
    registry = build_registry(cfg)

    desc = describe_resource(registry, "apt-source", "github-cli")

    assert desc.origin is not None
    assert desc.origin.source == "agentworks.manifests.builtin/apt-sources.yaml"
