# Using Proxmox with agentworks

Proxmox VE is a server virtualization platform based on KVM/QEMU. Agentworks provisions VMs by
cloning a Debian 12 cloud-init template that you prepare once on your Proxmox node.

## Prerequisites

- A Proxmox VE 8.x server accessible from your workstation
- Root SSH access to the Proxmox host for one-time setup
- A storage volume that supports VM disk images (e.g. `local-lvm`, `data`, or any LVM-thin/ZFS pool)

We strongly recommend running Tailscale on your Proxmox host so that the API and provisioned VMs are
accessible over your tailnet. This simplifies networking and avoids exposing the Proxmox API or VM
SSH ports to the public internet. Setting up Tailscale on the Proxmox host is outside the scope of
this guide -- see the [Tailscale Linux documentation](https://tailscale.com/kb/1031/install-linux)
for instructions.

## Step 1: Run the setup script

Agentworks includes a setup script that handles all Proxmox-side configuration in one step:

- Creates a Debian 12 cloud-init VM template with `qemu-guest-agent`
- Creates an `agentworks` resource pool for VM isolation
- Creates least-privilege custom roles and ACLs
- Creates a dedicated API user and token

Copy the script to your Proxmox host and run it as root:

```bash
scp scripts/proxmox-setup.sh root@<proxmox-host>:/tmp/
ssh -t root@<proxmox-host> bash /tmp/proxmox-setup.sh <vmid> <storage> <bridge>
```

For example:

```bash
scp scripts/proxmox-setup.sh root@pve.monkey-cat.ts.net:/tmp/
ssh -t root@pve.monkey-cat.ts.net bash /tmp/proxmox-setup.sh 9000 local vmbr0
```

To tear down all agentworks resources (template, pool, roles, user):

```bash
scp scripts/proxmox-teardown.sh root@<proxmox-host>:/tmp/
ssh -t root@<proxmox-host> bash /tmp/proxmox-teardown.sh <vmid>
```

| Argument  | Description              | Default     |
| --------- | ------------------------ | ----------- |
| `vmid`    | VMID for the template    | `9000`      |
| `storage` | Storage volume for disks | `local-lvm` |
| `bridge`  | Network bridge           | `vmbr0`     |

The script is idempotent -- it skips resources that already exist. At the end it prints the config
block and token secret for your agentworks config.

### Security model

The setup script follows least-privilege principles:

- **Custom roles** with only the privileges agentworks needs (no snapshots, backups, migrations,
  console access)
- **Pool-scoped ACLs** so the token can only manage VMs in the `agentworks` pool
- **Privilege-separated token** (`--privsep=1`) with its own permissions
- **Scoped storage/template/SDN access** to only the specific resources needed

**The token can:** Clone the template, configure/start/stop/delete VMs in the `agentworks` pool,
query the guest agent, allocate disk space on the specified storage.

**The token cannot:** Manage VMs outside the pool, access other storage, take snapshots, create
backups, migrate VMs, access the console, or manage users/nodes/cluster config.

### Manual setup

If you prefer to set things up manually, see the script source for the exact commands. The key
components are:

- A Debian 12 cloud-init template with `qemu-guest-agent` pre-installed
- A resource pool (`agentworks`) to scope API permissions
- Custom roles: `AgentworksVM`, `AgentworksTemplate`, `AgentworksStorage`, `AgentworksSDN`
- ACLs on `/pool/agentworks`, `/vms/<template>`, `/storage/<storage>`, `/sdn/zones/localnetwork`

## Step 2: Configure agentworks

Declare a `vm-site` resource for the cluster. Save this (any filename) under
`~/.config/agentworks/resources/`, filling in the values the setup script printed:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: proxmox
spec:
  platform: proxmox
  platform_config:
    api_url: "https://pve.example.com:8006"
    node: pve
    token_id: "agentworks@pam!agentworks"
    template_vmid: 9000
    storage: data
    bridge: vmbr0
    pool: agentworks
    verify_ssl: false
```

| Field           | Description                                                          |
| --------------- | -------------------------------------------------------------------- |
| `api_url`       | Your Proxmox web UI URL (same host, port 8006)                       |
| `node`          | Proxmox node name (visible in the UI sidebar, usually `pve`)         |
| `token_id`      | The `full-tokenid` from the setup script output                      |
| `template_vmid` | The VMID you used for the template (e.g. `9000`)                     |
| `storage`       | Storage for VM disks (block storage like `local-lvm` or `data`)      |
| `bridge`        | Network bridge (usually `vmbr0`)                                     |
| `pool`          | Proxmox resource pool for agentworks VMs (default `agentworks`)      |
| `verify_ssl`    | Set `false` if using a self-signed certificate (common for homelabs) |
| `token_secret`  | Name of the secret holding the API token (default below)             |

(`agw resource sample vm-site` prints a commented starter. The legacy flat `[proxmox]` section in
`config.toml` still loads as a deprecated declaration; `agw resource migrate vm-site` converts it.)

The API token value is an ordinary agentworks secret named `proxmox-token` (auto-declared; rename
per site via `platform_config.token_secret`). The default env-var backend reads it from:

```bash
export AW_SECRET_PROXMOX_TOKEN="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

With no env var set, agentworks prompts for it when a command needs it.
`agw secret describe proxmox-token` shows how it resolves.

Upgrading from the legacy flow and already exporting `PROXMOX_TOKEN_SECRET`? Either rename the
variable, or keep it by declaring the secret with a mapping:

```yaml
apiVersion: agentworks/v1
kind: secret
metadata:
  name: proxmox-token
spec:
  backend_mappings:
    env-var: PROXMOX_TOKEN_SECRET
```

## Step 3: Create a VM

```bash
agw vm create test-vm --site proxmox
```

Or set the site as your default:

```toml
[defaults]
site = "proxmox"
```

## Lifecycle commands

```bash
agw vm stop test-vm
agw vm start test-vm
agw vm describe test-vm
agw vm delete test-vm
```

## How it works

When you run `agw vm create <name> --site proxmox`:

1. Clones the template into the `agentworks` pool via the Proxmox REST API
2. Configures CPU, memory, cloud-init user/SSH keys, and DHCP networking
3. Starts the VM and waits for the QEMU guest agent to report an IP
4. SSHs into the VM via ProxyJump through the Proxmox host
5. Runs the bootstrap script (same one used by Lima and Azure) which installs system packages, sets
   up the admin user, and joins Tailscale
6. Hands off to the agentworks initializer (over Tailscale SSH) for remaining setup

After provisioning, everything works over Tailscale SSH -- identical to Lima and Azure VMs.

## Troubleshooting

### "Timed out waiting for guest agent"

The QEMU guest agent is not responding. Check:

- The guest agent is installed in the template (the setup script handles this via `virt-customize`)
- The guest agent is enabled in the VM config (`qm set <vmid> --agent enabled=1`)
- The VM has finished booting -- connect via `qm terminal <vmid>` to check

### The token secret won't resolve

Set it for the env-var backend (`export AW_SECRET_PROXMOX_TOKEN="your-secret-here"`) or let the
prompt backend ask. `agw secret describe proxmox-token` shows how each backend would look it up;
`agw doctor` reports the runtime outcome.

### "401 Unauthorized" from the API

- Verify `token_id` matches the `full-tokenid` from the setup script output
- Verify the `proxmox-token` value matches the token secret from the setup script
- Re-run the setup script to verify all ACLs are in place

### Permission denied on clone or network

Check that all four ACLs are set (re-run the setup script if unsure):

- `AgentworksVM` on `/pool/agentworks` -- VM lifecycle within the pool
- `AgentworksTemplate` on `/vms/<template_vmid>` -- clone permission on the template
- `AgentworksStorage` on `/storage/<storage>` -- disk allocation
- `AgentworksSDN` on `/sdn/zones/localnetwork` -- network bridge access

### Self-signed certificate errors

Set `verify_ssl: false` in the site's `platform_config`. This is common for homelab setups without a
trusted CA.
