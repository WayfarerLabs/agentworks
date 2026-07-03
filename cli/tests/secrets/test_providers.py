"""Provider/backend split (resource-manifests SDD, Phase 3).

The test-only provider here is the deliberate exerciser of the
provider-config plumbing (schema validation, defaults, error framing,
config reaching instantiate): the built-in providers accept no
configuration, so without it the contract would ship untested.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests
from agentworks.secrets import PROVIDER_REGISTRY, resolver_for
from agentworks.secrets.base import SecretBackendDecl

_BASE_TOML = """
[operator]
ssh_public_key = "{pub}"
ssh_private_key = "{priv}"
"""


def _config(tmp_path: Path, body: str = "") -> Any:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        _BASE_TOML.format(pub=tmp_path / "k.pub", priv=tmp_path / "k")
        + dedent(body)
    )
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test")
    (tmp_path / "k").write_text("key")
    return load_config(cfg, warn_issues=False)


def _manifest(tmp_path: Path, text: str, rel: str = "res.yaml") -> None:
    path = tmp_path / "resources" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text))


class _FakeSource:
    """Minimal SecretSource capturing the config it was built from."""

    kind = "fake"

    def __init__(self, backend_name: str, config: dict[str, object]) -> None:
        self.backend_name = backend_name
        self.config = config

    def would_attempt(self, secret: Any) -> bool:
        return True

    def get(self, secret: Any) -> str | None:
        return f"fake-{secret.name}"

    def batch_get(self, secrets: list[Any]) -> dict[str, str]:
        return {s.name: f"fake-{s.name}" for s in secrets}

    def describe_lookup(self, secret: Any) -> str | None:
        return f"fake://{secret.name}"


class _TestOnlyProvider:
    """Config-bearing provider: one required str field, one optional int."""

    name = "test-only"

    def validate_config(
        self, backend_name: str, config: dict[str, object]
    ) -> dict[str, object]:
        unknown = set(config) - {"endpoint", "timeout"}
        if unknown:
            raise ConfigError(
                f'secret-backend "{backend_name}": unknown test-only '
                f"provider field(s) {sorted(unknown)}"
            )
        endpoint = config.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ConfigError(
                f'secret-backend "{backend_name}": endpoint is required'
            )
        timeout = config.get("timeout", 30)
        if not isinstance(timeout, int):
            raise ConfigError(
                f'secret-backend "{backend_name}": timeout must be an int'
            )
        return {"endpoint": endpoint, "timeout": timeout}

    def instantiate(
        self, backend_name: str, config: dict[str, object]
    ) -> _FakeSource:
        return _FakeSource(backend_name, dict(self.validate_config(backend_name, config)))


@pytest.fixture
def test_only_provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    provider = _TestOnlyProvider()
    monkeypatch.setitem(PROVIDER_REGISTRY, "test-only", provider)  # type: ignore[misc]
    return provider


def test_builtin_providers_registered() -> None:
    assert set(PROVIDER_REGISTRY) >= {"env-var", "prompt"}


def test_builtin_provider_rejects_config(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: my-env
        spec:
          provider: env-var
          prefix: NOPE_
        """,
    )
    with pytest.raises(ConfigError, match="accepts no configuration") as exc:
        load_manifests(tmp_path / "resources")
    assert "res.yaml:2" in str(exc.value)


def test_reserved_builtin_backend_names(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: env-var
        spec:
          provider: env-var
        """,
    )
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="reserved name") as exc:
        build_registry(config)
    assert exc.value.hint is not None
    assert "differently-named" in exc.value.hint


def test_config_validation_at_decode(
    tmp_path: Path, test_only_provider: Any
) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: broken
        spec:
          provider: test-only
          bogus: 1
        """,
    )
    with pytest.raises(ConfigError, match="unknown test-only provider field") as exc:
        load_manifests(tmp_path / "resources")
    assert "res.yaml:2" in str(exc.value)


def test_config_defaults_and_instantiation(
    tmp_path: Path, test_only_provider: Any
) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: fake-store
          description: test-only backend
        spec:
          provider: test-only
          endpoint: https://example.test
        """,
    )
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["fake-store", "prompt"]
        """,
    )
    registry = build_registry(config)
    row = registry.lookup("secret-backend", "fake-store")
    assert isinstance(row, SecretBackendDecl)
    assert row.config == {"endpoint": "https://example.test", "timeout": 30}

    resolver = resolver_for(registry)
    source = resolver.sources[0]
    assert isinstance(source, _FakeSource)
    assert source.backend_name == "fake-store"
    assert source.config["endpoint"] == "https://example.test"


def test_custom_backend_with_builtin_provider_in_chain(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: sibling-env
          description: a second env-var backend
        spec:
          provider: env-var
        """,
    )
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["sibling-env", "prompt"]
        """,
    )
    registry = build_registry(config)
    resolver = resolver_for(registry)
    assert [s.kind for s in resolver.sources] == ["env-var", "prompt"]


def test_multiple_backends_share_a_provider(
    tmp_path: Path, test_only_provider: Any
) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: store-a
        spec:
          provider: test-only
          endpoint: https://a.test
        ---
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: store-b
        spec:
          provider: test-only
          endpoint: https://b.test
        """,
    )
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["store-a", "store-b"]
        """,
    )
    resolver = resolver_for(build_registry(config))
    endpoints = [s.config["endpoint"] for s in resolver.sources]  # type: ignore[attr-defined]
    assert endpoints == ["https://a.test", "https://b.test"]


def test_unknown_provider_fails_via_framework_miss_policy(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: mystery
        spec:
          provider: nonexistent
        """,
    )
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="unknown secret-provider"):
        build_registry(config)


def test_chain_naming_unknown_backend_errors_at_finalize(tmp_path: Path) -> None:
    """The chain is reference edges on the secret-config row, so an
    unknown name hits the secret-backend kind's error miss policy at
    build_registry -- the runtime never sees the invalid graph."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["no-such-backend"]
        """,
    )
    with pytest.raises(
        ConfigError, match="unknown secret-backend 'no-such-backend'"
    ):
        build_registry(config)


def test_legacy_toml_backend_rows_still_resolve(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [secret_backends.env-var]

        [secret_config]
        backends = ["env-var"]
        """,
    )
    registry = build_registry(config)
    row = registry.lookup("secret-backend", "env-var")
    assert row.origin.variant == "operator-declared"
    resolver = resolver_for(registry)
    assert [s.kind for s in resolver.sources] == ["env-var"]


def test_standard_registry_is_per_config_singleton(tmp_path: Path) -> None:
    """build_registry memoizes the standard path per Config object;
    explicit-manifests calls always build fresh."""
    from agentworks.manifests import ManifestSet

    config = _config(tmp_path)
    first = build_registry(config)
    second = build_registry(config)
    assert first is second
    fresh = build_registry(config, ManifestSet.empty())
    assert fresh is not first


def test_prompt_once_identity(tmp_path: Path) -> None:
    """Resolver identity follows registry identity: the standard
    registry is a per-config singleton, so every default-path caller
    shares one resolver (the prompt-once cache)."""
    config = _config(tmp_path)
    registry = build_registry(config)
    first = resolver_for(registry)
    second = resolver_for(build_registry(config))
    assert first is second


def test_backend_references_provider_in_registry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = build_registry(config)
    provider_row = registry.lookup("secret-provider", "env-var")
    sources = {entry.source for entry in provider_row.references}
    assert ("secret-backend", "env-var") in sources
