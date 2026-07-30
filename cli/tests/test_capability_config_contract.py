"""The capability config contract.

Capabilities are invoked during interpretation of the consuming
resource: ``dependencies`` extracts the resource references its config
block implies (``ConfigReference``, sourceless; the consuming resource
emits them with itself as the source) and ``validate`` is the throwing
shape check. Two shipped hosts exercise it: the git-credential
``provider_config`` blob and per-secret ``backend_mappings`` values.
(The API notes it may be superseded by registration-time schema
declarations.)
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY
from agentworks.capabilities.git_credential.base import GitCredentialProvider
from agentworks.config import load_config
from agentworks.errors import ConfigError
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
    """A malformed provider_config fails at build_registry now, not at
    load: capability validation moved out of the TOML loader into the
    finalize ``validate`` pass (R3). The source location is re-attached
    from the resource origin, so the TOML path gains a file:line it never
    framed before."""
    config = _config(
        tmp_path,
        """
        [git_credentials.ado]
        provider = "azdo"
        """,
    )
    with pytest.raises(ConfigError, match="org is required for the azdo provider") as exc:
        build_registry(config)
    assert "config.toml" in str(exc.value)


def test_azdo_rejects_unknown_blob_fields_yaml(tmp_path: Path) -> None:
    """Stray blob fields on the TRUE blob fail at build_registry: the
    capability validate moved to the finalize pass (R3), and the error
    keeps the manifest file:line (now re-attached from the resource
    origin, not the decode prefix)."""
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
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="unknown azdo provider field") as exc:
        build_registry(config)
    assert "res.yaml" in str(exc.value)


def test_base_class_accepts_no_configuration() -> None:
    """The base-class default: capabilities without config reject any
    blob content. (github grew scope fields in #166, so the pin uses a
    minimal subclass.)"""

    class _Bare(GitCredentialProvider):
        provider_name = "bare"

        def credential_lines(self, token: str) -> list[str]:
            return []

    with pytest.raises(ConfigError, match="accepts no configuration"):
        _Bare.validate("spec.provider_config", {"anything": 1})


def test_github_rejects_unknown_blob_fields(tmp_path: Path) -> None:
    """github validates its own vocabulary (scope fields only), now at
    build_registry (the finalize ``validate`` pass, R3)."""
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
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="unknown github provider field"):
        build_registry(config)


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


# -- The finalize validate pass: timing + ordering (R3, R9.3) ----------------


def test_cycle_reported_before_malformed_block(tmp_path: Path) -> None:
    """R9.3: capability-block validation moved out of decode/load into
    the finalize ``validate`` pass, which runs AFTER cycle detection. So
    a config carrying BOTH a malformed block and a cycle now reports the
    cycle first, where the malformed block used to fail earlier at
    decode/load, before finalize ever ran."""
    config = _config(
        tmp_path,
        """
        [session_templates.a]
        inherits = ["b"]
        harness = "shell"
        [session_templates.a.harness_config]
        nope = "x"

        [session_templates.b]
        inherits = ["a"]
        """,
    )
    with pytest.raises(ConfigError, match="cycle detected") as exc:
        build_registry(config)
    # The malformed shell block is deferred behind the cycle, not raised.
    assert "unknown shell harness field" not in str(exc.value)


def test_construct_time_validation_survives_the_move(tmp_path: Path) -> None:
    """R3 invariant: moving validation into the finalize pass does not
    relax the construct-time check. Constructing a capability directly
    still re-runs ``validate`` and rejects a malformed blob (a provider
    that reasons "validate ran at construct, so ``org`` is a valid str"
    still holds)."""
    from agentworks.capabilities.git_credential.azdo import AzDOCredentialProvider

    with pytest.raises(ConfigError, match="org is required for the azdo provider"):
        AzDOCredentialProvider("ado", {})


# -- The dependencies half ---------------------------------------------------


class _SigningCredentialProvider(GitCredentialProvider):
    """Test-only capability whose config names a secret: exercises the
    dependencies-extraction contract end to end."""

    provider_name = "test-signing"

    @classmethod
    def dependencies(cls, owner: str, config: Any) -> tuple[ConfigReference, ...]:
        key = config.get("signing_key", "code-signing-key")
        if not isinstance(key, str) or not key:
            return ()
        return (ConfigReference(kind="secret", name=key, usage="the signing key"),)

    @classmethod
    def validate(cls, owner: str, config: Any) -> None:
        unknown = sorted(set(config) - {"signing_key"})
        if unknown:
            raise ConfigError(f"{owner}: unknown field(s): {', '.join(unknown)}")
        key = config.get("signing_key", "code-signing-key")
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{owner}.signing_key must be a secret name")

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://signer:{token}@example.test"]

    def helper_entry(self):  # noqa: ANN201
        from agentworks.capabilities.git_credential.base import HelperEntry

        return HelperEntry(host="example.test", username="signer")


@pytest.fixture
def signing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(GIT_CREDENTIAL_PROVIDER_REGISTRY, "test-signing", _SigningCredentialProvider)


def test_capability_refs_attributed_to_consuming_resource(tmp_path: Path, signing_provider: None) -> None:
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
    sources = {entry.source for entry in registry.graph.dependents_of("secret", "code-signing-key")}
    assert ("git-credential", "signer") in sources


def test_capability_ref_default_is_operator_overridable(tmp_path: Path, signing_provider: None) -> None:
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
    registry.lookup("secret", "corp-signing-key")
    sources = {entry.source for entry in registry.graph.dependents_of("secret", "corp-signing-key")}
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


def test_declared_mapping_for_non_opted_in_backend_is_validated_at_build(tmp_path: Path) -> None:
    """R9.9: a declared mapping addressed to a PRESENT backend is validated at
    build even when that backend is not in the active chain (the secret's own
    ``validate``, run by the finalize pass, checks every present backend's
    mapping, not just the opted-in ones). Here env-var is not opted in (chain
    is prompt-only) but its structured mapping is malformed for env-var, so the
    build now fails, where the old ``validate_chain`` left it dormant."""
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
    with pytest.raises(ConfigError, match="env-var backend must be a non-empty string"):
        build_registry(config)


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
