# Proxmox Provider -- Implementation Plan

**Status:** Active **Parent:** [frd.md](frd.md)

---

## Checklist

- [x] SDD artifacts (frd.md, hla.md, plan.md)
- [x] Config: add ProxmoxConfig dataclass and loader
- [x] Config: add "proxmox" to VALID_PLATFORMS and EXPECTED_TOP_LEVEL_KEYS
- [x] Database: migration 16 for proxmox_vmid column
- [x] Database: VMRow field, update method, insert/converter changes
- [x] ProvisionResult: add proxmox_vmid field
- [x] ProxmoxAPI client (new file, stdlib urllib.request, JSON body for guest agent)
- [x] ProxmoxProvisioner (new file, guest agent bootstrap delivery)
- [x] Manager integration: config validation, provisioning branch, DB update, get_provisioner
- [x] Sample config: add [proxmox] section
- [x] Tests: config loading tests (table-driven)
- [x] Tests: API client unit tests
- [x] Spelling: add Proxmox terms to .cspell.json
- [x] LLD documentation update
- [x] User guide (docs/guides/proxmox.md)
- [x] Setup script (scripts/proxmox-setup.sh)
- [x] Teardown script (scripts/proxmox-teardown.sh)
- [x] CLI: add "proxmox" to platform choice list
- [x] ssh.py: add proxy_jump support to copy_to (bug fix)
- [x] initializer.py: fix bootstrap hostname platform detection
