"""Tests for the ``secret-backend`` descriptor kind (post-collapse).

One read-only row per registered capability, published by the secrets
code publisher; not manifest-declarable. The ``[secret_config]`` chain
names these rows directly.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError

BUILTIN_BACKENDS = ("env-var", "prompt")


def _write_cfg(path: Path, body: str = "") -> Path:
    pub = path.parent / "id.pub"
    priv = path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body),
    )
    return path


def test_kind_attributes() -> None:
    kind = KIND_REGISTRY["secret-backend"]
    assert kind.kind == "secret-backend"
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None
    assert kind.category == "capability"
    assert kind.description
    # The reserved tier stays (defensive default; plugin-SDD consumer)
    # even though its last reachable member died in the collapse.
    assert kind.builtin_override == "reserved"
    assert "secret-provider" not in KIND_REGISTRY  # collapsed 2026-07-07


#: The three members every capability kind is checked for by NAME rather
#: than through its descriptor's per-kind sets: ``_metadata_error`` reads
#: ``name`` and ``description`` (``capabilities/conformance.py:120-125``)
#: and ``_version_error`` reads ``contract_version``
#: (``capabilities/conformance.py:256-257``). They are named here because
#: they are named there; everything else must come off the descriptor.
_UNIVERSALLY_CHECKED = frozenset({"name", "description", "contract_version"})


def test_the_descriptor_declares_every_secret_backend_protocol_member() -> None:
    """``secret-backend`` is the one kind whose contract is a Protocol, so
    ``issubclass`` cannot check it (``conformance.py:96-97`` returns early)
    and ``inspect.isabstract`` has nothing to find. The descriptor's
    ``required_operations`` and ``required_attributes`` ARE the enforcement,
    and only for the members they happen to list: add a member to
    ``SecretBackend`` without adding it there and a backend omitting it
    seats cleanly, then raises ``AttributeError`` in the resolve loop, at
    runtime, on the operator.

    So this asserts the INVARIANT, that the declared sets partition the
    Protocol, rather than pinning today's names. Both sets are derived from
    ``SecretBackend`` itself: a Protocol member that is a function is an
    operation, and everything else is an attribute. The split matters as
    much as the coverage, because the two checks are not equally strong:
    an operation is checked for being CALLABLE
    (``conformance.py:164``) and an attribute only for being PRESENT
    (``conformance.py:138``), so a new method filed under attributes would
    let a backend seat with a non-callable of the same name.
    """
    import inspect

    from agentworks.secrets.backends import SecretBackend
    from agentworks.secrets.kinds import SECRET_BACKEND_DESCRIPTOR as descriptor

    # `typing` computes this when a Protocol class is created; the stubs do
    # not declare it. Read unguarded on purpose: if a future Python stops
    # publishing it, this test has to fail rather than quietly check nothing.
    members = set(SecretBackend.__protocol_attrs__)  # type: ignore[attr-defined]
    operations = {name for name in members if inspect.isfunction(getattr(SecretBackend, name, None))}

    assert members >= _UNIVERSALLY_CHECKED
    assert descriptor.required_operations == operations
    assert descriptor.required_attributes == members - operations - _UNIVERSALLY_CHECKED


def test_synthesize_raises() -> None:
    kind = KIND_REGISTRY["secret-backend"]
    with pytest.raises(NoUnreferencedDefaultError):
        kind.synthesize(())


def test_capability_descriptors_published(tmp_path: Path) -> None:
    """One descriptor row per registered capability, from the secrets
    code publisher (the bundled backend manifests died in the Phase 5.5
    collapse)."""
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    for backend_name in BUILTIN_BACKENDS:
        row = registry.lookup("secret-backend", backend_name)
        assert row.name == backend_name
        assert row.origin.variant == "built-in"
        assert row.origin.source == "agentworks.secrets"


def test_legacy_toml_backend_section_does_not_override_built_in(tmp_path: Path) -> None:
    """``[secret_backends.env-var]`` is a deprecated no-op: it publishes
    nothing (it warns at load), and the descriptor row survives
    untouched."""
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [secret_backends.env-var]
            """,
        ),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    env_var = registry.lookup("secret-backend", "env-var")
    assert env_var.origin.variant == "built-in"
