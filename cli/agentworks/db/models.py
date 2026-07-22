"""Row types for the Agentworks state database: enums, dataclasses, one
TypedDict, and the small constants owned by these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict


class ProvisioningStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


class InitStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class VMStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    DEALLOCATED = "deallocated"
    UNKNOWN = "unknown"


class SessionMode(Enum):
    ADMIN = "admin"
    AGENT = "agent"


class SessionStatus(Enum):
    """Session liveness state, computed live from has-session + PID/boot_id checks."""

    OK = "ok"
    STOPPED = "stopped"
    BROKEN = "broken"
    UNKNOWN = "unknown"


# Sentinel PID value: session is known to be stopped (no process to check).
# Distinct from NULL (never checked / pre-enhancement).
PID_STOPPED = -1

# Settings-table keys (install-level identity). Defined next to the accessors'
# owner so consumers (vms.manager, ssh_config) share one spelling
# without a layering inversion. (A retired key may linger as an orphan
# row in existing DBs, harmless; the settings table is a plain KV.)
SYSTEM_SLUG_KEY = "system_slug"


# -- Row types -------------------------------------------------------------


@dataclass
class VMRow:
    name: str
    # The vm-site the VM was created at (the resource name; resolved to
    # a bound platform via agentworks.vms.sites).
    site: str
    template: str | None
    # The admin-template the VM's admin user was provisioned from (the
    # resource name). NULL means the reserved ``default`` admin-template.
    admin_template: str | None
    extra_packages: list[str]
    provisioning_status: str
    init_status: str
    tailscale_host: str | None
    cpus: int | None
    memory_gib: int | None
    disk_gib: int | None
    swap_gib: int | None
    admin_username: str
    # The VM's OS-level hostname, recorded at create time so later
    # reads (SSH config, prompts) never re-derive it from live config.
    hostname: str
    created_at: str
    last_seen_at: str | None
    # Opaque per-platform identifiers (JSON in the column); the owning
    # platform is the only reader (azure resource_id, wsl2 distro_name,
    # proxmox vmid/node, lima instance_name).
    platform_metadata: dict[str, str] = field(default_factory=dict)
    # Operator intent flag: the operator explicitly stopped this VM, so
    # the activation gate's auto-start must not restart it.
    operator_stopped: bool = False


@dataclass
class VMEventRow:
    id: int
    vm_name: str
    event: str
    detail: str | None
    created_at: str


@dataclass
class WorkspaceRow:
    name: str
    vm_name: str
    template: str | None
    workspace_path: str
    created_at: str
    # Linux group on the VM. Set at create time so legacy workspaces
    # (created when the prefix was "ws--") keep their existing group even
    # after the prefix changed to "ws-".
    linux_group: str


@dataclass
class AgentRow:
    name: str
    vm_name: str
    linux_user: str
    template: str | None
    grant_all: bool
    created_at: str


@dataclass
class AgentGrantRow:
    agent_name: str
    workspace_name: str
    grant_type: str  # 'explicit' or 'implicit'
    session_name: str | None  # NULL for explicit, session name for implicit
    created_at: str


@dataclass
class SessionRow:
    name: str
    workspace_name: str
    template: str
    mode: str
    created_at: str
    updated_at: str
    agent_name: str | None = None
    created_workspace: bool = False
    created_agent: bool = False
    socket_path: str | None = None
    pid: int | None = None
    boot_id: str | None = None
    # The session harness's per-session state blob (harness-owned and
    # OPAQUE to the core: JSON object stored as TEXT). The harness reads
    # and mutates it during its ops; the session manager persists it back
    # after the op. Empty for a harness that keeps no state (``shell``);
    # ``claude-code`` stores its minted Claude session id here.
    harness_state: dict[str, object] = field(default_factory=dict)


class ShellEntry(TypedDict):
    """One shell pane in a console window. cwd None = workspace root."""

    cwd: str | None
    admin: bool


@dataclass
class ConsoleRow:
    name: str
    vm_name: str
    admin_shell: bool
    created_at: str
    updated_at: str


@dataclass
class ConsoleSessionRow:
    console_name: str
    session_name: str
    position: int
    shells: list[ShellEntry]
