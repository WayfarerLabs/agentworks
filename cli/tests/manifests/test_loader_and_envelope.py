"""Loader walk semantics and envelope validation (resource-manifests SDD,
Phase 2)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text))


def _secret_doc(name: str, description: str = "d") -> str:
    return f"""
    apiVersion: agentworks/v1
    kind: secret
    metadata:
      name: {name}
      description: {description}
    spec: {{}}
    """


def test_missing_directory_is_empty(tmp_path: Path) -> None:
    manifests = load_manifests(tmp_path / "resources")
    assert manifests.entries == ()
    assert manifests.issues == ()


def test_walk_order_is_lexicographic_and_skips_dotfiles(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(root, "b/later.yaml", _secret_doc("from-b"))
    _write(root, "a.yaml", _secret_doc("from-a"))
    _write(root, "a2.yml", _secret_doc("from-a2"))
    _write(root, ".hidden.yaml", _secret_doc("hidden"))
    _write(root, ".git/config.yaml", _secret_doc("also-hidden"))
    _write(root, "notes.txt", "not a manifest")

    manifests = load_manifests(root)
    assert [e.name for e in manifests.entries] == ["from-a", "from-a2", "from-b"]


def test_multi_document_lines_are_accurate(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "secrets.yaml",
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: one
          description: d
        spec: {}
        ---
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: two
          description: d
        spec: {}
        """,
    )
    manifests = load_manifests(root)
    lines = {e.name: e.location.line for e in manifests.entries}
    assert lines["one"] == 2
    assert lines["two"] == 9


def test_empty_documents_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(root, "a.yaml", "---\n" + _secret_doc("real") + "\n---\n")
    manifests = load_manifests(root)
    assert [e.name for e in manifests.entries] == ["real"]


def test_duplicate_across_files_cites_both_locations(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(root, "a.yaml", _secret_doc("npm-token"))
    _write(root, "b.yaml", _secret_doc("npm-token"))
    with pytest.raises(ConfigError) as exc:
        load_manifests(root)
    message = str(exc.value)
    assert "duplicate secret" in message
    assert "a.yaml" in message
    assert "b.yaml" in message


def test_duplicate_within_one_file_errors(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(root, "a.yaml", _secret_doc("npm-token") + "\n---\n" + _secret_doc("npm-token"))
    with pytest.raises(ConfigError, match="duplicate secret"):
        load_manifests(root)


def test_invalid_yaml_reports_location(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(root, "bad.yaml", "apiVersion: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_manifests(root)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("apiVersion: agentworks/v2", 'apiVersion must be "agentworks/v1"'),
        ("kind: 12", "kind is required"),
        ("extra: true", "unknown manifest key"),
    ],
)
def test_envelope_rejections(tmp_path: Path, mutation: str, match: str) -> None:
    root = tmp_path / "resources"
    base = dedent(
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: s1
          description: d
        spec: {}
        """
    )
    if mutation.startswith("apiVersion"):
        text = base.replace("apiVersion: agentworks/v1", mutation)
    elif mutation.startswith("kind"):
        text = base.replace("kind: secret", mutation)
    else:
        text = base + mutation + "\n"
    _write(root, "a.yaml", text)
    with pytest.raises(ConfigError, match=match):
        load_manifests(root)


def test_unknown_kind_gets_kebab_hint(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "a.yaml",
        _secret_doc("s1").replace("kind: secret", "kind: vm_template"),
    )
    with pytest.raises(ConfigError, match="unknown kind") as exc:
        load_manifests(root)
    assert exc.value.hint is not None
    assert "vm-template" in exc.value.hint


def test_secret_backend_not_declarable_yet(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "a.yaml",
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: my-backend
        spec: {}
        """,
    )
    with pytest.raises(ConfigError, match="not manifest-declarable yet"):
        load_manifests(root)


def test_descriptor_kind_rejected(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "a.yaml",
        """
        apiVersion: agentworks/v1
        kind: git-credential-provider
        metadata:
          name: github
        spec: {}
        """,
    )
    with pytest.raises(ConfigError, match="provided by the app"):
        load_manifests(root)


def test_singleton_kind_rejects_non_default_name(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "a.yaml",
        """
        apiVersion: agentworks/v1
        kind: admin-template
        metadata:
          name: custom
        spec: {}
        """,
    )
    with pytest.raises(ConfigError, match='accepts only metadata.name "default"'):
        load_manifests(root)


def test_unknown_metadata_key_rejected(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "a.yaml",
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: s1
          description: d
          labels: {a: b}
        spec: {}
        """,
    )
    with pytest.raises(ConfigError, match="unknown metadata key"):
        load_manifests(root)


def test_missing_spec_key_rejected(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "a.yaml",
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: s1
          description: d
        """,
    )
    with pytest.raises(ConfigError, match="spec is required"):
        load_manifests(root)


def test_duplicate_mapping_key_rejected(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "a.yaml",
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: s1
          description: first
          description: second
        spec: {}
        """,
    )
    with pytest.raises(ConfigError, match="duplicate mapping key"):
        load_manifests(root)


def test_nested_dir_order_is_component_wise(tmp_path: Path) -> None:
    """Ordering is per path component: ``a/`` sorts before ``a-b/`` even
    though the raw relative-path strings compare the other way."""
    root = tmp_path / "resources"
    _write(root, "a/x.yaml", _secret_doc("from-a-dir"))
    _write(root, "a-b/x.yaml", _secret_doc("from-a-dash-b"))
    manifests = load_manifests(root)
    assert [e.name for e in manifests.entries] == ["from-a-dir", "from-a-dash-b"]


def test_spec_unknown_key_warns_with_location_for_warn_kinds(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    _write(
        root,
        "a.yaml",
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: s1
          description: d
        spec:
          bogus: 1
        """,
    )
    manifests = load_manifests(root)
    assert len(manifests.issues) == 1
    assert "a.yaml:2" in manifests.issues[0]
    assert "bogus" in manifests.issues[0]
