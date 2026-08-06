"""The capability config contract: declare a model, receive an instance.

A capability DECLARES the shape of its config as a model and the core
does the rest. Validation is ``model_validate`` against that model,
reference extraction is a structural walk of its marked fields, and no
capability code runs for either: that is the whole point of the flip, and
it is what keeps a misbehaving plugin out of the finalize pass. Two
shipped hosts exercise it here: the git-credential ``provider_config``
blob and per-secret ``backend_mappings`` values.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Annotated, Any, Literal

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY
from agentworks.capabilities.git_credential.base import GitCredentialProvider, HelperEntry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, NonEmptyStr, SecretRef
from tests.conftest import ManifestDoc, write_manifests


def _config(tmp_path: Path, settings: str = "", *, enabled: bool = False) -> Any:
    """Write a settings-only config.toml and return the loaded Config.

    ``azdo`` ships in the opt-in ``azure`` system plugin, whose capability
    validation is deferred while disabled; a test exercising the azdo
    ``validate`` pass must enable the plugin (``enabled=True``) so validation
    fires. The resources under test are declared through :func:`_manifest`;
    config.toml is settings only now (ADR 0022).
    """
    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    plugins = '[plugins]\nsystem = ["azure"]\n\n' if enabled else ""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + plugins
        + dedent(settings)
    )
    return load_config(cfg, warn_issues=False)


def _manifest(tmp_path: Path, *docs: ManifestDoc | str) -> None:
    """Write the resource manifests under test into ``resources/res.yaml``.

    Accepts structured :class:`ManifestDoc` documents or raw YAML strings (the
    file's yaml-sibling tests hand-author the envelope). The fixed ``res.yaml``
    filename is what the file:line assertions match on.
    """
    write_manifests(tmp_path, *docs, filename="res.yaml")


# -- Blob validation through the capability ---------------------------------


def test_azdo_org_required(tmp_path: Path) -> None:
    """A malformed provider_config (azdo with no ``org``) fails at
    build_registry: capability validation runs in the finalize ``validate``
    pass (R3). The manifest file:line is re-attached from the resource
    origin, so the error frames the declaring document."""
    _manifest(tmp_path, ManifestDoc("git-credential", "ado", {"provider": {"name": "azdo"}}))
    config = _config(tmp_path, enabled=True)
    with pytest.raises(ConfigError, match="org: is required") as exc:
        build_registry(config)
    assert "res.yaml" in str(exc.value)


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
    config = _config(tmp_path, enabled=True)
    with pytest.raises(ConfigError, match="bogus: unknown field; expected one of: name, org, token") as exc:
        build_registry(config)
    assert "res.yaml" in str(exc.value)


def test_a_capability_with_no_config_rejects_every_key() -> None:
    """What the retired base ``validate`` did, now a property of the
    model a capability declares: closed world, so a model carrying only
    its tag accepts an empty blob and nothing else."""

    class _BareConfig(AgwModel):
        name: Literal["bare"]

    class _Bare(GitCredentialProvider):
        name = "bare"
        description = "declares no configuration"
        contract_version = 1
        config_model = _BareConfig

        def _verify_token(self, token: str) -> None: ...

        def helper_entry(self) -> HelperEntry:
            return HelperEntry(host="example.test", username="bare")

        def credential_lines(self, token: str) -> list[str]:
            return []

    _Bare("bare", {})
    with pytest.raises(ConfigError, match="anything: unknown field"):
        _Bare("bare", {"anything": 1})


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
    with pytest.raises(ConfigError, match="org: unknown field; expected one of: name, owner, repos, token"):
        build_registry(config)


def test_unknown_provider_defers_to_miss_policy(tmp_path: Path) -> None:
    """An unregistered provider name skips capability validation; the
    framework's miss policy reports it uniformly at build_registry."""
    _manifest(tmp_path, ManifestDoc("git-credential", "mystery", {"provider": {"name": "sourcehut"}}))
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="sourcehut"):
        build_registry(config)


# -- Fold-gated severity: WHO validates changed, WHEN did not ----------------
#
# The gate itself is the finalize fold's and predates this effort
# (``Registry.finalize`` pass 7 runs the throwing check over the READY and
# ENABLED set only). What the flip changed is that the CORE does the
# validating instead of the capability, so what these pin is that the
# gating survived that change. The property they protect is the reason the
# gate exists: a malformed ``platform_config`` on a site the host cannot
# run must not abort every command.


def _azure_site(tmp_path: Path, *, enabled: bool) -> Any:
    """A vm-site on the opt-in azure plugin's platform, with a blob that
    is malformed whatever the plugin's state (``regions`` is not a field)."""
    _manifest(
        tmp_path,
        ManifestDoc(
            "vm-site",
            "lab",
            {"platform": {"name": "azure-vm", "subscription_id": "s", "resource_group": "g", "regions": "eastus"}},
        ),
    )
    return _config(tmp_path, enabled=enabled)


def test_a_broken_blob_on_a_disabled_plugins_resource_loads_with_the_row_marked(tmp_path: Path) -> None:
    """R9.4, the headline property: the config loads, so every command
    that has nothing to do with this site still works, and the row says
    why it is unusable rather than going silent."""
    registry = build_registry(_azure_site(tmp_path, enabled=False))

    reason = registry.graph.readiness_of("vm-site", "lab").reason
    assert reason is not None, "a deferred row must carry its reason, not just fail quietly later"
    assert "azure-vm" in reason


def test_the_same_broken_blob_is_a_load_error_once_the_plugin_is_enabled(tmp_path: Path) -> None:
    """The other half of the same property, and the enabled + ready case
    the box names: enabling is what makes the resource's config this
    host's problem, and the error is the ordinary owner-framed one."""
    with pytest.raises(ConfigError, match="regions: unknown field") as exc:
        build_registry(_azure_site(tmp_path, enabled=True))

    assert "res.yaml" in str(exc.value)


# An unregistered capability name staying a HARD finalize error (R9.2 /
# R9.11, and the operator decision of 2026-08-01) is the third case the
# fold-gated box names. It is pinned by
# ``test_unknown_provider_defers_to_miss_policy`` above, end to end
# through ``build_registry``, so it is not repeated here.


# -- The finalize validate pass: timing + ordering (R3, R9.3) ----------------


def test_cycle_reported_before_malformed_block(tmp_path: Path) -> None:
    """R9.3: capability-block validation moved out of decode/load into
    the finalize ``validate`` pass, which runs AFTER cycle detection. So
    a config carrying BOTH a malformed block and a cycle now reports the
    cycle first, where the malformed block used to fail earlier at
    decode/load, before finalize ever ran."""
    _manifest(
        tmp_path,
        ManifestDoc(
            "session-template",
            "a",
            {"inherits": ["b"], "harness_integration": {"name": "shell", "nope": "x"}},
        ),
        ManifestDoc("session-template", "b", {"inherits": ["a"]}),
    )
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="cycle detected") as exc:
        build_registry(config)
    # The malformed shell block is deferred behind the cycle, not raised.
    assert "unknown shell harness field" not in str(exc.value)


def test_construct_time_validation_survives_the_flip(tmp_path: Path) -> None:
    """The construct-time invariant is unchanged in substance: a malformed
    blob still dies at construction. What changed is that the check is
    ``model_validate`` and its RESULT is kept, so a provider reading
    ``self.config.org`` is reading a value the model proved is there."""
    from agentworks.plugins.azure.azdo import AzDOCredentialProvider

    with pytest.raises(ConfigError, match="org: is required"):
        AzDOCredentialProvider("ado", {})

    assert AzDOCredentialProvider("ado", {"org": "my-org"}).config.org == "my-org"


# -- The dependencies half ---------------------------------------------------


class _SigningConfig(AgwModel):
    """A config that NAMES a secret, with a constant default. The whole
    of what used to be a hand-rolled ``dependencies`` plus its guard."""

    name: Literal["test-signing"]
    signing_key: Annotated[NonEmptyStr, SecretRef(usage="the signing key", default_template="code-signing-key")]


class _SigningCredentialProvider(GitCredentialProvider):
    """Test-only capability whose config names a secret: exercises the
    declare-and-receive contract end to end."""

    name = "test-signing"
    description = "signs with a declared secret"
    contract_version = 1
    config_model = _SigningConfig

    def _verify_token(self, token: str) -> None: ...

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://signer:{token}@example.test"]

    def helper_entry(self) -> HelperEntry:
        return HelperEntry(host="example.test", username="signer")


@pytest.fixture
def signing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(GIT_CREDENTIAL_PROVIDER_REGISTRY, "test-signing", _SigningCredentialProvider)


def test_capability_refs_attributed_to_consuming_resource(tmp_path: Path, signing_provider: None) -> None:
    """The full contract: the CORE reads the reference the blob implies
    off the capability's declared model; the consuming resource emits it
    as source; the framework
    auto-declares the secret with a per-consumer description."""
    _manifest(tmp_path, ManifestDoc("git-credential", "signer", {"provider": {"name": "test-signing"}}))
    config = _config(tmp_path)
    registry = build_registry(config)
    # Defaulted secret name: auto-declared, attributed to THIS credential.
    decl = registry.lookup("secret", "code-signing-key")
    assert decl.origin.variant == "auto-declared"
    assert "the signing key for git-credential/signer" in decl.description
    sources = {entry.source for entry in registry.graph.dependents_of("secret", "code-signing-key")}
    assert ("git-credential", "signer") in sources


def test_capability_ref_default_is_operator_overridable(tmp_path: Path, signing_provider: None) -> None:
    """The defaulted-and-overridable flavor: pointing the blob field at
    another secret moves the reference. ``signer`` takes the default
    (``code-signing-key``); ``signer2`` overrides ``signing_key`` in its
    provider table onto ``corp-signing-key``."""
    _manifest(
        tmp_path,
        ManifestDoc("git-credential", "signer", {"provider": {"name": "test-signing"}}),
        ManifestDoc("secret", "corp-signing-key", description="Corporate signing key"),
        ManifestDoc(
            "git-credential",
            "signer2",
            {"provider": {"name": "test-signing", "signing_key": "corp-signing-key"}},
        ),
    )
    config = _config(tmp_path)
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
    _manifest(
        tmp_path,
        ManifestDoc(
            "secret", "npm-token", {"backend_mappings": {"env-var": {"vault": "Work"}}}, description="npm token"
        ),
    )
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="backend_mappings.env-var: must be a string"):
        build_registry(config)


def test_prompt_rejects_any_mapping(tmp_path: Path) -> None:
    """Prompt has no mapping vocabulary: any non-false value is dead
    config (a typo for another backend) and errors at build_registry.
    The generic false opt-out is loop-owned and never reaches the
    capability."""
    _manifest(
        tmp_path,
        ManifestDoc("secret", "npm-token", {"backend_mappings": {"prompt": "ignored"}}, description="npm token"),
    )
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="prompt backend has no mapping vocabulary"):
        build_registry(config)


def test_prompt_false_opt_out_still_loads(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        ManifestDoc(
            "secret",
            "npm-token",
            {"backend_mappings": {"env-var": "NPM_TOKEN", "prompt": False}},
            description="npm token",
        ),
    )
    config = _config(tmp_path)
    build_registry(config)  # no error


def test_declared_mapping_for_non_opted_in_backend_is_validated_at_build(tmp_path: Path) -> None:
    """R9.9: a declared mapping addressed to a PRESENT backend is validated at
    build even when that backend is not in the active chain (the secret's own
    ``validate``, run by the finalize pass, checks every present backend's
    mapping, not just the opted-in ones). Here env-var is not opted in (chain
    is prompt-only) but its structured mapping is malformed for env-var, so the
    build now fails, where the old ``validate_chain`` left it dormant."""
    _manifest(
        tmp_path,
        ManifestDoc(
            "secret", "npm-token", {"backend_mappings": {"env-var": {"vault": "Work"}}}, description="npm token"
        ),
    )
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["prompt"]
        """,
    )
    with pytest.raises(ConfigError, match="backend_mappings.env-var: must be a string"):
        build_registry(config)


def test_prompt_rejects_structured_mapping_too(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        ManifestDoc(
            "secret", "npm-token", {"backend_mappings": {"prompt": {"vault": "Work"}}}, description="npm token"
        ),
    )
    config = _config(tmp_path)
    with pytest.raises(ConfigError, match="prompt backend has no mapping vocabulary"):
        build_registry(config)


# The former ``test_github_toml_stray_org_keeps_loading`` was removed here: it
# pinned the flat-TOML loader's behavior of hoisting ``org`` into the blob only
# for azdo, so a released github credential carrying a stray ``org`` loaded with
# the key silently ignored (``provider_config == {}``). config.toml no longer
# declares git-credentials (ADR 0022), and a manifest provider table has no flat
# hoisting: an ``org`` key on a github provider table is an explicit unknown
# field the capability rejects (``unknown github provider field``, pinned by
# ``test_github_rejects_unknown_blob_fields``). The silently-ignored-stray-key
# behavior is structurally gone with the flat TOML surface.
