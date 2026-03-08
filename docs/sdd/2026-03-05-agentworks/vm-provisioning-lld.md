# Agentworks -- VM Provisioning LLD

**Status:** Active **Parent:** [plan.md](plan.md) -- 1.5, 1.6

---

## Overview

VM provisioning is a two-phase process: platform provisioning (create the raw
VM) and VM initialization (configure it for workspaces). This document covers
the platform-specific details for each provisioner and the uniform
initialization flow.

---

## Provisioner Interface

Each platform provisioner implements a common interface:

```text
VMProvisioner
  create(vm_name, config, extra_packages) -> SSHTarget
  start(vm_name) -> void
  stop(vm_name) -> void
  delete(vm_name) -> void
  status(vm_name) -> VMStatus   # running, stopped, deallocated, unknown
  exec_target(vm) -> ExecTarget # provisioning transport for an existing VM
```

The `exec_target()` method returns the provisioning transport for an existing
VM. This is used by the Tailscale rejoin flow when a node is lost (e.g.
ephemeral auth key). Each provisioner returns its native transport:

- **Lima (local)**: `ExecTarget(lima=LimaTarget(vm_name))`
- **Lima (remote)**: `ExecTarget(ssh=SSHTarget(vm_host, user))`
- **Azure**: `ExecTarget(ssh=SSHTarget(public_ip, user))` (queries public IP via
  `az vm show`)
- **WSL2**: `ExecTarget(wsl2=WSL2Target(distro_name, user))`

`SSHTarget` is the connection info needed for the initializer to reach the new
VM (host, user, port, key). For Lima and WSL2, this is available immediately
after `create`. For Azure, the VM must be reachable over Tailscale first -- but
since Tailscale setup happens during initialization, Azure provisioning returns
a temporary SSH target (public IP or bastion) that is replaced with the
Tailscale address after init completes.

`VMStatus` is an enum: `running`, `stopped`, `deallocated` (Azure-specific),
`unknown`. This represents the live runtime status queried from the platform --
it is never cached in the database. See the VM Status Model in config-db-lld.md
for how this interacts with the persisted `init_status`.

---

## Lima Provisioner

Lima creates lightweight Linux VMs on macOS using QEMU. Agentworks supports two
modes: local (Lima runs on the User Workstation) and remote (Lima runs on a
separate VM Host accessed via SSH).

### Lima Template

Agentworks ships a Lima template (`agentworks-debian.yaml`) that defines:

```yaml
# Key fields -- not a complete template
arch: default # inherit host architecture (amd64 or arm64)
images:
  - location: <debian-cloud-image-url>
    arch: x86_64
  - location: <debian-cloud-image-url>
    arch: aarch64
cpus: 4 # sensible default, overridable
memory: 8GiB # sensible default, overridable
disk: 50GiB # sensible default, overridable
ssh:
  localPort: 0 # auto-assign
mountType: virtiofs # best performance on macOS
```

The template uses Debian cloud images. The exact image URL should reference the
latest stable Debian release at provisioning time.

### Local Mode

```text
1. limactl create --name <vm_name> --tty=false agentworks-debian.yaml
2. limactl start <vm_name>
3. Return SSHTarget from limactl show-ssh <vm_name>
```

### Remote Mode

All commands are executed over SSH on the VM Host:

```text
1. scp agentworks-debian.yaml <vm_host>:/tmp/agentworks-debian.yaml
2. ssh <vm_host> limactl create --name <vm_name> --tty=false /tmp/agentworks-debian.yaml
3. ssh <vm_host> limactl start <vm_name>
4. Parse SSH target from: ssh <vm_host> limactl show-ssh <vm_name>
```

The SSH target returned is relative to the VM Host, not the User Workstation.
After Tailscale setup during initialization, the VM becomes directly reachable
from the User Workstation.

### Lifecycle Commands

| Command | Local                                | Remote                                             |
| ------- | ------------------------------------ | -------------------------------------------------- |
| start   | `limactl start <vm_name>`            | `ssh <vm_host> limactl start <vm_name>`            |
| stop    | `limactl stop <vm_name>`             | `ssh <vm_host> limactl stop <vm_name>`             |
| delete  | `limactl delete --force <vm_name>`   | `ssh <vm_host> limactl delete --force <vm_name>`   |
| status  | `limactl list --json` (parse status) | `ssh <vm_host> limactl list --json` (parse status) |

---

## Azure Provisioner

Azure VMs are created via `az cli` and run Debian with cloud-init for initial
bootstrapping.

### VM Creation

```text
az vm create \
  --resource-group <resource_group> \
  --name <vm_name> \
  --image Debian:debian-12:12-gen2:latest \
  --size Standard_D4s_v5 \
  --admin-username agentworks \
  --ssh-key-values <user_ssh_public_key> \
  --custom-data <cloud-init-userdata> \
  --public-ip-sku Standard \
  --nsg-rule SSH \
  --tags owner=agentworks
```

The `--size` should be configurable in the future but uses a sensible default
initially.

### Cloud-Init Userdata

Cloud-init handles minimal bootstrapping that must happen before Agentworks can
SSH in:

```yaml
#cloud-config
package_update: true
packages:
  - openssh-server
users:
  - name: agentworks
    ssh_authorized_keys:
      - <user_ssh_public_key>
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
```

Everything else (apt packages, install commands, dotfiles, Tailscale) is handled
by the Agentworks initializer over SSH, not cloud-init. This keeps cloud-init
minimal and the initialization logic uniform across platforms.

### Auto-Suspend (Future)

Auto-suspend is deferred to a future phase. The design is documented here for
reference but is **not part of the initial implementation**.

The mechanism is a systemd timer on the VM that monitors for activity and
deallocates the VM after the configured idle timeout.

```text
/etc/systemd/system/agentworks-idle-check.service
/etc/systemd/system/agentworks-idle-check.timer    # runs every 15 minutes
/usr/local/bin/agentworks-idle-check.sh
```

The idle check script:

1. Count active SSH sessions via `who` or `ss`
2. If sessions > 0: update a timestamp file (`/var/run/agentworks-last-active`)
3. If sessions == 0 and timestamp is older than `idle_timeout_hours`: deallocate
   via `az vm deallocate --resource-group <rg> --name <vm_name>`

This requires `az cli` installed and authenticated on the VM itself. The
authentication mechanism (managed identity, service principal, or user-delegated
token) is TBD and is the primary reason this is deferred.

### Temporary SSH Target

Azure VMs get a public IP at creation. The initializer uses this for the initial
SSH connection. After Tailscale joins the tailnet, the state database is updated
with the Tailscale address, and subsequent connections use Tailscale. The public
IP can optionally be removed after Tailscale is confirmed working (future
enhancement).

### Lifecycle Commands

| Command | Implementation                                                                                  |
| ------- | ----------------------------------------------------------------------------------------------- |
| start   | `az vm start --resource-group <rg> --name <vm_name>`                                            |
| stop    | `az vm deallocate --resource-group <rg> --name <vm_name>`                                       |
| delete  | `az vm delete --resource-group <rg> --name <vm_name> --yes` + cleanup NSG, NIC, disk, public IP |
| status  | `az vm get-instance-view --resource-group <rg> --name <vm_name>` (parse power state)            |

Azure `stop` uses `deallocate` (not `stop`) to avoid continued billing.

Azure `delete` must clean up associated resources. The `az vm delete` command
does not remove the NIC, disk, NSG, or public IP by default. These must be
explicitly deleted.

---

## WSL2 Provisioner

WSL2 creates Debian distributions on Windows. Agentworks runs natively on
Windows (not inside WSL2).

### Distro Creation

```text
1. Download Debian rootfs tarball (if not cached)
2. wsl --import <vm_name> <install_path> <tarball_path>
3. wsl --distribution <vm_name> --user root -- useradd -m -s /bin/bash agentworks
4. wsl --distribution <vm_name> --user root -- usermod -aG sudo agentworks
5. Configure default user in /etc/wsl.conf
```

The install path is `%LOCALAPPDATA%\agentworks\wsl\<vm_name>`.

### SSH Access

WSL2 distros are not natively accessible via SSH. Agentworks uses
`wsl --distribution <vm_name> --user agentworks --` as the execution primitive
instead of SSH during initialization. After Tailscale setup, the distro becomes
SSH-accessible over the tailnet like any other VM.

This means the SSH execution primitive (`ssh.py`) needs to support a WSL2 mode
that wraps commands in `wsl --distribution <vm_name> --` instead of SSH. The
initializer should be agnostic to which transport is used.

### Lifecycle Commands

| Command | Implementation                                          |
| ------- | ------------------------------------------------------- |
| start   | `wsl --distribution <vm_name>` (starts if not running)  |
| stop    | `wsl --terminate <vm_name>`                             |
| delete  | `wsl --unregister <vm_name>` + remove install directory |
| status  | `wsl --list --verbose` (parse state)                    |

---

## VM Initialization Flow

After platform provisioning returns an SSH target (or WSL2 exec target), the
initializer runs the uniform setup sequence. This is platform-agnostic -- the
initializer does not know or care which provisioner created the VM.

### Provisioning Transport

The provisioning transport is the mechanism used to reach the VM before
Tailscale is available:

- **Lima (local)**: direct SSH via `limactl show-ssh` (SSH target on localhost
  with an auto-assigned port)
- **Lima (remote)**: multi-hop SSH -- the User Workstation SSHs to the VM Host,
  which in turn SSHs to the Lima VM. The SSH execution primitive (`ssh.py`)
  handles this transparently using SSH ProxyJump or nested commands. File
  transfers (scp/rsync) are not available over this path.
- **Azure**: direct SSH to the VM's public IP
- **WSL2**: `wsl --distribution <vm_name>` exec primitive (not SSH). File
  transfers are not available over this path.

Because some provisioning transports do not support file transfer, the
initializer uses a **Tailscale-first** approach: set up Tailscale early, then
switch to direct Tailscale SSH for the remainder of initialization. This ensures
operations like dotfiles rsync work uniformly across all platforms.

**WSL2 and Tailscale**: WSL2 distros share the Windows host's network stack by
default. If Tailscale is already running on the Windows host, the WSL2 distro
must run Tailscale in userspace networking mode
(`tailscale up --userspace-networking`) to avoid conflicts. The initializer
detects the WSL2 platform and applies this flag automatically.

### Pre-Flight

Before starting provisioning, verify auth for the **selected** git host
providers (resolved via `--git-hosts` flag, `defaults.git_hosts`, or all
configured providers as fallback). Providers that are configured but not
selected for this VM creation are not checked.

```text
For each provider in selected_git_hosts:
  if not provider.verify_auth():
    exit with error: "Authentication failed for {provider}. Run {provider.auth_hint}."
```

This happens before any VM is created, so the user does not end up with a
half-provisioned VM.

### Initialization Sequence

Initialization is split into two phases: bootstrap (over provisioning transport)
and setup (over Tailscale SSH).

#### Phase A: Bootstrap (over provisioning transport)

Sets `init_status = "bootstrapping"` at start.

```text
 1. Set init_status = "bootstrapping"
 2. Ensure the agentworks user exists:
      id agentworks || useradd -m -s /bin/bash agentworks
      usermod -aG sudo agentworks
      echo 'agentworks ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agentworks
    (Azure cloud-init and WSL2 distro creation handle this during provisioning;
     Lima does not, so the initializer must be idempotent here.)
 3. apt-get update && apt-get install -y <system_packages>
    System packages (always installed): openssh-server, curl, git, sudo, ca-certificates
 4. Append user's ssh_public_key to /home/agentworks/.ssh/authorized_keys
    (Enables SSH as agentworks user over Tailscale in the next steps.)
 5. Prompt user for Tailscale auth key
 6. curl -fsSL https://tailscale.com/install.sh | sh
 7. tailscale up --auth-key <key>
    (WSL2: add --userspace-networking to avoid conflict with host Tailscale.)
 8. Read Tailscale IP: tailscale ip -4
 9. Update VM record: tailscale_host, init_status = "tailscale_up"
```

#### Phase B: Setup (over Tailscale SSH as agentworks user)

The initializer switches to the Tailscale SSH target for all remaining steps.
This provides direct access from the User Workstation and enables file transfer
(rsync) that may not be available over the provisioning transport.

Sets `init_status = "initializing"` at start.

```text
10. Set init_status = "initializing"
11. apt-get install -y <user_apt_packages + extra_packages>
12. snap install <snap_packages>  (if any)
13. For each command in install_commands:
      Execute command as the agentworks user via: su - agentworks -c '<command>'
14. chsh -s $(which <shell>) agentworks
15. su - agentworks -c 'ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""'
16. Read /home/agentworks/.ssh/id_ed25519.pub
17. For each provider in selected_git_hosts:
      remote_key_id = provider.register_key(vm_name, public_key)
      db.insert_vm_git_host_key(vm_name, provider_name, remote_key_id)
18. If dotfiles.enabled and dotfiles.source (default: ~/.dotfiles) exists on User Workstation:
      a. rsync <dotfiles.source> to /home/agentworks/.dotfiles on VM
      b. cd /home/agentworks/.dotfiles && <install_cmd> (as agentworks user)
19. Set init_status = "complete"
```

### Error Handling

Error handling depends on the step type:

- **Install commands** (step 13): if a command fails, the initializer logs the
  error and **continues** to the next install command. This is intentional --
  install commands are often independent, and a failure in one should not block
  the rest of initialization.
- **All other steps**: if a step fails, the initializer **stops** immediately.
  It prints a clear error message identifying the failed step and leaves the VM
  in its current state (no rollback).

In both cases, the VM is recorded in the state database with
`init_status = "failed"`. The user can then SSH into the VM manually to debug,
or delete it and try again. Partial initialization is better than silent cleanup
that hides the failure.

### Install Command Execution

Install commands are shell commands for tools not available via apt or snap --
typically the one-liner install commands from the tool's official website (e.g.
`curl -fsSL https://bun.sh/install | bash`). They are:

- Run in order as listed in `vm.config.install_commands`
- Executed as the `agentworks` user (not root) -- commands that need root should
  use `sudo` internally
- Each command is run via `su - agentworks -c '<command>'` so it inherits the
  user's login environment

Commands should be idempotent where possible, since a user may re-run
initialization on a partially initialized VM in the future. If a command fails,
the initializer reports the failure and continues to the next step (see Error
Handling).

---

## Tailscale Rejoin Flow

When a VM is started and its Tailscale node is no longer reachable (e.g.
ephemeral auth key caused the node to be removed from the tailnet on stop),
Agentworks re-joins the tailnet via the provisioning transport.

The rejoin logic is extracted into `rejoin_tailscale()` in `initializer.py` and
is called from `vm start` (via `manager.py`).

### Flow

```text
1. vm start triggers platform provisioner to start the VM
2. Check Tailscale reachability: tailscale ping --timeout=5s -c=1 <host>
3. If reachable: done
4. If not reachable (or no host stored):
   a. Clear tailscale_host from DB
   b. Get provisioning transport: provisioner.exec_target(vm)
   c. Ensure Tailscale is installed on the VM (idempotent)
   d. Prompt for auth key (or use TAILSCALE_AUTH_KEY env var)
   e. tailscale up --auth-key <key> (WSL2: add --userspace-networking)
   f. Read new Tailscale IP: tailscale ip -4
   g. Update DB with new tailscale_host
```

### On Stop

After stopping a VM, Agentworks checks whether the Tailscale node survived:

```text
1. vm stop triggers platform provisioner to stop the VM
2. If tailscale_host is stored: ping to check reachability
3. If not reachable: clear tailscale_host from DB, log informational message
```

This ensures the next `vm start` knows to re-join rather than assuming
connectivity.
