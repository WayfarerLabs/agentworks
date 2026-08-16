"""Contract tests for ``agentworks.resources.access``.

The accessor layer's miss semantics are load-bearing: ``git_credential``
must return None on a miss (callers raise their own typed errors for
operator-typed names), while the singleton accessors rely on the
always-materialize guarantee. The shared identity seam adds syntax checking
and typed registry resolution without changing those concrete row accessors.
A regression in the older accessors surfaced as dead ``if cred is None``
guards and raw ``KeyError`` tracebacks reaching operators.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import NotFoundError, ValidationError
from agentworks.resources.access import (
    ResourceIdentity,
    admin_template,
    git_credential,
    kind_dict,
    named_console_template,
    parse_resource_identity,
    resolve_resource,
    secret_decls,
)
from tests.conftest import ManifestDoc, write_manifests


def _registry(tmp_path: Path, *manifests: ManifestDoc):  # noqa: ANN202 - test helper
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            """
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"
            """
        ).format(pub=tmp_path / "k.pub", priv=tmp_path / "k")
    )
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test")
    (tmp_path / "k").write_text("key")
    if manifests:
        write_manifests(cfg.parent, *manifests)
    return build_registry(load_config(cfg, warn_issues=False))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("session/legacy--name", ResourceIdentity("session", "legacy--name")),
        ("secret/name.with.dots", ResourceIdentity("secret", "name.with.dots")),
        ("secret/scope:name", ResourceIdentity("secret", "scope:name")),
        ("secret/name/with/slash", ResourceIdentity("secret", "name/with/slash")),
    ],
)
def test_parse_resource_identity_splits_only_on_first_slash(value: str, expected: ResourceIdentity) -> None:
    assert parse_resource_identity(value) == expected


@pytest.mark.parametrize("value", ["secret", "/name", "secret/"])
def test_parse_resource_identity_rejects_missing_or_empty_parts(value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        parse_resource_identity(value)
    assert exc.value.entity_kind == "resource"


def test_resource_identity_is_frozen_and_slotted() -> None:
    identity = ResourceIdentity("secret", "token")
    assert not hasattr(identity, "__dict__")
    with pytest.raises(FrozenInstanceError):
        identity.name = "changed"  # type: ignore[misc]


def test_resolve_resource_returns_exact_identity_row_and_origin(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ManifestDoc("vm-template", "legacy--name"))
    identity = parse_resource_identity("vm-template/legacy--name")

    resolved = resolve_resource(registry, identity)
    resource = registry.lookup("vm-template", "legacy--name")

    assert resolved.identity is identity
    assert resolved.origin is resource.origin
    assert resolved.resource is resource
    assert resolved.origin is not None
    assert resolved.origin.variant == "operator-declared"
    assert not hasattr(resolved, "__dict__")
    with pytest.raises(FrozenInstanceError):
        resolved.resource = object()  # type: ignore[misc]


def test_resolve_resource_rejects_unknown_kind_before_name_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    monkeypatch.setattr(registry, "lookup", lambda kind, name: pytest.fail("row lookup attempted"))

    with pytest.raises(NotFoundError) as exc:
        resolve_resource(registry, ResourceIdentity("not-a-kind", "anything"))

    assert exc.value.entity_kind == "resource-kind"
    assert exc.value.entity_name == "not-a-kind"


def test_resolve_resource_rejects_missing_name_under_known_kind(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(NotFoundError) as exc:
        resolve_resource(registry, ResourceIdentity("secret", "missing"))

    assert exc.value.entity_kind == "secret"
    assert exc.value.entity_name == "missing"


def test_git_credential_miss_returns_none(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert git_credential(registry, "does-not-exist") is None


def test_git_credential_hit_returns_entry(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        ManifestDoc("git-credential", "github", {"provider": {"name": "github"}}, description="gh"),
    )
    cred = git_credential(registry, "github")
    assert cred is not None
    assert cred.provider.name == "github"


def test_singleton_accessors_always_present(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert admin_template(registry) is not None
    assert named_console_template(registry) is not None


def test_kind_dict_unknown_kind_is_empty(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert kind_dict(registry, "no-such-kind") == {}


def test_secret_decls_includes_auto_declared(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    # The always-materialized vm-template default references the
    # Tailscale auth key, so at least that auto-declared row exists.
    assert "tailscale-auth-key" in secret_decls(registry)
