"""The capability config-validation contract.

Capabilities are invoked during validation of the consuming resource:
they validate their own config block and return the resource references
it implies (``ConfigReference``, sourceless); the consuming resource
emits those references with itself as the source. Two shipped hosts
exercise it: the git-credential ``provider_config`` blob and per-secret
``backend_mappings`` values. (The API notes it may be superseded by
registration-time schema declarations.)
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.git_credentials import GIT_CREDENTIAL_PROVIDER_REGISTRY
from agentworks.git_credentials.base import GitCredentialProvider
from agentworks.manifests import load_manifests
from agentworks.resources.reference import ConfigReference


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


def _manifest(tmp_path: Path, text: str) -> None:
    resources = tmp_path / "resources"
    resources.mkdir(exist_ok=True)
    (resources / "res.yaml").write_text(dedent(text))


# -- Blob validation through the capability ---------------------------------


def test_azdo_org_required_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="org is required for the azdo provider"):
        _config(
            tmp_path,
            """
            [git_credentials.ado]
            provider = "azdo"
            """,
        )


def test_azdo_rejects_unknown_blob_fields_yaml(tmp_path: Path) -> None:
    """The decoder invokes the capability on the TRUE blob, so stray
    blob fields error with file:line (the loader flatten would silently
    drop them)."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: ado
        spec:
          provider: azdo
          provider_config:
            org: my-org
            bogus: 1
        """,
    )
    with pytest.raises(ConfigError, match="unknown azdo provider field") as exc:
        load_manifests(tmp_path / "resources")
    assert "res.yaml" in str(exc.value)


def test_github_accepts_no_configuration(tmp_path: Path) -> None:
    """The base-class default: capabilities without config reject any
    blob content."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: gh
        spec:
          provider: github
          provider_config:
            org: nope
        """,
    )
    with pytest.raises(ConfigError, match="accepts no configuration"):
        load_manifests(tmp_path / "resources")


def test_unknown_provider_defers_to_miss_policy(tmp_path: Path) -> None:
    """An unregistered provider name skips capability validation; the
    framework's miss policy reports it uniformly at build_registry."""
    config = _config(
        tmp_path,
        """
        [git_credentials.mystery]
        provider = "sourcehut"
        """,
    )
    with pytest.raises(ConfigError, match="sourcehut"):
        build_registry(config)


# -- The reference-returning half --------------------------------------------


class _SigningCredentialProvider(GitCredentialProvider):
    """Test-only capability whose config names a secret: exercises the
    validate-and-return-references contract end to end."""

    provider_name = "test-signing"

    @classmethod
    def validate_config(
        cls, owner: str, config: Any
    ) -> tuple[ConfigReference, ...]:
        unknown = sorted(set(config) - {"signing_key"})
        if unknown:
            raise ConfigError(f"{owner}: unknown field(s): {', '.join(unknown)}")
        key = config.get("signing_key", "code-signing-key")
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{owner}.signing_key must be a secret name")
        return (
            ConfigReference(kind="secret", name=key, usage="the signing key"),
        )

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://signer:{token}@example.test"]


@pytest.fixture
def signing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        GIT_CREDENTIAL_PROVIDER_REGISTRY, "test-signing", _SigningCredentialProvider
    )


def test_capability_refs_attributed_to_consuming_resource(
    tmp_path: Path, signing_provider: None
) -> None:
    """The full contract: the capability returns the reference its blob
    implies; the consuming resource emits it as source; the framework
    auto-declares the secret with a per-consumer description."""
    config = _config(
        tmp_path,
        """
        [git_credentials.signer]
        provider = "test-signing"
        """,
    )
    registry = build_registry(config)
    # Defaulted secret name: auto-declared, attributed to THIS credential.
    decl = registry.lookup("secret", "code-signing-key")
    assert decl.origin.variant == "auto-declared"
    assert "the signing key for git-credential/signer" in decl.description
    sources = {entry.source for entry in decl.references}
    assert ("git-credential", "signer") in sources


def test_capability_ref_default_is_operator_overridable(
    tmp_path: Path, signing_provider: None
) -> None:
    """The defaulted-and-overridable flavor: pointing the blob field at
    another secret moves the reference (TOML hosts blob fields flat)."""
    config = _config(
        tmp_path,
        """
        [git_credentials.signer]
        provider = "test-signing"

        [secrets.corp-signing-key]
        description = "Corporate signing key"
        """,
    )
    # TOML flat domain has no blob columns beyond org today; drive the
    # override through a manifest instead.
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: signer2
        spec:
          provider: test-signing
          provider_config:
            signing_key: corp-signing-key
        """,
    )
    registry = build_registry(config)
    decl = registry.lookup("secret", "corp-signing-key")
    sources = {entry.source for entry in decl.references}
    assert ("git-credential", "signer2") in sources


# -- Mapping values: capability config in the per-secret host ----------------


def test_env_var_mapping_validated_at_build_registry(tmp_path: Path) -> None:
    """A structured mapping for env-var used to explode lazily at
    describe/resolve time; validate_chain now invokes the backend's
    validate_mapping so it fails at build_registry with config
    vocabulary."""
    config = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        backend_mappings.env-var = { vault = "Work" }
        """,
    )
    with pytest.raises(ConfigError, match="env-var backend must be a non-empty string"):
        build_registry(config)


def test_prompt_rejects_any_mapping(tmp_path: Path) -> None:
    """Prompt has no mapping vocabulary: any non-false value is dead
    config (a typo for another backend) and errors at build_registry.
    The generic false opt-out is loop-owned and never reaches the
    capability."""
    config = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        backend_mappings.prompt = "ignored"
        """,
    )
    with pytest.raises(ConfigError, match="prompt backend has no meaning"):
        build_registry(config)


def test_prompt_false_opt_out_still_loads(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        backend_mappings.env-var = "NPM_TOKEN"
        backend_mappings.prompt = false
        """,
    )
    build_registry(config)  # no error


def test_dormant_backend_mappings_not_validated(tmp_path: Path) -> None:
    """Mappings addressed to backends outside the active chain stay
    dormant and unvalidated, exactly as they stay unused."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["prompt"]

        [secrets.npm-token]
        description = "npm token"
        backend_mappings.env-var = { vault = "Work" }
        """,
    )
    build_registry(config)  # env-var not in chain -> not validated


def test_prompt_rejects_structured_mapping_too(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        backend_mappings.prompt = { vault = "Work" }
        """,
    )
    with pytest.raises(ConfigError, match="prompt backend has no meaning"):
        build_registry(config)


def test_github_toml_stray_org_keeps_loading(tmp_path: Path) -> None:
    """Loads-today: the flat TOML shape only ever read `org` for azdo,
    so a released github credential carrying a stray `org` key loaded
    with the key silently ignored. The capability validates the blob
    the loader assembles, and the loader must therefore hoist `org`
    into the blob only for azdo -- a stray key must not be promoted
    into a validation error on released surface."""
    config = _config(
        tmp_path,
        """
        [git_credentials.hub]
        provider = "github"
        org = "accidental"
        """,
    )
    registry = build_registry(config)
    cred = registry.lookup("git-credential", "hub")
    assert cred.provider_config == {}  # stray key stays ignored, as released
