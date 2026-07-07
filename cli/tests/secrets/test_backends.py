"""The secret-backend capability layer (post-collapse, ADR 0016).

Backends are code capabilities in ``SECRET_BACKEND_REGISTRY``, mirrored
into the resource Registry as read-only ``secret-backend`` descriptor
rows -- one per capability, no declarable instantiation layer. The
chain (``[secret_config].backends``) and per-secret ``backend_mappings``
name backends directly.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.secrets import SECRET_BACKEND_REGISTRY, active_backends, resolve_secrets
from agentworks.secrets.base import SecretDecl


def _config(tmp_path: Path, body: str = "") -> Any:
    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + dedent(body)
    )
    return load_config(cfg, warn_issues=False)


def _manifest(tmp_path: Path, text: str, rel: str = "res.yaml") -> None:
    resources = tmp_path / "resources"
    resources.mkdir(exist_ok=True)
    (resources / rel).write_text(dedent(text))


class _TestOnlyBackend:
    """A store-flavored capability registered only in tests: exercises
    the SecretBackend API end to end (structured mappings, soft-skip
    for unmapped secrets) without shipping artificial built-ins."""

    name = "test-only"
    description = "test-only store"
    interactive = False

    def __init__(self) -> None:
        self.batch_get_calls: list[list[str]] = []

    def would_attempt(self, secret: Any, mapping: Any) -> bool:
        # Store semantics: only attempts explicitly-mapped secrets
        # (soft-skip otherwise), unlike the always-attempt built-ins.
        return mapping is not None

    def describe_lookup(self, secret: Any, mapping: Any) -> str | None:
        if isinstance(mapping, dict):
            return f"store://{mapping.get('vault')}/{mapping.get('item')}"
        return str(mapping) if mapping is not None else None

    def batch_get(self, wants: list[tuple[Any, Any]]) -> dict[str, str]:
        self.batch_get_calls.append([s.name for s, _ in wants])
        return {
            s.name: f"value-of-{s.name}" for s, m in wants if m is not None
        }


@pytest.fixture
def test_only_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = _TestOnlyBackend()
    monkeypatch.setitem(SECRET_BACKEND_REGISTRY, "test-only", backend)  # type: ignore[misc]
    return backend


def test_builtin_backends_registered() -> None:
    assert set(SECRET_BACKEND_REGISTRY) >= {"env-var", "prompt"}


def test_one_descriptor_row_per_capability(tmp_path: Path) -> None:
    """The collapse's registry shape: kind secret-backend holds exactly
    the capability descriptors (no declarable rows, no secret-provider
    kind at all)."""
    config = _config(tmp_path)
    registry = build_registry(config)
    names = sorted(e.name for e in registry.iter_kind("secret-backend"))
    assert names == sorted(SECRET_BACKEND_REGISTRY)
    row = registry.lookup("secret-backend", "env-var")
    assert row.origin.variant == "built-in"
    assert row.description  # capability-supplied, for inspection surfaces
    with pytest.raises(KeyError):
        registry.lookup("secret-provider", "env-var")


def test_secret_backend_is_not_declarable(tmp_path: Path) -> None:
    """A kind: secret-backend manifest gets the permanent R3
    capability-kind envelope error."""
    from agentworks.manifests import load_manifests

    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: my-env
        spec:
          provider: env-var
        """,
    )
    with pytest.raises(ConfigError, match="provided by the app") as exc:
        load_manifests(tmp_path / "resources")
    assert "res.yaml" in str(exc.value)


def test_chain_resolves_against_descriptor_rows(
    tmp_path: Path, test_only_backend: Any
) -> None:
    """[secret_config].backends names capabilities; the runtime chain
    wraps them with the loop-side orchestration."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["test-only", "prompt"]
        """,
    )
    registry = build_registry(config)
    backends = active_backends(config, registry)
    assert [b.name for b in backends] == ["test-only", "prompt"]
    assert [b.interactive for b in backends] == [False, True]


def test_structured_mapping_reaches_the_backend(
    tmp_path: Path, test_only_backend: Any
) -> None:
    """Per-secret store addressing lives in backend_mappings (the
    collapse's answer to the 1Password case): the structured dict rides
    through would_attempt / describe_lookup / batch_get."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["test-only"]
        """,
    )
    registry = build_registry(config)
    (backend,) = active_backends(config, registry)
    mapped = SecretDecl(
        name="s1",
        description="s1",
        backend_mappings={"test-only": {"vault": "Work", "item": "npm"}},
    )
    unmapped = SecretDecl(name="s2", description="s2")
    assert backend.would_attempt(mapped)
    assert not backend.would_attempt(unmapped)  # store soft-skip
    assert backend.describe_lookup(mapped) == "store://Work/npm"
    values = resolve_secrets([mapped], [backend])
    assert values == {"s1": "value-of-s1"}


def test_opt_out_never_reaches_the_capability(
    tmp_path: Path, test_only_backend: Any
) -> None:
    """The generic `false` opt-out is loop-side orchestration: an
    opted-out secret is excluded before the capability sees the
    batch."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["test-only"]
        """,
    )
    registry = build_registry(config)
    (backend,) = active_backends(config, registry)
    opted_out = SecretDecl(
        name="s1", description="s1", backend_mappings={"test-only": False}
    )
    assert not backend.would_attempt(opted_out)
    assert backend.describe_lookup(opted_out) is None
    assert backend.resolve([opted_out]) == {}
    assert test_only_backend.batch_get_calls == []


def test_chain_naming_unknown_backend_errors_at_build_registry(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["nope", "prompt"]
        """,
    )
    with pytest.raises(ConfigError, match="unknown backend 'nope'") as exc:
        build_registry(config)
    assert exc.value.hint is not None
    assert "registered backends" in exc.value.hint


def test_legacy_toml_backend_section_is_warned_noop(tmp_path: Path) -> None:
    """[secret_backends.<name>] sections are deprecated no-ops: they
    warn, publish nothing, and the descriptor row keeps serving the
    chain."""
    config = _config(
        tmp_path,
        """
        [secret_backends.env-var]

        [secret_config]
        backends = ["env-var"]
        """,
    )
    assert any(
        "[secret_backends.env-var] is deprecated" in issue
        for issue in config.deprecation_issues
    )
    registry = build_registry(config)
    row = registry.lookup("secret-backend", "env-var")
    assert row.origin.variant == "built-in"
    backends = active_backends(config, registry)
    assert [b.name for b in backends] == ["env-var"]


def test_legacy_toml_backend_unknown_name_still_errors(tmp_path: Path) -> None:
    """Typo protection survives the deprecation: an unknown name in a
    legacy section is a hard ConfigError, not a silent no-op."""
    with pytest.raises(ConfigError, match="unknown secret provider"):
        _config(
            tmp_path,
            """
            [secret_backends.envvar]
            """,
        )


def test_build_registry_is_pure(tmp_path: Path) -> None:
    """No memo: every call builds fresh (a command's composition root
    calls it once and threads the result)."""
    config = _config(tmp_path)
    first = build_registry(config)
    second = build_registry(config)
    assert first is not second
