"""Core-domain adoption of schema-directed declaration merging."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import ClassVar

from agentworks.agents.template import AgentTemplate
from agentworks.agents.template import effective_references as agent_references
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.agents.templates import resolve_from_dict_with_provenance as resolve_agent
from agentworks.env.entry import EnvEntry
from agentworks.resources.inheritance import LayerSource, LayerSourceKind
from agentworks.schema import AgwModel, MergeStrategy
from agentworks.template_layers import merge_resolved_template_layer
from agentworks.vms.admin import AdminConfig
from agentworks.vms.admin import effective_references as admin_references
from agentworks.vms.admin_templates import resolve_from_dict_with_provenance as resolve_admin
from agentworks.vms.template import VMTemplate
from agentworks.vms.template import effective_references as vm_references
from agentworks.vms.templates import ResolvedVMTemplate
from agentworks.vms.templates import resolve_from_dict_with_provenance as resolve_vm
from agentworks.workspaces.template import WorkspaceTemplate
from agentworks.workspaces.template import effective_references as workspace_references
from agentworks.workspaces.templates import ResolvedTemplate
from agentworks.workspaces.templates import resolve_from_dict_with_provenance as resolve_workspace


def _without_name(
    value: ResolvedVMTemplate | ResolvedTemplate | ResolvedAgentTemplate,
) -> dict[str, object]:
    return {name: field_value for name, field_value in asdict(value).items() if name != "name"}


@dataclass
class _ResolvedProjection:
    name: str
    values: list[str] = field(default_factory=list)
    mode: str = "default"
    resolved_only: str = "retained"


class _ProjectionDeclaration(AgwModel):
    name: str
    values: list[str] | None = None
    mode: str | None = None
    declaration_only: str | None = None


class _ReplacingProjectionDeclaration(_ProjectionDeclaration):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE


class _ReplacingAdminConfig(AdminConfig):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE


def test_resolved_layer_projection_does_not_require_identical_field_sets() -> None:
    target = _ResolvedProjection(name="selected", values=["base"])
    declaration = _ProjectionDeclaration(
        name="declaration-name",
        values=["base", "child"],
        declaration_only="not part of the resolved value",
    )

    resolved, _operations = merge_resolved_template_layer(target, declaration, object())

    assert resolved.name == "selected"
    assert resolved.values == ["base", "child"]
    assert resolved.resolved_only == "retained"
    assert not hasattr(resolved, "declaration_only")
    assert target.values == ["base"]


def test_root_replacement_resets_omitted_resolved_fields_without_touching_resolved_only_fields() -> None:
    target = _ResolvedProjection(name="selected", values=["base"], mode="parent", resolved_only="retained")
    declaration = _ReplacingProjectionDeclaration(name="declaration-name")

    resolved, _operations = merge_resolved_template_layer(target, declaration, object())

    assert resolved.values == []
    assert resolved.mode == "default"
    assert resolved.resolved_only == "retained"
    assert target.values == ["base"]


def test_vm_template_and_instance_layers_share_schema_merge_semantics() -> None:
    base = VMTemplate(
        name="base",
        cpus=6,
        apt=["git"],
        apt_packages=["base", "shared"],
        env={"MODE": EnvEntry.model_validate({"secret": "base-mode"})},
    )
    child = VMTemplate(
        name="child",
        inherits=["base"],
        cpus=8,
        apt=["git", "jq"],
        apt_packages=["shared", "instance"],
        env={"MODE": EnvEntry.model_validate("instance")},
    )
    overlay = child.model_copy(update={"name": "overlay", "inherits": []})

    inherited = resolve_vm({"base": base, "child": child}, "child")
    overlaid = resolve_vm({"base": base}, "base", overlay=overlay, instance_name="vm-1")

    assert _without_name(inherited.value) == _without_name(overlaid.value)
    assert inherited.value.apt == ["git", "jq"]
    assert inherited.value.env["MODE"].value == "instance"
    assert set(inherited.provenance) == set(overlaid.provenance)
    reference_owners = {
        reference.name: reference.declarer
        for reference in vm_references(overlaid.value, ("vm", "vm-1"), overlaid.provenance)
        if reference.kind == "apt-package"
    }
    assert reference_owners == {
        "base": ("vm-template", "base"),
        "shared": ("vm", "vm-1"),
        "instance": ("vm", "vm-1"),
    }


def test_workspace_template_and_instance_layers_share_schema_merge_semantics() -> None:
    base = WorkspaceTemplate(
        name="base",
        repo="https://example.test/base.git",
        env={"MODE": EnvEntry.model_validate({"secret": "base-mode"})},
    )
    child = WorkspaceTemplate(
        name="child",
        inherits=["base"],
        repo="https://example.test/child.git",
        env={"MODE": EnvEntry.model_validate("instance")},
    )
    overlay = child.model_copy(update={"name": "overlay", "inherits": []})

    inherited = resolve_workspace({"base": base, "child": child}, "child")
    overlaid = resolve_workspace({"base": base}, "base", overlay=overlay, instance_name="workspace-1")

    assert _without_name(inherited.value) == _without_name(overlaid.value)
    assert inherited.value.env["MODE"].value == "instance"
    assert set(inherited.provenance) == set(overlaid.provenance)


def test_agent_template_and_instance_layers_share_schema_merge_semantics() -> None:
    base = AgentTemplate(
        name="base",
        shell="bash",
        git_credentials=["base", "shared"],
        env={"MODE": EnvEntry.model_validate({"secret": "base-mode"})},
    )
    child = AgentTemplate(
        name="child",
        inherits=["base"],
        shell="zsh",
        git_credentials=["shared", "instance"],
        env={"MODE": EnvEntry.model_validate("instance")},
    )
    overlay = child.model_copy(update={"name": "overlay", "inherits": []})

    inherited = resolve_agent({"base": base, "child": child}, "child")
    overlaid = resolve_agent({"base": base}, "base", overlay=overlay, instance_name="agent-1")

    assert _without_name(inherited.value) == _without_name(overlaid.value)
    assert inherited.value.git_credentials == ["base", "shared", "instance"]
    assert inherited.value.env["MODE"].value == "instance"
    assert set(inherited.provenance) == set(overlaid.provenance)


def test_optional_template_none_remains_absent_for_template_and_instance_layers() -> None:
    base = VMTemplate(name="base", cpus=12)
    silent = VMTemplate(name="silent", inherits=["base"], cpus=None)
    overlay = VMTemplate(name="overlay", cpus=None)

    inherited = resolve_vm({"base": base, "silent": silent}, "silent")
    overlaid = resolve_vm({"base": base}, "base", overlay=overlay, instance_name="vm-1")

    assert inherited.value.cpus == 12
    assert overlaid.value.cpus == 12
    assert inherited.provenance[("cpus",)][-1].name == "base"
    assert overlaid.provenance[("cpus",)][-1].name == "base"


def test_admin_omissions_do_not_materialize_layer_defaults() -> None:
    base = AdminConfig(
        name="ops",
        shell="zsh",
        git_credentials=["base", "shared"],
        user_install_commands=["base-command"],
        mise_install_before="30d",
    )
    overlay = AdminConfig(
        name="overlay",
        git_credentials=["shared", "instance"],
        user_install_commands=["base-command", "instance-command"],
    )

    resolution = resolve_admin({"ops": base}, "ops", overlay=overlay, instance_name="vm-1")

    assert resolution.value.shell == "zsh"
    assert resolution.value.git_credentials == ["base", "shared", "instance"]
    assert resolution.value.mise_install_before == "30d"
    assert resolution.provenance[("shell",)][-1].name == "ops"
    reference_owners = {
        (reference.kind, reference.name): reference.declarer
        for reference in admin_references(
            resolution.value,
            ("vm", "vm-1"),
            resolution.provenance,
        )
    }
    assert reference_owners == {
        ("git-credential", "base"): ("admin-template", "ops"),
        ("git-credential", "shared"): ("vm", "vm-1"),
        ("git-credential", "instance"): ("vm", "vm-1"),
        ("user-install-command", "base-command"): ("vm", "vm-1"),
        ("user-install-command", "instance-command"): ("vm", "vm-1"),
    }


def test_admin_root_replacement_resets_omitted_parent_fields() -> None:
    base = AdminConfig(
        name="ops",
        shell="zsh",
        git_credentials=["base"],
        user_install_commands=["base-command"],
    )
    overlay = _ReplacingAdminConfig(name="overlay", git_credentials=["instance"])

    resolution = resolve_admin({"ops": base}, "ops", overlay=overlay, instance_name="vm-1")

    assert resolution.value.shell == "bash"
    assert resolution.value.git_credentials == ["instance"]
    assert resolution.value.user_install_commands == []
    assert all(source.name != "ops" for sources in resolution.provenance.values() for source in sources)


def test_list_reference_owners_follow_result_indices_and_duplicate_contributors() -> None:
    base = AgentTemplate(
        name="base",
        git_credentials=["base", "shared"],
        user_install_commands=["base-command"],
    )
    overlay = AgentTemplate(
        name="overlay",
        git_credentials=["shared", "instance"],
        user_install_commands=["base-command", "instance-command"],
    )

    resolution = resolve_agent({"base": base}, "base", overlay=overlay, instance_name="agent-1")
    references = {
        (reference.kind, reference.name): reference.declarer
        for reference in agent_references(resolution.value, ("agent", "agent-1"), resolution.provenance)
    }

    assert [source.name for source in resolution.provenance[("git_credentials", 1)]] == [
        "base",
        "agent-1",
    ]
    assert resolution.provenance[("git_credentials", 2)][-1].name == "agent-1"
    assert references == {
        ("git-credential", "base"): ("agent-template", "base"),
        ("git-credential", "shared"): ("agent", "agent-1"),
        ("git-credential", "instance"): ("agent", "agent-1"),
        ("user-install-command", "base-command"): ("agent", "agent-1"),
        ("user-install-command", "instance-command"): ("agent", "agent-1"),
    }


def test_reference_owner_falls_back_to_the_longest_provenance_prefix() -> None:
    effective = ResolvedTemplate(
        name="workspace-1",
        env={"TOKEN": EnvEntry.model_validate({"secret": "workspace-token"})},
    )
    source = LayerSource(LayerSourceKind.INSTANCE, "workspace", "workspace-1")

    references = workspace_references(
        effective,
        ("workspace", "workspace-1"),
        {("env",): (source,)},
    )

    assert len(references) == 1
    assert references[0].declarer == ("workspace", "workspace-1")
