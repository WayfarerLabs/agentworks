"""The three kinds that host a capability: vm-site, git-credential,
session-template.

Each carries the capability as ONE tagged table, which is what the
operator writes, so the row and the manifest have the same shape and
nothing has to be synthesized back together. These are the properties
that follow from that.
"""

from __future__ import annotations

import pytest

from agentworks.git_credentials.credential import GitCredentialConfig
from agentworks.naming import MAX_FREEFORM_NAME_LENGTH
from agentworks.schema import CapabilityBlock
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.sites import VMSiteDecl

from ._specs import WHERE, decode, decode_issues, rejection

_HOSTS = [
    ("vm-site", "lab", "platform", "lima"),
    ("git-credential", "gh", "provider", "github"),
    ("session-template", "htop", "harness_integration", "shell"),
]


@pytest.mark.parametrize(("kind", "name", "field", "capability"), _HOSTS)
def test_the_tagged_table_lands_on_the_row_as_written(kind: str, name: str, field: str, capability: str) -> None:
    row = decode(kind, name, {field: {"name": capability, "extra_key": "v"}})
    block = getattr(row, field)

    assert block.name == capability
    assert block.config == {"extra_key": "v"}
    assert block.tagged == {"name": capability, "extra_key": "v"}


@pytest.mark.parametrize(("kind", "name", "field", "capability"), _HOSTS)
def test_the_capabilitys_own_keys_are_not_validated_here(kind: str, name: str, field: str, capability: str) -> None:
    """A key the capability does not accept decodes fine and is refused by
    the capability's own model at finalize (R3). Decode validating it too
    is how a host kind would end up encoding what its capabilities
    accept."""
    assert decode(kind, name, {field: {"name": capability, "nonsense": 1}}) is not None


def test_a_vm_site_round_trips() -> None:
    assert decode("vm-site", "lab", {"platform": {"name": "lima", "vm_host": "me@box"}}) == VMSiteDecl(
        name="lab", declared_at=WHERE, platform=CapabilityBlock.of("lima", vm_host="me@box")
    )


def test_a_git_credential_round_trips() -> None:
    assert decode("git-credential", "gh", {"provider": {"name": "github", "owner": "acme"}}) == GitCredentialConfig(
        name="gh", declared_at=WHERE, provider=CapabilityBlock.of("github", owner="acme")
    )


def test_a_session_template_round_trips() -> None:
    spec = {"inherits": ["base"], "harness_integration": {"name": "shell", "command": "htop"}}

    assert decode("session-template", "htop", dict(spec)) == SessionTemplate(
        name="htop",
        declared_at=WHERE,
        inherits=["base"],
        harness_integration=CapabilityBlock.of("shell", command="htop"),
    )


def test_a_session_template_may_name_no_integration() -> None:
    """``None`` is "not declared here", which the merge reads: a template
    with no selector legitimately inherits one or stays the default login
    shell."""
    assert decode("session-template", "child", {"inherits": ["base"]}).harness_integration is None


# -- What an operator reads when it is wrong ----------------------------------


@pytest.mark.parametrize(("kind", "name", "field", "capability"), _HOSTS)
def test_a_table_with_no_tag_reads_as_a_missing_field(kind: str, name: str, field: str, capability: str) -> None:
    assert rejection(kind, name, {field: {"vm_host": "h"}}) == f"res.yaml:7: {kind}/{name}.{field}.name: is required"


@pytest.mark.parametrize(("kind", "name", "field", "capability"), _HOSTS)
def test_a_non_table_reads_as_a_table_requirement(kind: str, name: str, field: str, capability: str) -> None:
    """A scalar that is not even a capability name, so not the retired
    string shape its own guard refuses first."""
    assert rejection(kind, name, {field: 42}) == f"res.yaml:7: {kind}/{name}.{field}: must be a table"


@pytest.mark.parametrize(("kind", "name"), [("vm-site", "lab"), ("git-credential", "gh")])
def test_a_host_that_requires_a_capability_says_so(kind: str, name: str) -> None:
    field = "platform" if kind == "vm-site" else "provider"

    assert rejection(kind, name, {}) == f"res.yaml:7: {kind}/{name}.{field}: is required"


def test_a_top_level_token_keeps_its_steer() -> None:
    """The mistake operators make coming from the flat TOML shape. As a
    plain unknown key it would name the valid field without saying where
    the token goes."""
    assert rejection("git-credential", "gh", {"token": "t"}) == (
        "res.yaml:7: git-credential/gh: 'token' is provider config now: move it into the "
        "spec.provider table (its 'name' key selects the provider)"
    )


def test_a_site_named_after_a_platform_must_declare_it() -> None:
    """``vm-site/azure-vm`` backed by lima would make every
    ``--site azure-vm`` mean something other than it says."""
    assert rejection("vm-site", "lima", {"platform": {"name": "wsl2"}}) == (
        "res.yaml:7: vm-site/lima: a vm-site named 'lima' must declare platform 'lima' "
        "(it shadows a platform name), not 'wsl2'"
    )


def test_a_site_name_takes_the_freeform_cap() -> None:
    """Site names hit no OS identifier limit: they are a registry key and
    a display surface only."""
    assert rejection("vm-site", "a" * (MAX_FREEFORM_NAME_LENGTH + 1), {"platform": {"name": "lima"}}).endswith(
        f"max {MAX_FREEFORM_NAME_LENGTH})"
    )


# -- The advisory that reaches through the block ------------------------------


def test_a_non_conforming_secret_inside_the_block_is_warned_about() -> None:
    """Found through the capability's OWN model, which is where the
    ``SecretRef`` lives: decode has no per-kind knowledge of which
    capability names a secret."""
    (issue,) = decode_issues("git-credential", "gh", {"provider": {"name": "github", "token": "Bad_Name"}})

    assert issue.startswith("res.yaml:7: git-credential/gh: secret name 'Bad_Name' for the auth token")


def test_an_unseated_capabilitys_block_earns_no_advisory() -> None:
    """The one honest soft edge, named rather than left to be discovered:
    the walk reads the capability's declared model, so a capability with
    no seated implementation contributes nothing. In production that also
    covers a plugin capability at MANIFEST-LOAD time, because plugins seat
    after `load_manifests` runs. A missed advisory line, never a wrong
    answer; the finalize pass still checks the blob's shape once the
    implementation is there."""
    assert decode_issues("vm-site", "px", {"platform": {"name": "not-installed", "token_secret": "Bad_Name"}}) == []


def test_a_derived_secret_name_is_warned_about_without_being_written() -> None:
    """Issue #308: a git-credential whose NAME makes ``git-token-<name>``
    non-conforming. Nothing in the document names that secret, so the
    warning comes from the marker's owner template through the same
    structural walk."""
    (issue,) = decode_issues("git-credential", "GITHUB", {"provider": {"name": "github"}})

    assert "git-token-GITHUB" in issue
