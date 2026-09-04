# Using Proxmox with agentworks

Proxmox VE is a server virtualization platform based on KVM/QEMU. Agentworks provisions VMs by
cloning a Debian 13 Trixie cloud-init template that you prepare once on your Proxmox node.

> **Enable the proxmox plugin first.** The Proxmox VM platform ships as an opt-in system plugin, so
> before any proxmox site will work you must enable it in your `config.toml`:
>
> ```toml
> [plugins]
> system = ["proxmox"]
> ```
>
> Until you do, a proxmox `vm-site` is present-but-not-ready with an "enable plugin `proxmox`" hint
> and every VM command on it is refused (never an unknown-name error). `agw doctor` lists the plugin
> and its enable state. Step 2 below repeats this alongside the site declaration.

## Prerequisites

- A Proxmox VE 8.x server accessible from your workstation
- Root SSH access to the Proxmox host for one-time setup
- A storage volume that supports VM disk images (e.g. `local-lvm`, `data`, or any LVM-thin/ZFS pool)

Run Tailscale on the Proxmox host to keep the API and provisioned VMs accessible over the tailnet
without exposing the Proxmox API or VM SSH ports to the public internet. Setting up Tailscale on the
Proxmox host is outside the scope of this guide; see the
[Tailscale Linux documentation](https://tailscale.com/kb/1031/install-linux) for instructions.

## Step 1: Run the setup script

Agentworks includes a setup script that handles all Proxmox-side configuration in one step:

- Creates a Debian 13 Trixie cloud-init VM template with `qemu-guest-agent`
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
ssh -t root@pve.monkey-cat.ts.net bash /tmp/proxmox-setup.sh 9001 local vmbr0
```

To tear down all agentworks resources (template, pool, roles, user):

```bash
scp scripts/proxmox-teardown.sh root@<proxmox-host>:/tmp/
ssh -t root@<proxmox-host> bash /tmp/proxmox-teardown.sh <vmid>
```

| Argument  | Description              | Default     |
| --------- | ------------------------ | ----------- |
| `vmid`    | VMID for the template    | `9001`      |
| `storage` | Storage volume for disks | `local-lvm` |
| `bridge`  | Network bridge           | `vmbr0`     |

The script builds Agentworks' current Debian release; it does not expose an operating-system or
release selector. Bookworm-era setup used VMID 9000, so the Trixie script defaults to 9001. Do not
reuse a Bookworm template VMID: choose another unused value if 9001 is occupied. On a rerun the
script skips an existing template only when its name and Agentworks release tags match; otherwise it
stops before printing a misleading Trixie mapping. At the end it prints the config block and token
secret for your agentworks config.

### Security model

The setup script follows least-privilege principles:

- **Custom roles** with only the privileges agentworks needs (no snapshots, backups, migrations,
  console access)
- **Pool-scoped ACLs** so the token can only manage VMs in the `agentworks` pool
- **Dedicated token** that inherits only the scoped user's permissions (`--privsep=0`)
- **Scoped storage/template/SDN access** to only the specific resources needed

**The token can:** Clone the template, configure/start/stop/delete VMs in the `agentworks` pool,
query the guest agent, and allocate disk space on the specified storage.

**The token cannot:** Manage VMs outside the pool, access other storage, take snapshots, create
backups, migrate VMs, access the console, or manage users/nodes/cluster config.

### Manual setup

If you prefer to set things up manually, see the script source for the exact commands. The key
components are:

- A Debian 13 Trixie cloud-init template with `qemu-guest-agent` pre-installed
- A resource pool (`agentworks`) to scope API permissions
- Custom roles: `AgentworksVM`, `AgentworksTemplate`, `AgentworksStorage`, `AgentworksSDN`
- ACLs on `/pool/agentworks`, `/vms/<template>`, `/storage/<storage>`, `/sdn/zones/localnetwork`

## Step 2: Configure agentworks

First enable the proxmox plugin in `config.toml` (see the note at the top of this guide), otherwise
the site you declare next is not-ready and refused at use:

```toml
[plugins]
system = ["proxmox"]
```

Then declare a `vm-site` resource for the cluster. Save this (any filename) under
`~/.config/agentworks/resources/`, filling in the values the setup script printed:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: proxmox
spec:
  platform:
    name: proxmox
    api_url: "https://pve.example.com:8006"
    node: pve
    token_id: "agentworks@pam!agentworks"
    template_vmids:
      trixie: 9001
    storage: data
    bridge: vmbr0
    pool: agentworks
    verify_ssl: false
```

`agw resource explain vm-platform/proxmox` documents every field above with its type, whether it is
required, and its default; `agw resource sample vm-site` prints a commented starter to edit. Two
values map to things this guide produced rather than to anything Proxmox calls by that name:
`token_id` is the `full-tokenid` the setup script printed, and `template_vmids.trixie` is the VMID
you gave the current template. `node` is the node name in the Proxmox UI sidebar, usually `pve`. The
old `template_vmid` scalar remains readable only as a legacy Bookworm mapping and cannot satisfy
current Trixie creation.

A site without `template_vmids.trixie` remains loadable for best-effort operations on existing VMs.
New VM creation validates the concrete core-selected release during preflight and fails before the
command's secret-resolution phase or Proxmox API authentication when that mapping is missing. After
cloning and bootstrapping the mapped template, core verifies the live guest's `/etc/os-release`
through the returned Tailscale SSH transport. A missing, non-Debian, or wrong-release observation
retains an addressable failed VM row for explicit deletion. There is no configuration switch to skip
the check.

For 0.13 configuration migration, see [Upgrading to 0.14](upgrading-to-0.14.md).

The API token value is an ordinary agentworks secret named `proxmox-token` (auto-declared; rename
per site via the `token_secret` key). The default env-var backend reads it from:

```bash
export AW_SECRET_PROXMOX_TOKEN="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

With no env var set, agentworks prompts for it when a command needs it.
`agw secret describe proxmox-token` shows how it resolves.

To use a different environment variable, declare a secret backend mapping:

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

### Native transport limitation

Proxmox does not currently implement the required administrative transport that works independently
of the VM's Tailscale state. Consequently, `vm shell --platform`, start-time Tailscale rejoin, and
`vm rekey` cannot use Proxmox to recover a broken VM registration. Use the Proxmox web UI's serial
console for manual access. [Issue #727](https://github.com/WayfarerLabs/agentworks/issues/727)
tracks the native execution transport.

## How it works

When you run `agw vm create <name> --site proxmox`:

1. Clones the template into the `agentworks` pool via the Proxmox REST API
2. Configures CPU, memory, cloud-init user/SSH keys, and DHCP networking
3. Starts the VM and waits for the QEMU guest agent to report an IP
4. Waits for cloud-init and runs the private bootstrap script through QEMU Guest Agent
5. The bootstrap installs system packages, configures the admin user, and joins Tailscale
6. Returns Tailscale SSH to core, which verifies the live Debian release and completes
   initialization

After provisioning, normal operations use Tailscale SSH, as they do on Lima and Azure VMs.

## Troubleshooting

### "Timed out waiting for guest agent"

The QEMU guest agent is not responding. Check:

- The guest agent is installed in the template (the setup script handles this via `virt-customize`)
- The guest agent is enabled in the VM config (`qm set <vmid> --agent enabled=1`)
- The VM has finished booting -- connect via `qm terminal <vmid>` to check

### The token secret won't resolve

Set it for the env-var backend (`export AW_SECRET_PROXMOX_TOKEN="your-secret-here"`) or let the
prompt backend ask. `agw secret list` shows each source's static mapping.
`agw secret describe proxmox-token` and `agw secret verify proxmox-token` perform provider-aware,
value-free previews; add `--allow-interaction` when a definitive answer may prompt or authenticate.
`agw doctor` uses the no-impact preview, which may read and safely discard a provider value but
never returns it.

### "401 Unauthorized" from the API

- Verify `token_id` matches the `full-tokenid` from the setup script output
- Verify the `proxmox-token` value matches the token secret from the setup script
- Re-run the setup script to verify all ACLs are in place

### Permission denied on clone or network

Check that all four ACLs are set (re-run the setup script if unsure):

- `AgentworksVM` on `/pool/agentworks`: VM lifecycle within the pool
- `AgentworksTemplate` on `/vms/<template_vmid>` -- clone permission on the template
- `AgentworksStorage` on `/storage/<storage>` -- disk allocation
- `AgentworksSDN` on `/sdn/zones/localnetwork` -- network bridge access

### Self-signed certificate errors

Set `verify_ssl: false` in the site's `platform` table. This is common for homelab setups without a
trusted CA.
