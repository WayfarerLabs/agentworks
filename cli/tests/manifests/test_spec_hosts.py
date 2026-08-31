"""The four kinds that host a capability: vm-site, git-credential,
session-template, and secret-source.

Each carries the capability as ONE tagged table, which is what the
operator writes, so the row and the manifest have the same shape and
nothing has to be synthesized back together. These are the properties
that follow from that.
"""

from __future__ import annotations

from pathlib import Path

from agentworks.git_credentials.credential import GitCredentialConfig
from agentworks.naming import MAX_FREEFORM_NAME_LENGTH
from agentworks.schema import CapabilityBlock
from agentworks.secrets.sources import SecretSourceDecl
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.sites import VMSiteDecl

from ._specs import WHERE, decode, decode_issues, rejection

#: ``(kind, name, field, capability)`` per hosting kind. A corpus, not a
#: table of cases: every sweep below applies ONE claim to all four, and
#: what breaks a claim is the shared decode path, so the useful report is
#: every host that stopped answering rather than one red id per host.
#: ``test_capability_shape.py::test_retired_capability_shapes_fail_decode_or_build``
#: exercises a retired sibling spelling for each host in this list.
_HOSTS = [
    ("vm-site", "lab", "platform", "lima"),
    ("git-credential", "gh", "provider", "github"),
    ("session-template", "htop", "harness_integration", "shell"),
    ("secret-source", "ci-env", "backend", "env-var"),
]


def test_the_tagged_table_lands_on_the_row_as_written() -> None:
    wrong: list[str] = []
    for kind, name, field, capability in _HOSTS:
        block = getattr(decode(kind, name, {field: {"name": capability, "extra_key": "v"}}), field)
        got = (block.name, block.config, block.tagged)
        want = (capability, {"extra_key": "v"}, {"name": capability, "extra_key": "v"})
        if got != want:
            wrong.append(f"{kind}.{field}: {got} is not {want}")
    assert not wrong, "\n".join(wrong)


def test_the_capabilitys_own_keys_are_not_validated_here() -> None:
    """A key the capability does not accept decodes fine and is refused by
    the capability's own model at finalize (R3). Decode validating it too
    is how a host kind would end up encoding what its capabilities
    accept."""
    for kind, name, field, capability in _HOSTS:
        assert decode(kind, name, {field: {"name": capability, "nonsense": 1}}) is not None, kind


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


def test_a_secret_source_round_trips() -> None:
    assert decode("secret-source", "ci-env", {"backend": {"name": "env-var"}}) == SecretSourceDecl(
        name="ci-env",
        declared_at=WHERE,
        backend=CapabilityBlock.of("env-var"),
    )


def test_a_session_template_may_name_no_integration() -> None:
    """``None`` is "not declared here", which the merge reads: a template
    with no selector legitimately inherits one or stays the default login
    shell."""
    assert decode("session-template", "child", {"inherits": ["base"]}).harness_integration is None


# -- What an operator reads when it is wrong ----------------------------------


def test_a_table_with_no_tag_reads_as_a_missing_field() -> None:
    misread = [
        (kind, got)
        for kind, name, field, _capability in _HOSTS
        if (got := rejection(kind, name, {field: {"vm_host": "h"}}))
        != f"res.yaml:7: {kind}/{name}.{field}.name: is required"
    ]
    assert not misread


def test_a_non_table_reads_as_a_table_requirement() -> None:
    """A scalar that is not even a capability name, so not the retired
    string shape its own guard refuses first."""
    misread = [
        (kind, got)
        for kind, name, field, _capability in _HOSTS
        if (got := rejection(kind, name, {field: 42})) != f"res.yaml:7: {kind}/{name}.{field}: must be a table"
    ]
    assert not misread


def test_a_host_that_requires_a_capability_says_so() -> None:
    """The two hosts whose capability is REQUIRED. ``session-template`` is
    absent on purpose: its selector is optional, which
    ``test_a_session_template_may_name_no_integration`` above states."""
    misread = [
        (kind, got)
        for kind, name, field, _capability in _HOSTS
        if kind != "session-template"
        and (got := rejection(kind, name, {})) != f"res.yaml:7: {kind}/{name}.{field}: is required"
    ]
    assert not misread


def test_a_site_named_after_a_platform_must_declare_it() -> None:
    """``vm-site/azure-vm`` backed by lima would make every
    ``--site azure-vm`` mean something other than it says."""
    assert rejection("vm-site", "lima", {"platform": {"name": "wsl2"}}) == (
        "res.yaml:7: vm-site/lima: a vm-site named 'lima' must declare platform 'lima' "
        "(it shadows a platform name), not 'wsl2'"
    )


def test_a_site_name_takes_the_freeform_cap() -> None:
    """Site names hit no OS identifier limit: they are a registry key and
    a display surface only, NOT derived into hostnames or SSH aliases (VM
    names are), so the bound is the freeform 64 rather than the tighter
    VM-name cap.

    Both directions, because either alone leaves the number unpinned from
    one side: a name AT the cap decodes, and the first one past it is
    refused with the cap in the message.
    """
    at_the_cap = "a" * MAX_FREEFORM_NAME_LENGTH
    assert decode("vm-site", at_the_cap, {"platform": {"name": "lima"}}).name == at_the_cap
    assert rejection("vm-site", "a" * (MAX_FREEFORM_NAME_LENGTH + 1), {"platform": {"name": "lima"}}).endswith(
        f"max {MAX_FREEFORM_NAME_LENGTH})"
    )


# -- The advisory that reaches through the block ------------------------------


def test_a_non_conforming_secret_inside_the_block_is_warned_about() -> None:
    """Found through the capability's OWN model, which is where the
    ``SecretRef`` lives: decode has no per-kind knowledge of which
    capability names a secret."""
    (issue,) = decode_issues(
        "git-credential",
        "gh",
        {"provider": {"name": "github", "source": {"mode": "secret", "secret": "Bad_Name"}}},
    )

    assert "Bad_Name" in issue


def test_an_unknown_capabilitys_block_earns_no_advisory() -> None:
    """The walk reads the capability's declared model, so a capability
    nothing implements contributes nothing. The dangling capability edge
    is what reports the name (R9.2), and the finalize pass checks the
    blob once an implementation exists."""
    assert decode_issues("vm-site", "px", {"platform": {"name": "not-installed", "token_secret": "Bad_Name"}}) == []


def test_a_plugin_capabilitys_block_earns_its_advisory_at_load(tmp_path: Path) -> None:
    """A plugin's impls seat when ``agentworks.plugins`` is imported, and
    no caller of ``load_manifests`` is obliged to have done that first.
    Doctor is the surface this advisory exists for and loads manifests
    before it reaches anything that imports the index, so this used to
    produce no line at all and doctor said "Config is valid".

    Run in a SUBPROCESS on purpose: in-process the suite has long since
    imported the plugin index, so an assertion here would pass whether or
    not decode does the import itself. The import order is the thing under
    test."""
    import subprocess
    import sys
    from textwrap import dedent

    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "site.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: px
        spec:
          platform:
            name: proxmox
            api_url: https://pve:8006
            node: n
            token_id: t
            template_vmid: 9000
            token_secret: Bad_Name
        """)
    )
    script = dedent(f"""\
        from pathlib import Path
        from agentworks.manifests import load_manifests
        print(len(load_manifests(Path({str(resources)!r})).issues))
        """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)  # noqa: S603

    assert result.stdout.strip() == "1", result.stderr


def test_a_derived_secret_name_is_warned_about_without_being_written() -> None:
    """Issue #308: a git-credential whose NAME makes ``git-token-<name>``
    non-conforming. Nothing in the document names that secret, so the
    warning comes from the marker's owner template through the same
    structural walk."""
    (issue,) = decode_issues(
        "git-credential",
        "GITHUB",
        {"provider": {"name": "github", "source": {"mode": "secret"}}},
    )

    assert "git-token-GITHUB" in issue
