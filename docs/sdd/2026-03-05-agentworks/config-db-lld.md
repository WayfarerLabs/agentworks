# Agentworks -- Configuration and Database Schema LLD

**Status:** Active **Parent:** [plan.md](plan.md) -- 1.2

---

## Configuration Schema

User config lives at `~/.config/agentworks/config.toml`. All paths support `~` expansion. The config
is read-only at runtime -- Agentworks never writes back to it.

Setting `AW_CONFIG_DIR` in the environment relocates the whole tree (config.toml, DB, backups, logs,
resource manifests). Intended for running multiple agentworks installs side-by-side under one user
without sharing DB migration state; not a production knob. Per k8s convention, env vars only answer
"where is the config" here; individual values inside the config are not env-overridable.

### Full Schema

```toml
# --- User identity and preferences ---

[user]
ssh_public_key = "~/.ssh/id_ed25519.pub"    # required -- used to authorize access to VMs
ssh_private_key = "~/.ssh/id_ed25519"        # required -- used for SSH connections to VMs
shell = "zsh"                                # optional -- default shell on VMs (default: "zsh")

# --- Paths ---

[paths]
local_workspaces = "~/workspaces"            # optional -- local workspace directory (default: "~/workspaces")
code_workspaces = "~/agentworks-workspaces"  # optional -- .code-workspace file directory
                                             #   (default: "~/agentworks-workspaces")

# --- Default values for CLI flags ---

[defaults]
platform = "lima"                            # optional -- default VM platform (lima|azure|wsl2)
vm_host = "mac-studio"                       # optional -- default VM host for Lima
                                             #   omit for local Lima; ignored for azure/wsl2
git_hosts = ["azdo", "github"]               # optional -- git hosts to register on vm create
                                             #   falls back to all configured git hosts if omitted

# --- Dotfiles ---

[dotfiles]
enabled = true                               # optional -- set to false to skip dotfiles (default: true)
source = "~/.dotfiles"                       # optional -- path to dotfiles dir (default: "~/.dotfiles")
install_cmd = "./install.sh"                 # optional -- command to run after copy
                                             #   auto-detected as ./install.sh if omitted

# --- VM configuration ---
# Agentworks always installs its own system dependencies first (openssh-server,
# curl, git, sudo, ca-certificates, etc.) regardless of this config.

[vm.config]
username = "agentworks"                      # optional -- VM user account name (default: "agentworks")
cpus = 4                                     # optional -- number of vCPUs (default: 4)
memory = "8GiB"                              # optional -- memory size (default: "8GiB")
disk = "50GiB"                               # optional -- disk size (default: "50GiB")
apt = [                                      # optional -- additional apt packages to install on all VMs
    "zsh",
    "git",
    "tmux",
    "tmuxinator",
    "curl",
    "unzip",
    "build-essential",
    "ripgrep",
    "fzf",
    "fd-find",
]
snap = []                                    # optional -- snap packages to install on all VMs
install_commands = [                         # optional -- shell commands run in order on the VM
    "sh -c \"$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)\"",
    "curl -fsSL https://bun.sh/install | bash",
    "curl -fsSL https://claude.ai/install.sh | bash",
]

# --- Workspace templates ---
# The "default" workspace template is used when --template is not specified.
# If no "default" workspace template exists, an empty workspace with tmuxinator config is created.

[workspace_templates.default]
# No repo -- empty workspace
# tmuxinator defaults to true if omitted

[workspace_templates.gruntweave]
repo = "git@ssh.dev.azure.com:v3/org/project/root-workspace"

[workspace_templates.agentic]
inherits = ["gruntweave"]                    # inherits gruntweave's config, overrides below
repo = "git@github.com:org/agentic-workspace.git"
tmuxinator = false                           # disable tmuxinator for this template

# Future: workspace templates will support a "files" section for copying
# files into the workspace with variable substitution (VS Code settings,
# Claude Code permissions, etc.)

# --- Git host providers ---
# Each entry under [git_hosts.*] defines a provider.
# The key (e.g. "azdo", "github") is the name used in defaults.git_hosts and --git-hosts.

[git_hosts.azdo]
type = "azdo"                                # required -- provider type
org = "my-org"                               # required for azdo -- AzDO organization name

[git_hosts.github]
type = "github"                              # required -- provider type
# Authentication via gh auth token or GITHUB_TOKEN env var

# --- Azure-specific settings (only needed for azure platform) ---

[azure]
subscription_id = "..."                      # required for azure -- Azure subscription
resource_group = "agentworks-vms"            # required for azure -- resource group for VMs
region = "eastus2"                           # required for azure -- Azure region
idle_timeout_hours = 2                       # optional -- hours before auto-suspend (default: 2)
```

### Validation Rules

The config is validated at load time. Errors are reported with the TOML key path for easy debugging.

- `user.ssh_public_key` and `user.ssh_private_key` must exist after path expansion
- `defaults.platform` must be one of: `lima`, `azure`, `wsl2`
- `defaults.vm_host` must reference an existing `vm_hosts` ID in the state database (validated at
  use time, not config load time, since VM hosts are added dynamically)
- `defaults.git_hosts` entries must reference keys under `[git_hosts.*]`
- `git_hosts.*.type` must be one of: `azdo`, `github`
- `git_hosts.azdo` requires `org`
- `paths.local_workspaces` and `paths.code_workspaces` must be valid paths after expansion (defaults
  used if omitted)
- `azure.*` fields are only required if `defaults.platform = "azure"` or if `--platform azure` is
  used
- `vm.config.install_commands` entries are not validated at config load time -- failures are
  reported during VM init
- `workspace_templates.*.repo` is not validated at config load time -- git clone failures are
  reported at workspace creation
- `workspace_templates.*.inherits` entries must reference other keys under `[workspace_templates.*]`
- `workspace_templates.*.inherits` must not form cycles (validated at config load time)
- `workspace_templates.*.tmuxinator` must be a boolean if present (default: `true`)

### Config Loading

1. Read `~/.config/agentworks/config.toml` using `tomllib` (stdlib, Python 3.11+)
2. Expand `~` in all path fields
3. Validate required fields and cross-references
4. Return a typed config object (dataclass or similar)

If the config file does not exist, Agentworks exits with a message pointing the user to create one.
There is no implicit default config -- the user must create the file.

---

## Database Schema

Runtime state lives at `~/.config/agentworks/agentworks.db` (SQLite). The database is created
automatically on first use. Schema migrations are handled via a simple version table.

### Entity Relationships

```text
vm_hosts 1──* vms 1──* workspaces
                |
                ├──* vm_git_host_keys
                └──* vm_events

(workspaces with vm_name = NULL are local workspaces)
```

### Tables

#### schema_version

Tracks the current database schema version for migrations.

```sql
CREATE TABLE schema_version (
    version   INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

#### vm_hosts

Machines that can host VMs. Only relevant for Lima (remote) -- Azure and WSL2 do not have a VM host
layer.

```sql
CREATE TABLE vm_hosts (
    name         TEXT PRIMARY KEY,              -- user-provided name (e.g. "mac-studio")
    ssh_host     TEXT NOT NULL,                 -- SSH address (e.g. "192.168.1.10")
    platform     TEXT NOT NULL DEFAULT 'lima',  -- VM platform (currently always "lima" -- CLI rejects others)
    os           TEXT,                          -- auto-detected OS (e.g. "darwin", "linux")
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_seen_at TEXT                           -- updated on successful SSH connection
);
```

#### vms

Agentworks-managed virtual machines.

```sql
CREATE TABLE vms (
    name                TEXT PRIMARY KEY,              -- user-provided or auto-generated name
    platform            TEXT NOT NULL,                 -- "lima", "azure", "wsl2"
    vm_host_name        TEXT,                          -- FK to vm_hosts (NULL for azure, wsl2)
    vm_user             TEXT NOT NULL DEFAULT 'agentworks', -- VM user account name
    cpus                INTEGER,                       -- number of vCPUs (from config or --cpus)
    memory              TEXT,                          -- memory size (from config or --memory)
    disk                TEXT,                          -- disk size (from config or --disk)
    extra_packages      TEXT,                          -- JSON array of extra apt packages from --extra-packages
    provisioning_status TEXT NOT NULL DEFAULT 'pending', -- see VM Status Model below
    init_status         TEXT NOT NULL DEFAULT 'pending', -- see VM Status Model below
    tailscale_host      TEXT,                          -- Tailscale hostname/IP (set after provisioning, nullable for rejoin)
    azure_resource_id   TEXT,                          -- Azure resource ID (azure platform only)
    wsl_distro_name     TEXT,                          -- WSL2 distro name (wsl2 platform only)
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_seen_at        TEXT,                          -- updated on successful SSH connection

    FOREIGN KEY (vm_host_name) REFERENCES vm_hosts(name)
);
```

#### workspaces

Ephemeral working contexts. May live on a VM or locally.

```sql
CREATE TABLE workspaces (
    name           TEXT PRIMARY KEY,              -- user-provided or auto-generated name
    type           TEXT NOT NULL,                 -- "vm" or "local"
    vm_name        TEXT,                          -- FK to vms (NULL for local workspaces)
    template       TEXT,                          -- workspace template name used at creation
    workspace_path TEXT NOT NULL,                 -- path to workspace directory on its host
                                                  --   VM workspaces: remote path on the VM (e.g. /home/agentworks/workspaces/ws-123)
                                                  --   local workspaces: local path on the User Workstation
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_seen_at   TEXT,                          -- updated on workspace shell access

    FOREIGN KEY (vm_name) REFERENCES vms(name)
);
```

#### vm_git_host_keys

Tracks SSH keys registered with git host providers during VM initialization. Enables clean removal
on `vm delete`. The selected git hosts for a VM are not stored separately -- they are derived from
this table (which providers have keys registered for a given VM).

```sql
CREATE TABLE vm_git_host_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_name       TEXT NOT NULL,                  -- FK to vms
    git_host_name TEXT NOT NULL,                  -- key in config (e.g. "azdo", "github")
    remote_key_id TEXT NOT NULL,                  -- ID returned by the provider's API
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    FOREIGN KEY (vm_name) REFERENCES vms(name),
    UNIQUE (vm_name, git_host_name)              -- one key per provider per VM
);
```

#### vm_events

Append-only lifecycle event log. Records every significant state transition and operation for
diagnostics and debugging. Events are never updated or deleted -- the table is insert-only. This
provides a full audit trail without cluttering the main status columns on the `vms` table.

```sql
CREATE TABLE vm_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_name    TEXT NOT NULL,                  -- FK to vms
    event      TEXT NOT NULL,                  -- event type (see below)
    detail     TEXT,                           -- optional human-readable detail or error message
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    FOREIGN KEY (vm_name) REFERENCES vms(name)
);

CREATE INDEX idx_vm_events_vm_name ON vm_events(vm_name);
```

Event types:

| Event                   | Detail                                              |
| ----------------------- | --------------------------------------------------- |
| `provisioning_started`  | Platform name                                       |
| `provisioning_complete` | Tailscale address                                   |
| `provisioning_failed`   | Error message                                       |
| `init_started`          | "auto" (after provisioning) or "reinit" (manual)    |
| `init_step_complete`    | Step name (e.g. "apt_install", "dotfiles", "shell") |
| `init_complete`         | _(none)_                                            |
| `init_partial`          | Summary of warnings from non-fatal step failures    |
| `init_failed`           | Error message and step name                         |

The event log is purely diagnostic. No business logic depends on it -- the `provisioning_status` and
`init_status` columns on `vms` are the source of truth for current state. The event log answers
"what happened and when?" for debugging failed or slow operations.

### Constraints and Invariants

- Names are globally unique within each table -- vm_hosts, vms, and workspaces are separate
  namespaces (PRIMARY KEY)
- Names must match `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$` with no consecutive hyphens (validated at the
  application level before insert)
- `vm_git_host_keys` enforces one key per provider per VM (UNIQUE constraint)
- `vms.provisioning_status` must be one of: `pending`, `in_progress`, `complete`, `failed`
- `vms.init_status` must be one of: `pending`, `in_progress`, `complete`, `partial`, `failed`
- `workspaces.vm_name` is NULL for local workspaces, NOT NULL for VM workspaces
- `workspaces.type` must be consistent with `vm_name`: type "local" requires vm_name NULL, type "vm"
  requires vm_name NOT NULL
- Foreign keys are enforced (`PRAGMA foreign_keys = ON`)
- All timestamps are UTC ISO 8601 strings

### last_seen_at Semantics

The `last_seen_at` column is updated on successful interaction:

- **vm_hosts**: updated when an SSH command executes successfully against the host
- **vms**: updated when an SSH command executes successfully against the VM (including workspace
  operations)
- **workspaces**: updated when `workspace shell` is used to access the workspace

This is best-effort -- it is not a heartbeat. It provides a rough signal for "when did I last touch
this?" to help users identify stale resources.

### VM Status Model

VMs have three independent status dimensions:

**`provisioning_status` (persisted in DB)** -- tracks whether the VM has been successfully
provisioned. This is a one-time pass/fail gate:

| Status        | Meaning                                                       |
| ------------- | ------------------------------------------------------------- |
| `pending`     | VM record created, provisioning not yet started               |
| `in_progress` | Platform provisioning and bootstrap are running               |
| `complete`    | VM is provisioned, on Tailscale, and ready for initialization |
| `failed`      | Provisioning failed -- VM must be deleted and recreated       |

Provisioning is irreversible. Once `complete`, it never changes. If `failed`, the only recovery is
`vm delete` and recreate.

**`init_status` (persisted in DB)** -- tracks the most recent initialization attempt. Unlike
provisioning, this can be re-run:

| Status        | Meaning                                                                |
| ------------- | ---------------------------------------------------------------------- |
| `pending`     | Not yet initialized (provisioning just completed, or reinit requested) |
| `in_progress` | Initialization is running                                              |
| `complete`    | Fully initialized and ready for workspaces                             |
| `partial`     | Core succeeded but one or more non-fatal steps had warnings            |
| `failed`      | Fatal initialization step failed                                       |

On `vm reinit`, the `init_status` is reset to `pending` and the full initialization sequence runs
again. This means `init_status` reflects the state of the _last_ attempt, not a cumulative history.
The `vm_events` table provides the full history.

VMs in `partial` state are fully usable -- workspace and agent operations work normally. The status
serves as a reminder that something was skipped during init. VMs in `failed` state are unusable from
an Agentworks perspective -- only `vm delete` and `vm reinit` are supported.

**Runtime status (queried live from platform)** -- the current power state of the VM. This is
**never cached** in the database because it can change outside of Agentworks (manual stops, Azure
auto-deallocate, host reboots). Each platform provisioner implements `status(vm_name) -> VMStatus`
which returns one of:

| Status        | Meaning                                               |
| ------------- | ----------------------------------------------------- |
| `running`     | VM is powered on and should be reachable              |
| `stopped`     | VM is powered off (Lima, WSL2)                        |
| `deallocated` | VM is deallocated (Azure-specific, no compute charge) |
| `unknown`     | Status could not be determined                        |

**Command behavior based on status:**

Commands check all three dimensions as appropriate:

- `workspace create`: requires `provisioning_status = "complete"`, `init_status` in (`complete`,
  `partial`), and runtime `running`. If stopped/deallocated, auto-starts and waits. If provisioning
  incomplete, errors with guidance. If init incomplete, suggests `vm reinit`.
- `workspace shell`: same checks as `workspace create`. Auto-starts stopped VMs.
- `vm reinit`: requires `provisioning_status = "complete"`. Re-runs initialization regardless of
  current `init_status`.
- `vm start`: queries platform status, starts if not already running. Works regardless of other
  statuses.
- `vm stop`: queries platform status, stops if running. Works regardless of other statuses.
- `vm delete`: works regardless of all statuses. Cleans up what it can.
- `vm list`: shows `provisioning_status`, `init_status` (from DB), and runtime status (live query)
  in output.

### Migration Strategy

Migrations are applied at database open time. The `schema_version` table tracks the current version.
Each migration is a Python function that runs the necessary SQL. Migrations are forward-only -- no
rollback support. The initial schema is version 1.

```text
1. Open database
2. Create schema_version table if it does not exist
3. Read current version (0 if table is empty)
4. Apply all migrations from current+1 to latest
5. Update schema_version
```

### Database Access Pattern

All database operations go through `db.py`, which provides typed helper functions (not raw SQL in
callers). The database connection is opened once per CLI invocation and closed on exit. WAL mode is
enabled for better concurrent read performance (`PRAGMA journal_mode = WAL`).

Notable methods beyond standard CRUD:

- `clear_vm_tailscale(name)`: sets `tailscale_host = NULL` for a VM, used when an ephemeral
  Tailscale node is lost
- `check_schema(path)`: static method that checks schema version without migrating, used by the
  `doctor` command to report DB status and offer to run pending migrations
