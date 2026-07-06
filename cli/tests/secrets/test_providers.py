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
from agentworks.secrets import SECRET_PROVIDER_REGISTRY, active_backends, resolve_secrets
from agentworks.secrets.base import SecretBackendDecl, SecretDecl

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


class _TestOnlyProvider:
    """Config-bearing provider: one required str field, one optional int.

    Stateless per the provider contract; every call receives the
    backend's config, and ``batch_get`` records what it saw so tests
    can pin that the backend threaded ITS OWN config through.
    """

    name = "test-only"
    interactive = False

    def __init__(self) -> None:
        self.batch_get_configs: list[dict[str, object]] = []

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

    def would_attempt(self, config: Any, secret: Any, mapping: Any) -> bool:
        return True

    def describe_lookup(self, config: Any, secret: Any, mapping: Any) -> str | None:
        return f"{config['endpoint']}/{secret.name}"

    def batch_get(self, config: Any, wants: list[Any]) -> dict[str, str]:
        self.batch_get_configs.append(dict(config))
        return {
            secret.name: f"{config['endpoint']}::{secret.name}"
            for secret, _mapping in wants
        }


@pytest.fixture
def test_only_provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    provider = _TestOnlyProvider()
    monkeypatch.setitem(SECRET_PROVIDER_REGISTRY, "test-only", provider)  # type: ignore[misc]
    return provider


def test_builtin_providers_registered() -> None:
    assert set(SECRET_PROVIDER_REGISTRY) >= {"env-var", "prompt"}


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
          provider_config:
            prefix: NOPE_
        """,
    )
    with pytest.raises(ConfigError, match="accepts no configuration") as exc:
        load_manifests(tmp_path / "resources")
    assert "res.yaml:2" in str(exc.value)


def test_provider_fields_must_nest_under_provider_config(tmp_path: Path) -> None:
    """The spec outside provider_config is provider-agnostic: a stray
    top-level field errors with a pointer at the nesting rule, before
    any provider is consulted."""
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
    with pytest.raises(
        ConfigError, match="goes under spec.provider_config"
    ):
        load_manifests(tmp_path / "resources")


def test_reserved_builtin_backend_names(tmp_path: Path) -> None:
    """An operator manifest colliding with a bundled built-in backend
    errors at Registry.add via the kind's builtin_override="reserved" --
    the sole enforcement; no publisher-side special case."""
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
    with pytest.raises(ConfigError, match="reserved") as exc:
        build_registry(config)
    assert "differently-named" in str(exc.value)


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
          provider_config:
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
          provider_config:
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
    assert row.provider_config == {"endpoint": "https://example.test", "timeout": 30}

    # End-to-end through the door: the backend threads its validated
    # config into the provider on resolve.
    backends = active_backends(config, registry)
    assert [b.name for b in backends] == ["fake-store", "prompt"]
    values = resolve_secrets([SecretDecl(name="s", description="s")], backends)
    assert values == {"s": "https://example.test::s"}
    assert test_only_provider.batch_get_configs == [
        {"endpoint": "https://example.test", "timeout": 30}
    ]


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
    backends = active_backends(config, registry)
    # Backends present as THEIR OWN names; the provider is a field.
    assert [b.name for b in backends] == ["sibling-env", "prompt"]
    assert [b.provider for b in backends] == ["env-var", "prompt"]


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
          provider_config:
            endpoint: https://a.test
        ---
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: store-b
        spec:
          provider: test-only
          provider_config:
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
    backends = active_backends(config, build_registry(config))
    assert [b.name for b in backends] == ["store-a", "store-b"]
    # Independent configs per backend, one shared provider.
    assert [b.provider_config["endpoint"] for b in backends] == [
        "https://a.test",
        "https://b.test",
    ]
    # Independent mappings per backend name: opt out of store-a only.
    decl = SecretDecl(
        name="s", description="s", backend_mappings={"store-a": False}
    )
    assert backends[0].would_attempt(decl) is False
    assert backends[1].would_attempt(decl) is True
    values = resolve_secrets([decl], backends)
    assert values == {"s": "https://b.test::s"}


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


def test_chain_naming_unknown_backend_errors_at_build_registry(tmp_path: Path) -> None:
    """The chain is config; validate_chain (run by build_registry right
    after finalize) rejects unknown names with the operator's vocabulary
    -- the runtime never sees the invalid chain."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["no-such-backend"]
        """,
    )
    with pytest.raises(
        ConfigError,
        match=r"\[secret_config\].backends names unknown backend 'no-such-backend'",
    ) as exc:
        build_registry(config)
    assert exc.value.hint is not None
    assert "secret-backend manifest" in exc.value.hint


def test_legacy_toml_backend_section_is_warned_noop(tmp_path: Path) -> None:
    """[secret_backends.<kind>] sections are deprecated no-ops: they warn,
    publish nothing, and the bundled built-in row keeps serving the
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


def test_legacy_toml_backend_unknown_kind_still_errors(tmp_path: Path) -> None:
    """Typo protection survives the deprecation: an unknown kind in a
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


def test_backend_references_provider_in_registry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = build_registry(config)
    provider_row = registry.lookup("secret-provider", "env-var")
    sources = {entry.source for entry in provider_row.references}
    assert ("secret-backend", "env-var") in sources
