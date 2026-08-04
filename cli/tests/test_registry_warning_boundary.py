"""Guard the request-warning boundary around registry composition roots."""

from __future__ import annotations

import ast
from pathlib import Path


# These direct uses deliberately stay pure. Every request-serving caller must
# use ``load_request_registry`` so the old-selector warning is rendered once
# at the request boundary instead of leaking from ``build_registry``.
_PURE_BUILD_REGISTRY_CALLERS = {
    "cli/commands/resource.py": "resource migrate is the warning remediation",
    "doctor.py": "doctor passes an explicit ManifestSet to render health rows",
    "migrate/execute.py": "post-migration equivalence verification",
}


def test_only_intentional_pure_callers_import_build_registry() -> None:
    source_root = Path(__file__).parents[1] / "agentworks"
    callers: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "agentworks.bootstrap"
            and any(alias.name == "build_registry" for alias in node.names)
            for node in ast.walk(tree)
        ):
            callers.add(path.relative_to(source_root).as_posix())

    assert callers == set(_PURE_BUILD_REGISTRY_CALLERS), (
        "request-serving registry roots must use load_request_registry; "
        f"unexpected pure callers: {sorted(callers - set(_PURE_BUILD_REGISTRY_CALLERS))}"
    )


def test_build_registry_with_explicit_manifest_set_does_not_render_warnings(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Doctor and verification can safely compose an explicit manifest set."""
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.manifests import load_manifests

    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA")
    priv.write_text("private")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[operator]\nssh_public_key = \"{pub}\"\nssh_private_key = \"{priv}\"\n"
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "old.yaml").write_text(
        """apiVersion: agentworks/v1
kind: session-template
metadata:
  name: shell-old
spec:
  harness: shell
"""
    )
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)

    config = load_config(config_path, warn_issues=False)
    manifests = load_manifests(resources)
    build_registry(config, manifests)

    assert manifests.deprecated_harness_selectors == ("session-template/shell-old",)
    assert warnings == []
