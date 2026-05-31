# Proxmox Provider -- Functional Requirements

**Status:** Active **Parent:** [../2026-03-05-agentworks/plan.md](../2026-03-05-agentworks/plan.md)

---

## Overview

Add Proxmox VE as a supported VM platform in Agentworks, enabling users with Proxmox homelab or
datacenter setups to provision and manage VMs through the same CLI interface used for Lima, Azure,
and WSL2.

---

## Requirements

### FR-1: Proxmox as a Supported Platform

- Users can specify `--platform proxmox` when creating a VM.
- The `defaults.platform` config field accepts `"proxmox"`.

### FR-2: Clone-Based Provisioning

- VMs are created by cloning a pre-existing Proxmox VM template (identified by VMID).
- The template is a Debian 12 cloud image with cloud-init and qemu-guest-agent pre-installed (via
  `virt-customize`).
- The provisioner allocates a new VMID, clones the template into a resource pool, configures
  resources (CPU, memory, disk), and starts the VM.

### FR-3: Guest Agent Bootstrap

- Cloud-init handles basic setup (admin user, SSH keys, DHCP networking).
- The bootstrap script (same `generate_bootstrap_script()` used by Lima/Azure) is delivered and
  executed inside the VM via the QEMU guest agent `file-write` and `exec` APIs.
- The bootstrap installs system packages, configures the admin user, and joins Tailscale.

### FR-4: Full Lifecycle Management

- **create**: Clone template, configure, start, wait for guest agent, run bootstrap, return
  ProvisionResult.
- **start**: Start a stopped VM via the Proxmox API.
- **stop**: Stop a running VM via the Proxmox API.
- **delete**: Stop (if running) then delete the VM via the Proxmox API.
- **status**: Query VM status and map to the VMStatus enum (running/stopped/unknown).

### FR-5: QEMU Guest Agent

The guest agent serves three purposes:

- **IP discovery**: Poll `network-get-interfaces` for the VM's DHCP-assigned IP address.
- **Cloud-init gate**: Run `cloud-init status --wait` to ensure cloud-init has finished before
  running bootstrap (avoids apt lock conflicts).
- **Bootstrap delivery**: Write and execute the bootstrap script inside the VM via `file-write` and
  `exec` -- no SSH to the Proxmox host required at runtime.

### FR-6: Configuration

- A `[proxmox]` section in config.toml provides: `api_url`, `node`, `token_id`, `template_vmid`, and
  optional `storage`, `bridge`, `pool`, `verify_ssl`.
- The API token secret is read from the `PROXMOX_TOKEN_SECRET` environment variable at API call time
  (not stored in config).

### FR-7: Database Tracking

- The Proxmox VMID is stored in the `vms` table (`proxmox_vmid` column) for lifecycle operations.

### FR-8: Resource Pool Isolation

- Cloned VMs are placed in a configurable Proxmox resource pool (default `agentworks`).
- The setup script creates least-privilege custom roles and pool-scoped ACLs so the API token can
  only manage VMs within the pool.
