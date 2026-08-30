"""Closed resolved-spec projection and provenance behavior."""

from __future__ import annotations

from agentworks.env.entry import EnvEntry
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.resolved_spec import (
    UnresolvedSpec,
    project_resolved_spec,
    resolved_spec_data,
)
from agentworks.vms.admin import AdminConfig
from agentworks.vms.admin_templates import resolve_from_dict_with_provenance as resolve_admin
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import resolve_from_dict_with_provenance as resolve_vm


def test_projection_is_complete_ordered_and_maps_layer_roles() -> None:
    base = VMTemplate(name="base")
    selected = VMTemplate(
        name="dev",
        inherits=["base"],
        cpus=8,
    )
    projected = project_resolved_spec(
        resolve_vm({"base": base, "dev": selected}, "dev"),
        ResourceIdentity("vm-template", "dev"),
    )

    assert list(projected.spec) == [
        "cpus",
        "memory",
        "disk",
        "swap",
        "apt",
        "apt_packages",
        "snap",
        "system_install_commands",
        "env",
        "tailscale_auth_key",
    ]
    by_path = {item.path: item.sources for item in projected.provenance}
    assert set(projected.spec) == {path[0] for path in by_path}
    assert [source.role for source in by_path[("cpus",)]] == ["declared"]
    assert [source.role for source in by_path[("memory",)]] == ["defaulted"]


def test_inherited_map_container_uses_truthful_descendant_paths() -> None:
    projected = project_resolved_spec(
        resolve_vm(
            {
                "base": VMTemplate(
                    name="base",
                    env={"TOKEN": EnvEntry.model_validate({"secret": "build-token"})},
                ),
                "dev": VMTemplate(
                    name="dev",
                    inherits=["base"],
                    env={"DEBUG": EnvEntry.model_validate("1")},
                ),
            },
            "dev",
        ),
        ResourceIdentity("vm-template", "dev"),
    )

    assert projected.spec["env"] == {
        "TOKEN": {"secret": "build-token"},
        "DEBUG": {"value": "1"},
    }
    by_path = {item.path: item.sources for item in projected.provenance}
    assert ("env",) not in by_path
    assert [source.role for source in by_path[("env", "TOKEN")]] == ["inherited"]
    assert [source.role for source in by_path[("env", "TOKEN", "secret")]] == ["inherited"]
    assert [source.role for source in by_path[("env", "DEBUG")]] == ["declared"]
    assert [source.role for source in by_path[("env", "DEBUG", "value")]] == ["declared"]


def test_append_list_uses_final_positions_and_ordered_contributors() -> None:
    projected = project_resolved_spec(
        resolve_vm(
            {
                "base": VMTemplate(name="base", apt=["git"]),
                "dev": VMTemplate(name="dev", inherits=["base"], apt=["git", "jq"]),
            },
            "dev",
        ),
        ResourceIdentity("vm-template", "dev"),
    )

    assert projected.spec["apt"] == ["git", "jq"]
    by_path = {item.path: item.sources for item in projected.provenance}
    assert ("apt",) not in by_path
    assert [source.role for source in by_path[("apt", 0)]] == ["inherited", "declared"]
    assert [source.role for source in by_path[("apt", 1)]] == ["declared"]


def test_instance_layer_is_projected_as_overlaid() -> None:
    projected = project_resolved_spec(
        resolve_vm(
            {"dev": VMTemplate(name="dev", cpus=8)},
            "dev",
            overlay=VMTemplate(name="overlay", memory=16),
            instance_name="vm-1",
        ),
        ResourceIdentity("vm-template", "dev"),
    )

    memory = next(item for item in projected.provenance if item.path == ("memory",))
    assert [(source.role, source.resource_kind, source.resource_name) for source in memory.sources] == [
        ("overlaid", "vm", "vm-1")
    ]


def test_admin_projection_omits_declaration_envelope_and_seeds_empty_values() -> None:
    projected = project_resolved_spec(
        resolve_admin({"ops": AdminConfig(name="ops", shell="zsh")}, "ops"),
        ResourceIdentity("admin-template", "ops"),
    )

    assert not ({"name", "description", "expires", "declared_at", "origin"} & set(projected.spec))
    assert projected.spec["git_credentials"] == []
    assert projected.spec["dotfiles_source"] is None
    by_path = {item.path: item.sources for item in projected.provenance}
    assert by_path[("git_credentials",)][0].role == "defaulted"
    assert by_path[("dotfiles_source",)][0].role == "defaulted"
    assert by_path[("env",)][0].role == "defaulted"
    assert by_path[("shell",)][0].role == "declared"


def test_replaced_object_subtree_keeps_only_the_replacement_source() -> None:
    projected = project_resolved_spec(
        resolve_vm(
            {
                "base": VMTemplate(
                    name="base",
                    env={"TOKEN": EnvEntry.model_validate({"secret": "build-token"})},
                ),
                "dev": VMTemplate(
                    name="dev",
                    inherits=["base"],
                    env={"TOKEN": EnvEntry.model_validate("literal")},
                ),
            },
            "dev",
        ),
        ResourceIdentity("vm-template", "dev"),
    )

    by_path = {item.path: item.sources for item in projected.provenance}
    assert ("env",) not in by_path
    assert [source.role for source in by_path[("env", "TOKEN")]] == ["declared"]
    assert [source.role for source in by_path[("env", "TOKEN", "value")]] == ["declared"]
    assert ("env", "TOKEN", "secret") not in by_path


def test_unresolved_projection_is_closed_and_does_not_invent_a_spec() -> None:
    data = resolved_spec_data(
        UnresolvedSpec(
            ResourceIdentity("workspace-template", "removed"),
            "missing-selection",
        )
    )

    assert data == {
        "status": "unresolved",
        "selection": {"kind": "workspace-template", "name": "removed"},
        "reason": "missing-selection",
    }
