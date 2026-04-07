# Proxmox Provider -- High-Level Architecture

**Status:** Active **Parent:** [frd.md](frd.md)

---

## Overview

The Proxmox provider fits into the existing `VMProvisioner` interface. It communicates with the
Proxmox VE REST API using a thin client built on stdlib `urllib.request` (no new dependencies).
Bootstrap delivery uses the QEMU guest agent `exec` API -- no SSH to the Proxmox host is needed
at runtime.

---

## Component Diagram

```text
CLI (vm create --platform proxmox)
  |
  v
manager.py
  |-- validates [proxmox] config section exists
  |-- creates ProxmoxProvisioner(proxmox_config)
  |-- calls provisioner.create(...)
  |-- stores proxmox_vmid in DB
  |-- hands off to initializer (same as Azure/Lima)
  |
  v
ProxmoxProvisioner (vms/provisioners/proxmox.py)
  |-- implements VMProvisioner interface
  |-- uses ProxmoxAPI client for all REST calls
  |
  v
ProxmoxAPI (vms/provisioners/proxmox_api.py)
  |-- thin REST client over urllib.request
  |-- auth via PVEAPIToken header
  |-- form-urlencoded for most endpoints, JSON for guest agent
  |-- methods: next_id, clone_vm, configure_vm, resize_disk,
  |   start_vm, stop_vm, delete_vm, vm_status, wait_for_task,
  |   guest_agent_network, guest_agent_exec_wait,
  |   guest_agent_file_write
  |
  v
Proxmox VE REST API (user's server)
```

---

## Config Schema

```toml
[proxmox]
api_url = "https://pve.example.com:8006"   # Proxmox API URL
node = "pve"                                # Proxmox node name
token_id = "agentworks@pam!agentworks"      # API token ID (user@realm!tokenname)
template_vmid = 9000                        # VMID of Debian 12 cloud-init template
storage = "local-lvm"                       # target storage (default)
bridge = "vmbr0"                            # network bridge (default)
pool = "agentworks"                         # resource pool for VMs (default)
verify_ssl = true                           # verify TLS certificate (default)
```

Token secret: `PROXMOX_TOKEN_SECRET` environment variable.

---

## Provisioning Flow

1. Get next available VMID via `GET /cluster/nextid`
2. Clone template into pool: `POST /nodes/{node}/qemu/{template_vmid}/clone`
3. Wait for clone task completion
4. Configure VM: set CPU, memory, CPU type (host), cloud-init (ciuser, sshkeys, ipconfig0), boot
   order, guest agent
5. Resize disk if needed
6. Start VM
7. Poll QEMU guest agent until it responds (`network-get-interfaces`)
8. Get VM IP from guest agent
9. Wait for cloud-init to finish (`cloud-init status --wait` via guest agent exec)
10. Write bootstrap script to VM via guest agent `file-write`
11. Execute bootstrap via guest agent `exec` (installs packages, joins Tailscale)
12. Return ProvisionResult with Tailscale IP and proxmox_vmid

---

## Bootstrap Delivery

The Proxmox upload API does not support snippet files (hardcoded enum limitation). Instead, the
provisioner delivers the bootstrap script via the QEMU guest agent:

1. Cloud-init handles minimal setup: admin user, SSH authorized keys, DHCP networking
2. After cloud-init finishes, the provisioner writes the bootstrap script to
   `/tmp/agentworks-bootstrap.sh` via the `agent/file-write` API
3. The provisioner executes the script via the `agent/exec` API (JSON body with command as array,
   required by Proxmox 8+)
4. The bootstrap installs system packages, configures the admin user, and joins Tailscale
5. After Tailscale join, the initializer takes over via Tailscale SSH (identical to all platforms)

This avoids SSH to the Proxmox host at runtime and keeps the provisioner fully API-driven.

---

## Security Model

The setup script (`scripts/proxmox-setup.sh`) creates least-privilege resources:

- **Resource pool** (`agentworks`): all VMs are cloned into this pool
- **Custom roles**: `AgentworksVM` (VM lifecycle), `AgentworksTemplate` (clone-only),
  `AgentworksStorage` (disk allocation), `AgentworksSDN` (network bridge)
- **Scoped ACLs**: permissions are granted on specific paths (`/pool/agentworks`,
  `/vms/{template}`, `/storage/{storage}`, `/sdn/zones/localnetwork`)
- **API token** (`--privsep=0`): inherits user permissions, which are limited to the above ACLs

The token cannot manage VMs outside the pool, access other storage, take snapshots, create backups,
migrate VMs, or manage cluster infrastructure.
