# Proxmox provider: locked

**Locked:** 2026-08-05

The completed checklist was reverified against the current implementation, automated tests, setup
and teardown scripts, sample resources, and `docs/guides/proxmox.md`. The original provider shipped
clone-based creation, lifecycle operations, QEMU guest-agent discovery and execution, bootstrap
delivery, persisted VMID tracking, and least-privilege pool setup as planned.

The implementation later evolved without invalidating those completed records. Proxmox is now an
opt-in system plugin and implements the `VMPlatform` capability. Configuration is declared through a
`vm-site` resource rather than the original `[proxmox]` table, and the VMID and node are stored in
platform metadata rather than relying only on the original dedicated column. API credentials use the
secret-resource system, with `proxmox-token` as the default secret name. Provisioning now uses
ProxyJump SSH through the Proxmox host for bootstrap and then uses Tailscale for normal lifecycle
transport. Create rollback and interruption handling were also hardened after the initial effort.

The stable operator contract, current YAML examples, setup instructions, ACL model, and
troubleshooting guidance live in `docs/guides/proxmox.md`. Implementation and regression coverage
live under `cli/agentworks/plugins/proxmox/` and `cli/tests/plugins/test_proxmox*.py`; operational
setup remains in `scripts/proxmox-setup.sh` and `scripts/proxmox-teardown.sh`. Nothing in this
directory is required to operate or maintain the current provider.
