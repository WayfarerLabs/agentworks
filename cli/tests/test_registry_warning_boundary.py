"""Guard the request-warning boundary around registry composition roots."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# These direct uses deliberately stay pure. Every request-serving caller must
# use ``load_request_registry`` so the old-selector warning is rendered once
# at the request boundary instead of leaking from ``build_registry``.
_PURE_BUILD_REGISTRY_CALLERS = {
    "doctor.py": "doctor passes an explicit ManifestSet to render health rows",
}


def _uses_build_registry(source: str, *, filename: str = "<unknown>") -> bool:
    tree = ast.parse(source, filename=filename)
    direct_import = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "agentworks.bootstrap"
        and any(alias.name == "build_registry" for alias in node.names)
        for node in ast.walk(tree)
    )
    bootstrap_aliases = {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "agentworks.bootstrap" and alias.asname is not None
    }
    bootstrap_aliases.update(
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "agentworks"
        for alias in node.names
        if alias.name == "bootstrap"
    )
    package_aliases = {
        alias.asname or "agentworks"
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name in {"agentworks", "agentworks.bootstrap"}
    }
    qualified_reference = any(
        isinstance(node, ast.Attribute)
        and node.attr == "build_registry"
        and (
            (isinstance(node.value, ast.Name) and node.value.id in bootstrap_aliases)
            or (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "bootstrap"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in package_aliases
            )
        )
        for node in ast.walk(tree)
    )
    return direct_import or qualified_reference


@pytest.mark.parametrize(
    "source",
    [
        "from agentworks.bootstrap import build_registry",
        "import agentworks.bootstrap as boot\nboot.build_registry(config)",
        "from agentworks import bootstrap as boot\nfactory = boot.build_registry",
        "import agentworks.bootstrap\nagentworks.bootstrap.build_registry(config)",
        "import agentworks as aw\naw.bootstrap.build_registry(config)",
    ],
)
def test_build_registry_use_detection(source: str) -> None:
    assert _uses_build_registry(source)


def test_build_registry_use_detection_ignores_unrelated_references() -> None:
    assert not _uses_build_registry("from another_package import build_registry")


def test_only_intentional_pure_callers_use_build_registry() -> None:
    source_root = Path(__file__).parents[1] / "agentworks"
    callers: set[str] = set()
    for path in source_root.rglob("*.py"):
        if _uses_build_registry(path.read_text(encoding="utf-8"), filename=str(path)):
            callers.add(path.relative_to(source_root).as_posix())

    assert callers == set(_PURE_BUILD_REGISTRY_CALLERS), (
        "request-serving registry roots must use load_request_registry; "
        f"unexpected pure callers: {sorted(callers - set(_PURE_BUILD_REGISTRY_CALLERS))}"
    )


def test_build_registry_with_explicit_manifest_set_does_not_render_warnings(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Doctor and verification can safely compose an explicit manifest set."""
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.manifests import load_manifests

    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA")
    priv.write_text("private")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[operator]\nssh_public_key = "{pub}"\nssh_private_key = "{priv}"\n')
    resources = tmp_path / "resources"
    resources.mkdir()
    # An advisory (an AGENTWORKS_* env key the runtime prelude overrides),
    # so the manifest set carries a real issue for the boundary to stay
    # quiet about. An issue-free set would pass this test without proving
    # anything.
    (resources / "warned.yaml").write_text(
        """apiVersion: agentworks/v1
kind: vm-template
metadata:
  name: small
spec:
  cpus: 2
  env:
    AGENTWORKS_VM: nonsense
"""
    )
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)

    config = load_config(config_path, warn_issues=False)
    manifests = load_manifests(resources)
    build_registry(config, manifests)

    assert len(manifests.issues) == 1
    assert "AGENTWORKS_VM" in manifests.issues[0]
    assert warnings == []
