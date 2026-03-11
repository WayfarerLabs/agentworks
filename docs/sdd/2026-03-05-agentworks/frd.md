# Agentworks -- Functional Requirements Document

**Status:** Active **Repo:** `agentworks` (in upstreams) **Path:** `cli/`

---

## Overview

Agentworks is a CLI tool for provisioning and managing ephemeral agent workspaces. It abstracts over
platform-specific primitives (Lima VMs, Azure VMs, WSL2, local directories) to provide a uniform
workspace lifecycle -- create, use, destroy -- regardless of where the underlying compute lives.

The primary use case is a developer spinning up a fresh, isolated workspace for a single task or
agentic session, with their full toolchain and personal environment already in place.

---

## Terminology

- **User Workstation**: the developer's personal working machine -- where VS Code and the Agentworks
  CLI run
- **Workspace Host**: the abstract concept of an environment that can host workspaces. In the
  current implementation, this is always a VM, but the concept generalizes to K8s pods, containers,
  or the local machine. The CLI and database use `vm` terminology today; this may be generalized
  when non-VM Workspace Host types are implemented.
- **VM Host**: a machine capable of running VMs (e.g. a Mac Studio running Lima). May or may not
  exist depending on platform -- Azure and WSL2 have no separate VM Host layer
- **VM**: a long-lived personal Linux VM (Debian), provisioned by Agentworks and accessed directly
  by the User Workstation over Tailscale after provisioning. The initial (and currently only)
  Workspace Host type.
- **Workspace**: an ephemeral working context -- created per task or agentic session. May live on a
  Workspace Host (VM, future K8s pod) or directly on the User Workstation (local workspace)
- **Workspace Template**: a named configuration that defines what a workspace looks like at creation
  time -- which repo to clone (if any), which files to copy/template, and which settings to inject
- **Agent**: an isolated Linux user within a workspace, representing a single AI coding agent.
  Agents belong strictly to a workspace, have their own home directory, and access the workspace
  through Linux group membership. See the user-based security SDD (2026-03-08) for the full security
  model.
- **Git Credential Provider**: a service where personal access tokens can be configured for git
  access over HTTPS (AzDO, GitHub, etc.)

### Naming Conventions

All user-provided names (VM hosts, VMs, workspaces, agents) follow the same rules:

- **Character set**: lowercase alphanumeric, hyphens, and underscores: `[a-z0-9_-]`
- **Structure**: must start and end with `[a-z0-9]`. Interior characters may include hyphens and
  underscores. Single-character names (`[a-z0-9]`) are allowed.
- **No double hyphens**: consecutive hyphens (`--`) are reserved as the agent username separator
  (see Agents below) and are not allowed in any name.
- **Uniqueness**: globally unique within each entity type (vm_hosts, vms, workspaces are separate
  namespaces). Workspace names are globally unique, not per-VM -- this simplifies the CLI (no need
  to qualify `workspace shell ws-123 --vm dev-vm`) and avoids ambiguity in `.code-workspace` file
  naming. Agent names are unique within their workspace.
- **`--name` flag**: all create commands accept an optional `--name` flag. If not specified, the
  user is prompted with a random 7-character default (lowercase alphanumeric only: `[a-z0-9]`). The
  user can accept the default or type a custom name. If the generated random name collides with an
  existing entity, a new random name is generated (retry up to 5 times before failing).
- **Positional references**: subsequent commands reference entities by name as a positional argument
  (e.g. `vm start dev-vm`, `workspace shell ws-123`)

---

## Goals

- Uniform workspace lifecycle across all supported platforms (VM-based and local)
- Simple VM provisioning and initialization per platform
- Personal environment (dotfiles, shell, packages) stamped into VMs once at init time -- inherited
  by all workspaces naturally
- Workspaces are lightweight: a directory, an optional repo clone, a tmuxinator config, and a local
  `.code-workspace` file on the User Workstation
- VMs join a user-managed Tailscale network so the User Workstation can reach them directly
  regardless of where they live. Ephemeral Tailscale nodes are supported -- Agentworks detects node
  loss on stop and re-joins on start
- Azure VMs will support auto-suspend after idle to minimize cost (future enhancement -- see
  Phasing)
- Git credential provider agnostic -- PATs can be configured for AzDO, GitHub, or other providers
  via `git credential-store`
- Designed for a single developer first; extensible to a team

## Non-Goals

- Not a general-purpose VM manager (no snapshots, migration, etc.)
- Not a CI/CD runner (see Gruntweave)
- Not responsible for project-level setup -- that is handled by `workspace-initialize.sh` or
  equivalent
- No GUI
- Does not own or manage VM Host connectivity -- Agentworks only requires a reachable SSH host

---

## Workspace Types

Agentworks supports multiple workspace types that share the same lifecycle commands and workspace
identity model. The workspace type determines where the workspace lives and how Agentworks reaches
it.

### VM Workspaces (Phase 1)

Workspaces that live on a provisioned VM. All operations (create, ssh, delete) go over SSH to the
VM. This is the primary workspace type and the initial implementation target.

### Local Workspaces (Phase 2)

Workspaces that live directly on the User Workstation. These bypass the VM Host and VM layers
entirely. The user is responsible for ensuring the right tools are available on their machine. Local
workspaces are first-class citizens -- they appear in `workspace list`, support `workspace shell`
(which opens a shell in the workspace directory), generate `.code-workspace` files, and are tracked
in the state database.

Local workspaces are only supported on Unix-like hosts (macOS, Linux). The workspace directory lives
under a configurable local path (default: `~/workspaces/`).

Local workspaces do not support agents. The agent model requires Linux user management and process
isolation that is only available on Agentworks-managed VMs. Local workspaces remain useful for
non-agentic developer work.

### Containerized Workspaces (Future)

Workspaces that run inside a container on a VM or locally. The container provides isolation and
reproducibility without requiring a full VM. This is a future extension -- the CLI surface and
workspace identity model should accommodate it, but implementation is deferred.

---

## Why VMs and Not Just Containers?

Agentworks starts with VMs because agentic workloads frequently need to run containers themselves --
including full Kubernetes clusters via Kind for local dev and testing. This requires a real kernel,
real networking stack, and access to container runtimes (Podman). Containers-in-containers (DinD)
requires `--privileged` mode and involves significant complexity and security exposure. VMs provide
clean isolation with none of those tradeoffs, making them the right default for heavy workloads.

That said, not every workspace needs a full VM. Containerized workspaces are on the roadmap (see
Phasing) to provide a lighter-weight option for tasks that do not need nested container support. The
goal is full flexibility: VMs when you need a complete environment, containers when you need
something faster and more disposable, local workspaces when you just need a directory.

---

## Workspace Templates

A workspace template defines the initial contents and configuration of a new workspace. Templates
are referenced by name at workspace creation time.

### Template Configuration

Templates are defined in the user config under `[workspace_templates.<name>]`:

```toml
[workspace_templates.default]
# No repo -- just an empty directory with tmuxinator config

[workspace_templates.gruntweave]
repo = "https://dev.azure.com/org/project/_git/root-workspace"

[workspace_templates.agentic]
inherits = ["gruntweave"]
repo = "https://github.com/org/agentic-workspace.git"
tmuxinator = false
```

### Template Fields

- **`repo`**: optional git URL. If set, the repo is cloned directly into the workspace directory
  (i.e. `git clone <repo> <workspace-dir>` -- the repo contents are at the root, not in a
  subdirectory). If omitted, an empty directory is created.
- **`tmuxinator`**: optional boolean (default: `true`). If true, a tmuxinator session config is
  generated and tmux is expected to be available. If false, no tmuxinator config is created.
- **`inherits`**: optional list of parent template names. See Template Inheritance below.
- **File templating (future)**: templates will support a `files` section that copies files into the
  workspace with variable substitution. This is the mechanism for injecting any per-workspace files
  -- VS Code settings, Claude Code permissions, editor configs, etc. The templating language is TBD
  -- this is architectural room, not a Phase 1 requirement.

### Template Inheritance

A template can inherit from one or more parent templates via the `inherits` field. Resolution walks
parents in order (left to right), then applies the child. The merge rules are:

- **Booleans** (`tmuxinator`): last-one-wins. The child's value overrides all parents. If the child
  does not set the field, the last parent to set it wins.
- **Strings** (`repo`): last-one-wins. Same semantics as booleans.
- **Lists** (future `files`): appended in order with deduplication. Parents first, then child. If a
  value already appeared in an earlier parent, it is skipped.

Inheritance is resolved at workspace creation time. Cycles are detected and rejected at config load
time (not deferred to workspace creation). The resolution is depth-first -- if a parent itself
inherits, those ancestors are resolved first.

Example: if `agentic` inherits from `gruntweave`, and `gruntweave` has `tmuxinator = true`, but
`agentic` sets `tmuxinator = false`, the resolved value for `agentic` is `false`.

### Default Template

If `--template` is not specified at workspace creation, the `default` workspace template is used. If
no `default` workspace template is configured, an empty workspace is created with
`tmuxinator = true`.

---

## Configuration

User-editable config lives in `~/.config/agentworks/config.toml`. Runtime state (VMs, workspaces) is
stored in a SQLite database at `~/.config/agentworks/agentworks.db`.

### User Config

`~/.config/agentworks/config.toml`

```toml
[user]
ssh_public_key = "~/.ssh/id_ed25519.pub"
ssh_private_key = "~/.ssh/id_ed25519"
shell = "zsh"

[paths]
local_workspaces = "~/workspaces"               # local workspace directory
code_workspaces = "~/agentworks-workspaces"      # .code-workspace file directory

[defaults]
platform = "lima"
vm_host = "mac-studio"           # optional -- default VM host for Lima (omit for local Lima)
git_credentials = ["azdo", "github"]   # which git credentials to configure on vm create

[dotfiles]
enabled = true                 # set to false to skip dotfiles entirely
source = "~/.dotfiles"        # path to dotfiles directory (default: ~/.dotfiles)
install_cmd = "./install.sh"   # run after copy if present; auto-detected if omitted

[vm.config]
username = "agentworks"   # optional -- VM user account name (default: "agentworks")
cpus = 4                  # optional -- number of vCPUs (default: 4)
memory = "8GiB"           # optional -- memory size (default: "8GiB")
disk = "50GiB"            # optional -- disk size (default: "50GiB")
apt = [
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
snap = []
install_commands = ["ohmyzsh", "bun", "claude-code"]

[install_commands.ohmyzsh]
command = "sh -c \"$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)\""

[install_commands.bun]
command = "curl -fsSL https://bun.sh/install | bash"
path = "~/.bun/bin"

[install_commands.claude-code]
command = "npm install -g @anthropic-ai/claude-code"

[workspace_templates.default]
# Empty workspace with tmuxinator config

[workspace_templates.gruntweave]
repo = "https://dev.azure.com/org/project/_git/root-workspace"

[git_credentials.azdo]
type = "azdo"
org = "my-org"
# Token prompted or read from GIT_CREDENTIALS_AZDO env var

[git_credentials.github]
type = "github"
# Token prompted or read from GIT_CREDENTIALS_GITHUB env var

[azure]
subscription_id = "..."
resource_group = "agentworks-vms"
region = "eastus2"
idle_timeout_hours = 2
```

The `[vm.config]` nesting under `vm` is intentional -- it leaves room for future `[vm.*]` sections
(e.g. named VM templates). See "Future: VM Templates" in Phasing.

Per-VM package additions are specified at VM creation time via `--extra-packages`. The configured
package list is the floor -- it cannot be reduced per-VM, only extended. The extra packages and
other creation arguments are persisted in the state database for reference and potential
re-initialization.

---

## VM Hosts

Agentworks does not provision or manage VM Hosts. It only requires a reachable SSH address and the
VM platform. The platform is currently always Lima (the only platform that uses VM Hosts) --
`vm-host add` rejects other platform values. OS is auto-detected on first connect if not specified.

```shell
agentworks vm-host add --name mac-studio --ssh-host 192.168.1.10 [--platform lima]
agentworks vm-host list
agentworks vm-host remove <name>
```

`vm-host remove` refuses to remove a host that has VMs referencing it. The user must delete those
VMs first (or use `--force` to skip the check and remove the host record, leaving the VMs orphaned).

### Platform and VM Host Interaction

VM hosts are only used by the Lima platform. Azure and WSL2 provision directly from the User
Workstation and have no VM host layer.

- **Lima**: VM host is optional. If a VM host is provided (via `--vm-host` or `defaults.vm_host`),
  Lima runs remotely on that host. If no VM host is resolved, Lima runs locally on the User
  Workstation.
- **Azure, WSL2**: VM host is not supported. If `--vm-host` is explicitly passed on the CLI,
  Agentworks errors. A configured `defaults.vm_host` is silently ignored for these platforms.

VM host resolution order for Lima:

1. `--vm-host <name>` CLI argument
2. `defaults.vm_host` from config
3. Neither present: local Lima

---

## VM Provisioning

For Agentworks-managed VMs, the provisioning process occurs in two phases: **platform provisioning**
(platform-specific) and **VM initialization** (uniform).

### Phase 1: Platform Provisioning

During this phase, Agentworks creates a new VM on the specified platform. This varies based on the
platform:

#### Lima (remote VM Host)

- SSH into VM Host
- Run `limactl create`

#### Lima (local)

- Run `limactl create` locally

#### Azure

- Run `az vm create` with a Debian image and cloud-init userdata

#### WSL2

Agentworks runs natively on Windows (not from within WSL2) for this platform, so this is simply:

- Create a new Debian distro named after the VM name

### Phase 2: VM Initialization

After provisioning, Agentworks pulls a shell on the new VM and runs uniform initialization steps to
prepare it for workspaces. All Agentworks-managed VMs use a configurable user account (default:
`agentworks`, configurable via `vm.config.username`). All VMs are Debian-based, so initialization is
a uniform process regardless of platform. Only the architecture (amd64 vs arm64) may differ, which
should be handled automatically by the underlying tools.

**Secrets are collected upfront** before provisioning begins. The Tailscale auth key and git
credential tokens are prompted (or read from `TAILSCALE_AUTH_KEY` and `GIT_CREDENTIALS_<NAME>` env
vars, with a console message when env vars are used). This avoids mid-process prompts.

Initialization uses a **Tailscale-first** approach: the minimum system bootstrap happens over the
provisioning transport (which may be indirect -- e.g. proxied through a VM Host for Lima remote, or
`wsl` exec for WSL2), then Tailscale is set up to provide direct SSH access from the User
Workstation for the remainder of initialization. This ensures that operations requiring file
transfer (dotfiles rsync) work uniformly across all platforms.

The steps are:

**Bootstrap (over provisioning transport):**

1. Ensure `agentworks` user exists (idempotent -- Azure cloud-init and WSL2 handle this during
   provisioning, Lima does not)
2. Install Agentworks's own system dependencies via apt (`openssh-server`, `curl`, `git`, `sudo`,
   `ca-certificates`, etc.) -- these are always installed regardless of user config
3. Add user's SSH public key to `~/.ssh/authorized_keys` (enables Tailscale SSH in the next step)
4. Install Tailscale, join user's tailnet (using pre-collected auth key)
5. Read Tailscale IP, update VM record (`tailscale_host`, `init_status = "tailscale_up"`) -- switch
   to Tailscale SSH for remaining steps

**Remaining setup (over Tailscale SSH):**

1. Install user-configured apt packages from `[vm.config]`, merged with any per-VM additions
   specified at create time
2. Install snap packages (if any)
3. Set default shell to user's configured shell (default: `zsh`)
4. Run named install commands under the user's shell (e.g. `zsh -lc '...'`) -- this ensures
   installers write to the correct rc file. Install commands are defined as named sections
   (`[install_commands.*]`) with `command` and `path` fields. `vm.config.install_commands`
   references them by name.
5. Configure PATH: accumulate `path` values from install commands into `~/.agentworks-path.sh`,
   sourced from `~/.profile`
6. Write git credentials to `~/.git-credentials` and configure `git credential-store` globally (see
   Git Credentials below)
7. If dotfiles enabled and `dotfiles.source` (default: `~/.dotfiles`) exists on the User
   Workstation: rsync to VM, run `install_cmd` if present or auto-detect `install.sh`
8. Mark VM `init_status = "complete"`

Agentworks verifies required authentication for the **selected** git credential providers before
beginning provisioning, failing fast with a clear error if any is missing. Providers that are
configured but not selected for this VM creation are not checked.

#### Future VM Initialization Enhancements

In the future we may want to add support for things like:

- Auto-authenticating certain tools (az cli, Claude Code, etc.)

### CLI

```shell
agentworks vm create [--platform lima|azure|wsl2] [--vm-host <name>] [--name <name>] \
  [--extra-packages pkg1,pkg2] [--git-credentials azdo,github] \
  [--cpus N] [--memory SIZE] [--disk SIZE] [--vm-user USER]
agentworks vm reinit <name>
agentworks vm list
agentworks vm shell <name>
agentworks vm start <name>
agentworks vm stop <name>
agentworks vm delete <name>
agentworks vm add-git-credential <vm-name> <credential-name>
```

`vm shell` opens an SSH session to the VM's home directory as the VM user. This is a convenience
command for debugging and inspection -- not for workspace-level work.

### VM Status Model

VMs have three independent status dimensions:

- **`provisioning_status`** (persisted in DB): tracks whether the VM has been successfully
  provisioned -- `pending`, `in_progress`, `complete`, `failed`. Provisioning is one-time and
  irreversible. If `failed`, the only recovery is `vm delete` and recreate.
- **`init_status`** (persisted in DB): tracks the most recent initialization attempt -- `pending`,
  `in_progress`, `complete`, `partial`, `failed`. Unlike provisioning, initialization can be re-run
  via `vm reinit`. VMs in `partial` state are fully usable (non-fatal steps had warnings). VMs in
  `failed` state can be retried via `vm reinit`.
- **Runtime status** (queried live from the platform): the current power state -- `running`,
  `stopped`, `deallocated`, `unknown`. This is **never cached** in the database because it can
  change outside of Agentworks (manual stops, Azure auto-deallocate, host reboots).

Commands check all three dimensions:

- `workspace create` and `workspace shell` require `provisioning_status = "complete"` and
  `init_status` in (`complete`, `partial`). If the VM is stopped, they auto-start and wait. If
  provisioning is incomplete, they error with guidance. If init is incomplete, they suggest
  `vm reinit`.
- `vm reinit` requires `provisioning_status = "complete"`. Re-runs initialization regardless of
  current `init_status`.
- `vm start`, `vm stop`, and `vm delete` work regardless of other statuses.
- `vm list` shows `provisioning_status`, `init_status`, and live runtime status.

`vm delete` refuses to delete a VM that still has workspaces. The user must delete the workspaces
first (or use `--force` to cascade-delete all workspaces on the VM). If the VM is unreachable (e.g.
VM Host is down), database cleanup still proceeds -- only the platform-specific VM cleanup is
skipped with a warning.

---

## Workspaces

All workspace operations on VM workspaces run over SSH directly to the VM over Tailscale. The VM
Host is not involved. Local workspace operations run directly on the User Workstation.

### Workspace Identity

Each workspace has a unique name following the naming conventions above. Uniqueness is enforced at
create time against the state database.

### Workspace Creation

```shell
agentworks workspace create [--vm <name>] [--local] [--name <name>] [--template <name>] [--open-vscode]
```

If `--local` is specified, a local workspace is created. Otherwise, a VM workspace is created. VM
selection when `--vm` is not specified: if exactly one VM exists, it is auto-selected; if multiple
VMs exist, the user is prompted to select one interactively; if no VMs exist, the command errors
with a message to create one first.

**For VM workspaces -- remote steps** (executed over SSH on the VM):

1. Check name uniqueness in state database
2. Create `~/workspaces/<workspace-name>/`
3. If the template specifies a repo: clone it into the workspace directory
4. If `tmuxinator` is enabled: write `.tmuxinator.yml` and symlink to
   `~/.tmuxinator/<workspace-name>.yml`
5. (Future) Apply template file processing if configured

**For VM workspaces -- local steps** (executed on the User Workstation):

1. Generate `<workspace-name>.code-workspace` in a configurable local directory (default:
   `~/agentworks-workspaces/`), pointing at the VM via SSH remote
2. Register workspace in state database
3. Print workspace name, SSH connection string, and workspace path
4. If `--open-vscode`: open the `.code-workspace` file in VS Code

**For local workspaces:**

1. Check name uniqueness in state database
2. Create `<local-workspace-path>/<workspace-name>/`
3. If the template specifies a repo: clone it into the workspace directory
4. If `tmuxinator` is enabled: write `.tmuxinator.yml` and symlink to
   `~/.tmuxinator/<workspace-name>.yml`
5. (Future) Apply template file processing if configured
6. Generate `<workspace-name>.code-workspace` pointing at the local directory
7. Register workspace in state database
8. Print workspace name and workspace path
9. If `--open-vscode`: open the `.code-workspace` file in VS Code

The user then completes the workspace setup via shell (e.g. runs `workspace-initialize.sh` for
Gruntweave, etc.). Agentworks's responsibility is only to create the workspace and provide access to
it.

### Workspace Shell Access

`agentworks workspace shell <name> [--no-tmuxinator]`

For VM workspaces: checks both `init_status` (must be `complete`) and runtime status (see VM Status
Model). If the VM is stopped or deallocated, starts it first and polls SSH connectivity until
reachable (timeout: 5 minutes). If tmuxinator is enabled for the workspace, runs
`tmuxinator start <workspace-name>` which creates a new session or attaches to an existing one. If
tmuxinator is not enabled, opens a plain SSH shell. A `--no-tmuxinator` flag overrides the template
setting and opens a plain shell regardless.

The tmuxinator session includes a "user" window (the default, running as the user account) plus one
window per agent configured to `su - <agent-linux-user>` with the working directory set to the
workspace root. This gives the operator a single entry point to observe and interact with all agents
in the workspace. Windows are regenerated when agents are added or removed.

For local workspaces: opens a new shell in the workspace directory. Same tmuxinator behavior applies
-- if enabled, `tmuxinator start <workspace-name>` is run; `--no-tmuxinator` overrides.

### Workspace Listing

`agentworks workspace list [--vm <name>] [--local]`

Lists all live workspaces with: name, type (vm/local), VM (if applicable), template, created
timestamp.

### Workspace Deletion

`agentworks workspace delete <name>`

1. Prompts for confirmation
2. For VM workspaces -- remote steps (SSH into VM):
   - Removes `~/workspaces/<workspace-name>/`
   - Removes `~/.tmuxinator/<workspace-name>.yml` symlink (if present)
3. For local workspaces:
   - Removes `<local-workspace-path>/<workspace-name>/`
   - Removes `~/.tmuxinator/<workspace-name>.yml` symlink (if present)
4. Local steps (User Workstation, all types):
   - Removes `<workspace-name>.code-workspace`
   - Removes workspace record from state database

### Workspace Move (Future)

`agentworks workspace move <name> --to <vm-name|local>`

Package up the entire workspace directory (using `tar`) and move it to a new location, deleting the
old one once it is securely in place. This is a future enhancement -- the CLI surface should
accommodate it, but implementation is deferred.

---

## Agents

Agents are isolated Linux users within a workspace, each representing a single AI coding agent. They
are the mechanism by which AI tools operate within a workspace with controlled, auditable access.

**Agents are only supported on VM workspaces.** The agent model requires Linux user management
(useradd, group membership, SUID executables) which is only possible on a VM that Agentworks
controls. Local workspaces do not support agents -- `agent create` on a local workspace errors with
guidance. See "Why VMs and Not Just Containers?" for related rationale.

### Agent Identity

Each agent has a unique name within its workspace. The agent's Linux username is derived from the
workspace name using the double-hyphen separator: `<workspace-name>--<agent-name>`. Because
workspace names are globally unique and agent names are unique within a workspace, the Linux
username is guaranteed unique on the VM.

Examples:

- Workspace `my-project`, agent `coder`: Linux user `my-project--coder`
- Workspace `ws-task-123`, agent `reviewer`: Linux user `ws-task-123--reviewer`

### Agent Provisioning

When an agent is created, Agentworks:

1. Creates a Linux user with the derived username
2. Adds the user to the workspace's Linux group (grants access to the workspace directory)
3. Creates the agent's home directory
4. Regenerates the workspace's tmuxinator config to include a window for the new agent

The agent's home directory is separate from the workspace directory. The agent accesses the
workspace through group membership, not by living inside it. This means the agent has a place for
its own config, history, and scratch files while still being able to read and write the shared
workspace.

### Agent Shell Access

`agentworks agent shell <agent-name> --workspace <workspace-name>`

A convenience command that SSHs to the VM as the user account, then runs `su - <agent-linux-user>`
with the working directory set to the workspace root. This gives operators a quick way to inspect
what an agent is doing or debug issues from the agent's perspective. No separate tmuxinator config
is needed -- for a multi-pane agent experience, use `workspace shell` which already includes agent
windows.

If the agent name is globally unique (common when there are few workspaces), `--workspace` can be
omitted and the agent is resolved by scanning all workspaces.

### Agent Lifecycle

- Agents are scoped to a workspace. Deleting a workspace cascades to all its agents (Linux users and
  home directories are removed).
- Agents can be individually deleted without affecting the workspace or other agents.
- Agent creation and deletion regenerate the workspace's tmuxinator config.

### CLI

```shell
agentworks agent create <name> --workspace <workspace-name>
agentworks agent list [--workspace <workspace-name>]
agentworks agent shell <name> [--workspace <workspace-name>]
agentworks agent delete <name> --workspace <workspace-name>
```

`agent list` without `--workspace` lists all agents across all workspaces. With `--workspace`, it
filters to a specific workspace.

### Relationship to Other SDDs

The user-based security SDD (2026-03-08) defines the full Linux security model for agents:
group-based workspace access, tools access via the `aw-tools` group, and credential isolation
(agents access credentials only via nerfed commands, not directly). The nerfed commands SDD
(2026-03-08) defines RBAC-controlled operations that agents can perform through SUID executables,
with rulesync skills that are auto-configured in agent workspaces. This SDD focuses on agent
lifecycle management within Agentworks -- the provisioning and teardown of the Linux users and group
memberships that those SDDs depend on.

---

## Git Credentials

Agentworks configures git authentication on VMs using `git credential-store` with personal access
tokens (PATs). No SSH keys are generated or registered -- all git access uses HTTPS URLs.

Tokens are collected upfront before provisioning begins. Each configured provider is either prompted
interactively or read from a `GIT_CREDENTIALS_<NAME>` environment variable (e.g.
`GIT_CREDENTIALS_AZDO`, `GIT_CREDENTIALS_GITHUB`). When an env var is present, the prompt is skipped
with a console message.

Providers are configured in the user config under `[git_credentials.<name>]`.

### Supported Providers

#### AzDO

Writes credential lines for `dev.azure.com` and `ssh.dev.azure.com` HTTPS endpoints using the
provided PAT.

#### GitHub

Writes a credential line for `github.com` using the provided PAT.

### CLI

New VMs configure the git credentials listed in `defaults.git_credentials`. If that key is not set,
all configured providers are used. The `--git-credentials` flag on `vm create` overrides the default
for that VM:

```shell
agentworks vm create --git-credentials azdo,github
```

To add or rotate a credential on an existing VM:

```shell
agentworks vm add-git-credential <vm-name> <credential-name>
```

---

## Dotfiles

Agentworks handles dotfiles during VM initialization only. If `dotfiles.enabled` is `true` (the
default) and the dotfiles source directory exists on the User Workstation (configurable via
`dotfiles.source`, default: `~/.dotfiles`), it is copied to the VM and `install_cmd` is run
(auto-detected as `./install.sh` if not configured). If the source directory does not exist, the
step is skipped silently. Set `dotfiles.enabled = false` to skip dotfiles entirely.

Agentworks does not know or care where the User Workstation's dotfiles came from. All workspaces on
a VM inherit the dotfiles environment naturally -- this is a VM-level concern, not a workspace-level
concern.

Local workspaces inherit the User Workstation's environment directly -- no dotfiles step is needed.

---

## VS Code Integration

Agentworks generates a `<workspace-name>.code-workspace` file on the User Workstation at workspace
creation time. For VM workspaces, this points at the VM workspace directory via SSH Remote. For
local workspaces, it points at the local directory directly. The `--open-vscode` flag opens this
file in VS Code.

Other VS Code artifacts (`.vscode/settings.json`, `extensions.json`, etc.) are either checked into
the workspace repo or will be injected via template file processing (see Workspace Templates).
Agentworks does not manage these directly.

---

## Top-Level Commands

In addition to the entity-specific command groups (`vm-host`, `vm`, `workspace`), Agentworks
provides:

- **`agentworks init`**: generates a sample `config.toml` at the config path. The sample config is
  shipped in the repo at `cli/agentworks/sample-config.toml` and documents all options with inline
  comments.
- **`agentworks doctor`**: checks environment health -- Python version, required/optional tools,
  Tailscale connectivity, config validation, SSH key accessibility, database schema status (with
  automatic migration), and git credential authentication.
- **`agentworks completion zsh`**: outputs a zsh completion script for shell integration.

---

## Ephemeral Tailscale Nodes

Agentworks supports ephemeral Tailscale auth keys (keys with `?ephemeral=true`). Ephemeral nodes are
automatically removed from the tailnet when the VM goes offline, so Agentworks handles this
gracefully:

- **On stop**: after stopping the VM, Agentworks checks whether the Tailscale node is still
  reachable. If not (ephemeral key), the stored Tailscale host is cleared from the database.
- **On start**: after starting the VM, Agentworks verifies Tailscale connectivity. If the node is
  gone, it re-joins the tailnet via the provisioning transport and prompts for a new auth key (or
  uses `TAILSCALE_AUTH_KEY`).
- **Resilience**: even if the database says the Tailscale host is present, start checks reachability
  before proceeding.

This means ephemeral keys work for disposable VMs and are also resilient across stop/start cycles.

---

## Clone URL Convention

VMs authenticate to git hosts via `git credential-store` with HTTPS tokens. Workspace template repos
should use HTTPS URLs (e.g. `https://dev.azure.com/...`, `https://github.com/...`). SSH URLs will
not work unless the user has separately configured SSH keys on the VM.

---

## Phasing

### Phase 1: VM Workspaces + Core CLI + Local Workspaces

- VM provisioning (Lima, Azure, WSL2)
- VM initialization with git credential configuration (AzDO + GitHub)
- Named install commands with PATH management
- Secrets collected upfront (Tailscale auth key, git credential tokens)
- Workspace create/list/shell/delete on VMs
- Local workspace create/list/shell/delete (originally planned for Phase 2, delivered early)
- Unified workspace listing across VM and local workspaces
- Workspace templates (`[workspace_templates.*]`) with optional repo, tmuxinator toggle, and
  inheritance
- VS Code integration
- State database and user config
- Top-level commands: init, doctor, completion
- Ephemeral Tailscale node handling
- `vm add-git-credential` command

### Phase 3: File Templating

- Workspace template `files` section with variable substitution
- Bootstrap files for agentic tooling (Claude Code permissions, etc.)

### Phase 4: Agents

- Agent create/list/shell/delete on VM workspaces
- Linux user provisioning with workspace group membership
- Tmuxinator integration (user + per-agent windows)
- Name validation tightening (no dots, no double hyphens) across all entity types
- Cascading agent cleanup on workspace delete

### Future: Azure Auto-Suspend

Azure VMs will support auto-suspend after a configurable idle timeout (`idle_timeout_hours`). The
mechanism is a systemd timer on the VM that monitors for active SSH sessions (`who`/`ss`) and
deallocates the VM via `az cli` when idle. This requires `az cli` installed and authenticated on the
VM itself, which adds complexity to the initialization flow. The design is documented in the VM
provisioning LLD but implementation is deferred to a future phase.

### Future: VM Templates

The current `[vm.config]` section (apt, snap, install_commands) is effectively a single implicit VM
template. In the future, this could be formalized into named VM templates with the same inheritance
model as workspace templates, allowing different VMs to be provisioned with different toolchains.

### Future: VM Initialization Plugins

Named install commands (`[install_commands.*]`) partially address the original "VM initialization
plugins" concept by making install steps reusable and named. Full plugins would go further --
providing structured, version-aware building blocks (e.g. `install.bun` installs bun, writes a
`.bun-version` file, and verifies the installation as a single declarative step). Named install
commands are a pragmatic middle ground.

### Future: Agent Install Commands

Agent templates (dependent on nerfed commands) will reference install commands from the same
`[install_commands.*]` pool. This allows defining per-agent tooling requirements declaratively.

**Open question:** should agent templates be constrained by the VM they run on? For example, if an
agent template requires `bun`, should it only be usable on VMs that have `bun` in their install
commands? Should there be explicit compatibility declarations, or is this left to the user?

### Future: Non-VM Workspace Hosts

New Workspace Host types beyond VMs:

- **Kubernetes**: a StatefulSet pod as a Workspace Host (`--platform k8s`). The pod is initialized
  like a VM (packages, dotfiles, git credentials) and hosts workspaces on its persistent volume. The
  K8s cluster serves a similar role to a VM Host (provisioning target). Tailscale connectivity via
  the Tailscale Kubernetes operator (sidecar or per-pod annotation). This maps cleanly to the
  existing `vm` command group -- the pod is a long-lived Workspace Host that happens to be a
  container instead of a VM.
- **Containers on VMs**: lighter-weight workspace isolation using containers running on an existing
  VM. The container provides isolation without a full VM. Same CLI surface: create, shell (exec into
  container), list, delete.

When non-VM Workspace Host types ship, the `vm` CLI command group may be generalized to `host` or
similar. The database schema and config would follow.

**Agent model on K8s/containers**: the current agent model (Linux users within a VM) does not
translate to containers, which typically run as a single user and lack the capabilities needed for
multi-user isolation (useradd, SUID, etc.). On K8s, the agent model would likely use **one pod per
agent** instead of one user per agent, with shared workspace data via a PersistentVolumeClaim and
fsGroup settings. Nerfed command gating would use network policies or a sidecar proxy rather than
SUID executables. This is a fundamentally different isolation primitive and would require its own
design work when K8s support is pursued.
