# Agentworks -- Implementation Plan

**Status:** Draft **Branch:** `feat/agentworks-sdd` (specs), implementation branches TBD

---

## Phase 1: VM Workspaces + Core CLI

The initial implementation. Delivers a working CLI that can provision VMs, initialize them, and manage workspaces on
those VMs.

### 1.1 Project Scaffolding

- [ ] Create `agentworks` repo with `cli/` subdirectory for the Python CLI
- [ ] Set up `cli/pyproject.toml` with uv, Python 3.12+ target
- [ ] Set up Typer CLI entrypoint (`cli.py`) with command groups: `vm-host`, `vm`, `workspace`
- [ ] Set up linting/formatting (ruff, mypy)
- [ ] Add basic README

**Definition of done:** `agentworks --help` runs and shows the command groups.

### 1.2 Configuration and State

- [ ] Implement config loader (`config.py`): parse `~/.config/agentworks/config.toml`, validate structure, expand paths
- [ ] Implement state database (`db.py`): SQLite schema creation, migrations, CRUD for `vm_hosts`, `vms`, `workspaces`,
      `vm_git_host_keys`
- [x] Write LLD: [config-db-lld.md](config-db-lld.md)

**Definition of done:** Config loads from a sample TOML file. Database creates tables and round-trips records.

### 1.3 SSH Execution Primitive

- [ ] Implement `ssh.py`: run commands on a remote host via native `ssh` subprocess
- [ ] Support: single command execution, file copy (scp/rsync), interactive shell
- [ ] Respect user's SSH config and agent -- no custom key handling beyond what SSH provides

**Definition of done:** Can execute a command on a remote host and retrieve output. Can copy a file to a remote host.

### 1.4 VM Host Management

- [ ] Implement `vm-host add`, `vm-host list`, `vm-host remove` commands
- [ ] OS auto-detection on first connect (via SSH)
- [ ] Persist to state database

**Definition of done:** Can add a VM host, list it, remove it. OS is detected and stored.

### 1.5 VM Provisioning -- Platform Provisioners

- [x] Write LLD: [vm-provisioning-lld.md](vm-provisioning-lld.md)
- [ ] Implement Lima provisioner (`provisioners/lima.py`): local and remote VM Host variants
  - [ ] Lima template for Debian VM
  - [ ] Local: run `limactl create` directly
  - [ ] Remote: SSH to VM Host, run `limactl create`
- [ ] Implement Azure provisioner (`provisioners/azure.py`):
  - [ ] `az vm create` with Debian image and cloud-init
  - (Auto-suspend deferred to future phase -- see vm-provisioning-lld.md)
- [ ] Implement WSL2 provisioner (`provisioners/wsl2.py`):
  - [ ] PowerShell subprocess to import Debian distro

**Definition of done:** Each provisioner can create a raw VM on its platform. The VM is SSH-accessible.

### 1.6 VM Initialization

- [ ] Implement `initializer.py`: uniform post-provisioning setup over SSH (tracks `init_status` in DB)
- [ ] Steps (Tailscale-first approach):
  - [ ] Bootstrap (over provisioning transport): ensure user, apt system packages, add SSH key, Tailscale join
  - [ ] Setup (over Tailscale SSH): apt user packages, snap, install commands, shell, SSH keypair, git host keys,
        dotfiles
- [ ] Pre-flight auth verification for selected git host providers (fail-fast)

**Definition of done:** A freshly provisioned VM is fully initialized and reachable over Tailscale. SSH keys are
registered with configured git hosts.

### 1.7 Git Host Providers

- [x] Write LLD: [git-hosts-lld.md](git-hosts-lld.md)
- [ ] Implement `GitHostProvider` base interface (`git_hosts/base.py`)
- [ ] Implement AzDO provider (`git_hosts/azdo.py`): register/remove key via REST API with Azure AD token
- [ ] Implement GitHub provider (`git_hosts/github.py`): register/remove key via REST API with `gh cli` or PAT
- [ ] Track registered keys in `vm_git_host_keys` table for clean removal on `vm delete`

**Definition of done:** SSH keys are registered with AzDO and GitHub during VM init. Keys are removed on `vm delete`.

### 1.8 VM Lifecycle Commands

- [ ] Implement `vm create`: orchestrates platform provisioning + initialization
- [ ] Implement `vm list`: query state database
- [ ] Implement `vm start`, `vm stop`: platform-specific (Lima: `limactl start/stop`, Azure: `az vm start/deallocate`,
      WSL2: `wsl --terminate/--distribution`)
- [ ] Implement `vm delete`: stop VM, remove git host keys, platform-specific cleanup, remove from state database

**Definition of done:** Full VM lifecycle works end-to-end on at least one platform. Can create, list, start, stop,
delete.

### 1.9 Workspace Templates

- [ ] Implement workspace template resolution (`templates.py`): `--template` flag, fall back to `default`, fall back to
      built-in empty
- [ ] Implement workspace template inheritance: depth-first resolution, merge rules (booleans last-one-wins, lists
      append with dedup), cycle detection
- [ ] Workspace template processing: create directory, optional git clone
- [ ] Conditional tmuxinator config generation and symlink (based on resolved `tmuxinator` field)

**Definition of done:** Workspace creation with and without a repo workspace template works. Inheritance resolves
correctly. Tmuxinator config is generated only when enabled.

### 1.10 Workspace Lifecycle (VM)

- [ ] Implement `workspace create` for VM workspaces: remote steps over SSH + local `.code-workspace` generation
- [ ] Implement `workspace shell`: SSH into VM with working directory set to workspace root. Auto-start deallocated
      Azure VMs (poll SSH readiness with timeout). Tmuxinator integration: if enabled, run
      `tmuxinator start     <workspace-name>`; support `--no-tmuxinator` flag.
- [ ] Implement `workspace list`: query state database, display type/VM/template/timestamp
- [ ] Implement `workspace delete`: confirmation prompt, remote cleanup, local cleanup, state database removal
- [ ] Implement `--open-vscode` flag on create

**Definition of done:** Can create a workspace on a VM, shell into it, list it, and delete it. VS Code workspace file is
generated and opens correctly.

### 1.11 End-to-End Testing

- [ ] Manual end-to-end test on Lima (local)
- [ ] Manual end-to-end test on Azure
- [ ] Manual end-to-end test on WSL2 (if Windows host available)
- [ ] Document known issues and gaps

**Definition of done:** Full workflow (vm create, workspace create, workspace shell, workspace delete, vm delete) works
on at least two platforms.

---

## Phase 2: Local Workspaces

Adds local workspaces that bypass the VM layer entirely.

### 2.1 Local Workspace Backend

- [ ] Implement `LocalWorkspaceBackend` (`workspaces/backends/local.py`):
  - [ ] Create directory under configurable local path (default: `~/workspaces/`)
  - [ ] Apply workspace template (optional git clone, tmuxinator config)
  - [ ] Shell access: open a new shell in the workspace directory
  - [ ] Delete: remove workspace directory and tmuxinator symlink
- [ ] Wire `--local` flag into `workspace create`
- [ ] Generate local-path `.code-workspace` files (no SSH Remote)

**Definition of done:** Can create, shell into, list, and delete local workspaces. They appear alongside VM workspaces
in `workspace list`.

### 2.2 Unified Workspace Listing

- [ ] `workspace list` shows both VM and local workspaces with type indicator
- [ ] `workspace list --local` filters to local only
- [ ] `workspace list --vm <name>` filters to specific VM

**Definition of done:** `workspace list` shows all workspaces regardless of type.

---

## Phase 3: File Templating

Adds file templating support to workspace templates.

### 3.1 Workspace Template File Processing

- [ ] Write LLD for workspace template file processing (templating language choice, variable model, processing order)
- [ ] Implement `files` section in workspace templates: copy files into workspace with variable substitution
- [ ] Define standard variables (workspace name, VM name, workspace template name, etc.)
- [ ] Use cases: VS Code settings, Claude Code permissions, editor configs, etc.

**Definition of done:** Workspace templates with a `files` section copy and process files into new workspaces with
variable substitution.

---

## Future (Not Planned)

These items have architectural room in the current design but are not scheduled for implementation.

- **VM templates**: named VM configurations (packages, install commands, shell) that can be selected at `vm create`
  time, replacing the current single implicit default in `[vm.config]`
- **VM initialization plugins**: structured, reusable initialization steps (built-in and user-provided) that replace raw
  `install_commands` with declarative, version-aware building blocks (e.g. `install.bun` installs bun and writes
  `.bun-version`)
- **Non-VM Workspace Hosts**: Kubernetes StatefulSet pods as Workspace Hosts (`--platform k8s`), and/or container-based
  workspaces on existing VMs. When non-VM types ship, the `vm` CLI command group may be generalized to `host` or
  similar.
- **Workspace move**: `workspace move <name> --to <vm-name|local>` to relocate workspaces between VMs/local
- **Azure auto-suspend**: systemd timer on Azure VMs that deallocates after idle timeout (requires `az cli` auth on the
  VM -- authentication mechanism TBD)
- **Auto-authentication**: auto-authenticate tools (az cli, Claude Code, etc.) during VM initialization
- **Non-interactive Tailscale join**: avoid interactive prompt for Tailscale auth key
