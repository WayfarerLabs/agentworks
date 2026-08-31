"""Canonical effective desired-state fingerprint for managed VM restore."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from agentworks.debian import DebianRelease, profile_for_release
from agentworks.errors import ConfigError, NotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.resources import Registry
    from agentworks.resources.reference import ResourceReference


def _canonical_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported desired-state value: {type(value).__name__}")


def _vm_declaration(vm: VMRow) -> dict[str, object]:
    return {
        "name": vm.name,
        "site": vm.site,
        "template": vm.template,
        "admin_template": vm.admin_template,
        "extra_packages": vm.extra_packages,
        "cpus": vm.cpus,
        "memory_gib": vm.memory_gib,
        "disk_gib": vm.disk_gib,
        "swap_gib": vm.swap_gib,
        "admin_username": vm.admin_username,
        "hostname": vm.hostname,
        "platform_metadata": vm.platform_metadata,
    }


def _authorized_key_fingerprints(config: Config) -> tuple[str, ...]:
    from agentworks.path_rendering import format_host_path
    from agentworks.ssh_identity import SSHIdentityReadError, parse_public_ssh_identity
    from agentworks.vms.applied_state import prepare_configured_ssh_identity

    prepared = prepare_configured_ssh_identity(
        config.operator.ssh_public_key,
        config.operator.ssh_private_key,
    )
    fingerprints = {parse_public_ssh_identity(prepared.public_text).fingerprint}
    for path in config.operator.extra_ssh_public_keys:
        try:
            text = path.read_text(encoding="utf-8")
            fingerprints.add(parse_public_ssh_identity(text).fingerprint)
        except (OSError, UnicodeError, SSHIdentityReadError) as error:
            hint = (
                error.detail
                if isinstance(error, SSHIdentityReadError)
                else "The public key file must be readable UTF-8."
            )
            raise ConfigError(
                f"configured extra SSH public key cannot be fingerprinted: {format_host_path(path)}",
                hint=hint,
            ) from None
    return tuple(sorted(fingerprints))


def _resource_spec(resource: object, release: DebianRelease) -> object | None:
    """Return declared spec only, excluding provenance and envelope prose."""

    from agentworks.apt import AptSourceEntry
    from agentworks.declared_resource import DeclaredResource
    from agentworks.instance_overlay_codec import OVERLAY_EXCLUDED_FIELDS

    if not isinstance(resource, DeclaredResource):
        return None
    spec = resource.model_dump(
        mode="python",
        exclude=set(OVERLAY_EXCLUDED_FIELDS),
    )
    if isinstance(resource, AptSourceEntry):
        spec.pop("sources", None)
        spec["source"] = resource.source_for(release)
    return spec


def _declared_resource_closure(
    registry: Registry,
    roots: Iterable[tuple[str, str]],
    release: DebianRelease,
) -> list[dict[str, object]]:
    """Walk effective runtime references without following inheritance edges."""

    from agentworks.schema.reference import RefRelationship

    pending = list(sorted(set(roots), reverse=True))
    visited: set[tuple[str, str]] = set()
    result: list[dict[str, object]] = []
    while pending:
        kind, name = pending.pop()
        key = (kind, name)
        if key in visited:
            continue
        visited.add(key)

        try:
            resource = registry.lookup(kind, name)
        except KeyError:
            raise ConfigError(
                f"checkpoint desired state references missing resource {kind}/{name}",
                hint="Restore the matching Agentworks declarations before retrying.",
            ) from None

        if kind == "secret":
            result.append({"kind": kind, "name": name})
            continue

        spec = _resource_spec(resource, release)
        if spec is not None:
            result.append({"kind": kind, "name": name, "spec": spec})

        for reference in reversed(registry.graph.edges_of(kind, name)):
            if reference.relationship is RefRelationship.USES:
                pending.append((reference.kind, reference.name))

    return sorted(result, key=lambda row: (str(row["kind"]), str(row["name"])))


def _reference_roots(references: Iterable[ResourceReference]) -> list[tuple[str, str]]:
    return [(reference.kind, reference.name) for reference in references]


def checkpoint_desired_state_fingerprint(
    db: Database,
    config: Config,
    registry: Registry,
    vm: VMRow,
    *,
    capture_release: DebianRelease,
) -> str:
    """Hash topology plus resolved non-secret declarations for one VM tree."""

    from agentworks.agents.template import effective_references as agent_references
    from agentworks.agents.templates import resolve_live_template_with_provenance as resolve_agent_template
    from agentworks.instance_specs import get_vm_instance_overlays
    from agentworks.sessions.template import effective_references as session_references
    from agentworks.sessions.templates import resolve_live_template_with_provenance as resolve_session_template
    from agentworks.vms.admin import effective_references as admin_references
    from agentworks.vms.admin_templates import resolve_template_with_provenance as resolve_admin_template
    from agentworks.vms.sites import lookup_site
    from agentworks.vms.template import effective_references as vm_references
    from agentworks.vms.templates import resolve_live_template_with_provenance as resolve_vm_template
    from agentworks.workspaces.template import effective_references as workspace_references
    from agentworks.workspaces.templates import resolve_live_template_with_provenance as resolve_workspace_template

    profile_for_release(capture_release)
    with db.transaction():
        current_vm = db.get_vm(vm.name)
        if current_vm is None:
            raise NotFoundError(
                f"VM '{vm.name}' not found",
                entity_kind="vm",
                entity_name=vm.name,
            )
        workspaces = sorted(db.list_workspaces(vm_name=vm.name), key=lambda row: row.name)
        agents = sorted(db.list_agents(vm_name=vm.name), key=lambda row: row.name)
        sessions = sorted(db.list_sessions(vm_name=vm.name), key=lambda row: row.name)
        consoles = sorted(db.list_consoles(vm_name=vm.name), key=lambda row: row.name)
        grants = sorted(
            (grant for agent in agents for grant in db.list_agent_grants(agent.name)),
            key=lambda row: (row.agent_name, row.workspace_name, row.grant_type, row.session_name or ""),
        )
        memberships = sorted(
            (member for console in consoles for member in db.list_console_sessions(console.name)),
            key=lambda row: (row.console_name, row.position, row.session_name),
        )
        overlays = db.instance_state.list_vm_owner_tree_desired_overlays(vm.name)

        vm_layered = resolve_vm_template(db, registry, current_vm.name, current_vm.template)
        vm_refs = vm_references(vm_layered.value, ("vm", current_vm.name), vm_layered.provenance)

        vm_overlays = get_vm_instance_overlays(db, current_vm.name)
        admin_layered = resolve_admin_template(
            registry,
            current_vm.admin_template,
            overlay=None if vm_overlays is None else vm_overlays.admin,
            instance_name=current_vm.name,
        )
        admin_refs = admin_references(
            admin_layered.value,
            ("vm", current_vm.name),
            admin_layered.provenance,
        )

        workspace_layers = [
            (row, resolve_workspace_template(db, registry, row.name, row.template)) for row in workspaces
        ]
        agent_layers = [(row, resolve_agent_template(db, registry, row.name, row.template)) for row in agents]
        session_layers = [(row, resolve_session_template(db, registry, row.name, row.template)) for row in sessions]
        site_spec = _resource_spec(lookup_site(current_vm.site, registry), capture_release)
        assert site_spec is not None

        reference_roots = [("vm-site", current_vm.site)]
        reference_roots.extend(_reference_roots(vm_refs))
        reference_roots.extend(_reference_roots(admin_refs))
        for workspace_row, workspace_layered in workspace_layers:
            reference_roots.extend(
                _reference_roots(
                    workspace_references(
                        workspace_layered.value,
                        ("workspace", workspace_row.name),
                        workspace_layered.provenance,
                    )
                )
            )
        for agent_row, agent_layered in agent_layers:
            reference_roots.extend(
                _reference_roots(
                    agent_references(
                        agent_layered.value,
                        ("agent", agent_row.name),
                        agent_layered.provenance,
                    )
                )
            )
        for session_row, session_layered in session_layers:
            reference_roots.extend(
                _reference_roots(
                    session_references(
                        session_layered.value,
                        ("session", session_row.name),
                        session_layered.provenance,
                    )
                )
            )

        projection: dict[str, object] = {
            "capture_release": capture_release,
            "vm": _vm_declaration(current_vm),
            "site": site_spec,
            "authorized_ssh_key_fingerprints": _authorized_key_fingerprints(config),
            "effective_vm_template": vm_layered.value,
            "effective_admin_template": admin_layered.value,
            "workspaces": [
                {
                    "row": {
                        "name": row.name,
                        "vm_name": row.vm_name,
                        "template": row.template,
                        "workspace_path": row.workspace_path,
                        "linux_group": row.linux_group,
                    },
                    "effective_template": layered.value,
                }
                for row, layered in workspace_layers
            ],
            "agents": [
                {
                    "row": {
                        "name": row.name,
                        "vm_name": row.vm_name,
                        "linux_user": row.linux_user,
                        "template": row.template,
                        "grant_all": row.grant_all,
                    },
                    "effective_template": layered.value,
                }
                for row, layered in agent_layers
            ],
            "grants": [
                {
                    "agent_name": row.agent_name,
                    "workspace_name": row.workspace_name,
                    "grant_type": row.grant_type,
                    "session_name": row.session_name,
                }
                for row in grants
            ],
            "sessions": [
                {
                    "row": {
                        "name": row.name,
                        "workspace_name": row.workspace_name,
                        "template": row.template,
                        "mode": row.mode,
                        "agent_name": row.agent_name,
                        "created_workspace": row.created_workspace,
                        "created_agent": row.created_agent,
                    },
                    "effective_template": layered.value,
                }
                for row, layered in session_layers
            ],
            "consoles": [
                {"name": row.name, "vm_name": row.vm_name, "admin_shell": row.admin_shell} for row in consoles
            ],
            "console_memberships": [
                {
                    "console_name": row.console_name,
                    "session_name": row.session_name,
                    "position": row.position,
                    "shells": row.shells,
                }
                for row in memberships
            ],
            "desired_overlays": [
                {
                    "instance_kind": row.instance_kind,
                    "instance_name": row.instance_name,
                    "payload_version": row.payload.payload_version,
                    "value": row.payload.value,
                }
                for row in overlays
            ],
            "declared_resource_closure": _declared_resource_closure(
                registry,
                reference_roots,
                capture_release,
            ),
        }

    encoded = json.dumps(
        _canonical_value(projection),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
