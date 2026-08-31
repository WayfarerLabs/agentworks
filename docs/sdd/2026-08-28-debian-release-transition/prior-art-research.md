# Prior Art: Debian Release Transition

- Date: 2026-08-28
- Scope: Debian's supported upgrade contract, current provider images, recovery primitives, and
  existing Agentworks seams

## Executive summary

Debian supports a direct Bookworm-to-Trixie in-place upgrade and documents the preparation,
two-stage package procedure, reboot, and recovery posture in detail. Agentworks can orchestrate that
procedure, but it cannot honestly promise automatic rollback: its current VM backup contains
metadata and workspace data rather than a bootable disk, and the six platforms expose incompatible
snapshot, retention, and restore models.

Official Trixie artifacts exist for every current platform. Proxmox is different because it has no
provider-wide image selector; the operator must build a Trixie template and map the release to its
VMID. This does not justify a general image override on the other platforms.

The smallest safe product shape is therefore:

- fixed Trixie creation from platform-owned release maps;
- live release validation and first-class persistence;
- a durable, resumable `vm upgrade` that follows Debian's documented procedure;
- local application/config backups plus an operator-attested external recovery artifact; and
- no provider snapshot lifecycle until Agentworks can also own restore, retention, and cleanup.

## Debian's supported path

### Direct Bookworm to Trixie is supported

Debian supports upgrades to Trixie only from Debian 12 Bookworm. It requires Bookworm to be on its
latest point release first. The notes recommend a backup, recovery access for remote systems,
package database checks, removal or review of complicating sources/packages, codename-pinned Trixie
sources, a minimal upgrade, a full upgrade, and a reboot.

Design consequences:

- `vm upgrade` accepts only a verified Bookworm guest or an already-Trixie guest being adopted.
- Trixie's profile owns an explicit upgrade-from-Bookworm policy, not a generic version increment or
  arbitrary pair graph.
- The command brings Bookworm current before changing suites.
- The preliminary plan and first confirmation authorize only bringing Bookworm current. Agentworks
  then recomputes the complete source/removal/package plan from that changed state and requires a
  second confirmation before the suite switch.
- It uses `apt-get` in scripts because Debian says `apt` is intended for interactive use.
- It performs the equivalent of `upgrade --without-new-pkgs` before `full-upgrade`.

Source:
[Debian Trixie upgrade notes](https://www.debian.org/releases/trixie/release-notes/upgrading.en.html)

### Remote recovery is a prerequisite

Debian recommends a full backup, or at minimum irreplaceable data and configuration, before
upgrading. For a remotely managed system it recommends a serial console or equivalent recovery route
because a new kernel or network configuration can make SSH unavailable. It also recommends a durable
terminal such as screen or tmux so a transient SSH disconnect does not terminate apt.

Design consequences:

- Package work runs detached from SSH with durable remote state.
- The orchestrator checks native dpkg/APT ownership and durably inhibits known automatic APT timers
  for the mutation window. It restores them only from a Bookworm-safe abort or verified healthy
  Trixie state, not while the package system is mixed or unhealthy.
- Agentworks creates local workspace/metadata and Debian configuration/package-state artifacts.
- The command requires an external checkpoint reference that identifies an actual recoverable
  artifact and shows platform console guidance.
- Those artifacts are not described as an automatic or bootable rollback.

Source:
[Upgrade preparation and recovery](https://www.debian.org/releases/trixie/release-notes/upgrading.en.html#preparing-for-the-upgrade)

### Preflight must be narrower than arbitrary Debian

Debian calls out `dpkg --audit`, package holds, APT pinning, backports, unofficial sources, obsolete
and non-Debian packages, package removals, disk capacity, kernel metapackages, and conffile
decisions. The Trixie issues chapter also names workload-specific cases that require manual
handling. Examples include MariaDB shutdown state and RabbitMQ's difficult direct upgrade path.

Design consequences:

- Agentworks fails closed on unhealthy dpkg, holds that block transition, inadequate space,
  unsupported source layouts, changed package conffiles, and known package blockers.
- It shows simulated removals for operator approval.
- It does not grow a general debconf/conffile answer engine or automatically apply force options
  after dependency failures.

Sources:
[Debian package and APT preparation](https://www.debian.org/releases/trixie/release-notes/upgrading.en.html#start-from-pure-debian),
[Trixie issues to be aware of](https://www.debian.org/releases/trixie/release-notes/issues.en.html)

### Trixie changes relevant to Agentworks

The release notes identify several concrete certification risks:

- interrupted SSH upgrades require Bookworm OpenSSH `1:9.2p1-2+deb12u7` or newer;
- a separate `/boot` should be at least 768 MB with about 300 MB free;
- `/tmp` becomes tmpfs after reboot and defaults to a 50 percent memory ceiling;
- DSA SSH keys are removed;
- sshd no longer reads `~/.pam_environment` by default;
- `/etc/sysctl.conf` is no longer read by `systemd-sysctl`, while `/etc/sysctl.d` remains valid;
- network interface names can change; and
- APT is moving toward deb822 `.sources` files.

Agentworks already writes sysctl drop-ins, so that item needs certification rather than a redesign.
Its size-unbounded VM backup and workspace copy archives currently stage under `/tmp`, so those
paths must move before Trixie certification.

Source:
[Trixie issues to be aware of](https://www.debian.org/releases/trixie/release-notes/issues.en.html)

### Support horizons

Bookworm LTS ends on 2028-06-30. Trixie's regular support runs through 2028-08-09 and its LTS period
ends on 2030-06-30.

Design consequences:

- New Bookworm creation ends immediately when the feature ships.
- Upstream dates remain useful release-planning and diagnostic facts, but do not define a second
  Agentworks support clock.
- Agentworks supports the release immediately before current, including its adjacent upgrade, and
  treats current-2 or older VMs as best effort with warnings and no supported upgrade.

Sources: [Bookworm release lifecycle](https://www.debian.org/releases/bookworm/),
[Trixie release lifecycle](https://www.debian.org/releases/trixie/)

### WSL2 kernel and restart ownership

WSL2 runs distributions as isolated containers inside a Microsoft-managed lightweight VM with a
shared Linux kernel. Systemd is supported inside the distribution and participates in its clean
shutdown, while `wsl --terminate`/activation owns the distribution lifecycle. Therefore a WSL2
distribution upgrade must not require a Debian kernel metapackage or apply guest udev predictions to
provider-managed interfaces; it verifies the Microsoft kernel and a changed distribution boot ID.

Sources:
[Microsoft WSL version comparison](https://learn.microsoft.com/en-us/windows/wsl/compare-versions),
[Microsoft WSL systemd architecture](https://learn.microsoft.com/en-us/windows/wsl/systemd), and
[Microsoft WSL lifecycle commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

## Official Trixie image selectors

| Platform | Official current selector                                                     | Design consequence                                                                               |
| -------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| AWS      | SSM path below `/aws/service/debian/release/13/latest`, split by architecture | Map release to SSM release segment and retain architecture mapping; do not pin regional AMI IDs. |
| Azure    | `Debian:debian-13:13-gen2:latest`                                             | Map the complete image record and disk floor together.                                           |
| GCP      | project `debian-cloud`, families `debian-13` and `debian-13-arm64`            | Add release as the outer key of the existing architecture map.                                   |
| WSL2     | Docker Official Image `debian:trixie`                                         | Pin the codename tag rather than moving `latest`; derive cache and diagnostic values from it.    |
| Lima     | official rolling `debian-13-generic-amd64.qcow2` and arm64 image              | Render Lima's image block from release and architecture maps.                                    |
| Proxmox  | no provider image catalog                                                     | Map release to an operator template VMID and verify the cloned guest.                            |

Sources:

- [Debian AWS Trixie images](https://wiki.debian.org/Cloud/AmazonEC2Image/Trixie)
- [Debian Azure images](https://wiki.debian.org/Cloud/MicrosoftAzure) and
  [Microsoft Debian 13 Marketplace entry](https://marketplace.microsoft.com/en-us/product/virtual-machines/debian.debian-13)
- [Google Compute Engine operating-system details](https://cloud.google.com/compute/docs/images/os-details)
- [Docker Official Debian image](https://hub.docker.com/_/debian/)
- [Debian Trixie cloud image checksums](https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS)
- [Lima templates](https://lima-vm.io/docs/templates/)
- [Proxmox VE administration guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)

## Third-party APT values

A host release and a vendor repository suite are not the same concept. The Trixie mapping must use
the vendor's supported value rather than replace every literal `bookworm` with `trixie`.

| Source         | Bookworm value | Trixie host value | Evidence                                                                                  |
| -------------- | -------------- | ----------------- | ----------------------------------------------------------------------------------------- |
| HashiCorp      | `bookworm`     | `trixie`          | HashiCorp derives and publishes the Debian codename.                                      |
| tofuutils/tenv | `bookworm`     | `trixie`          | Cloudsmith's generated Trixie setup publishes that suite.                                 |
| ngrok          | `bookworm`     | `bookworm`        | ngrok's current Debian instructions still prescribe `bookworm`; no Trixie Release exists. |
| GitHub CLI     | `stable`       | scalar `stable`   | Vendor suite is host-release independent.                                                 |
| NodeSource     | `nodistro`     | scalar `nodistro` | Vendor suite is host-release independent.                                                 |

Design consequences:

- Release-specific `apt-source` data is an explicit map selected from the verified guest release.
- A Trixie map value may contain `bookworm` when that is reviewed vendor policy.
- Distribution upgrade disables third-party sources, then Phase B recreates only selected sources
  from target-release values.

Sources: [HashiCorp Terraform installation](https://developer.hashicorp.com/terraform/install),
[tofuutils Cloudsmith setup](https://cloudsmith.io/~tofuutils/repos/tenv/setup/),
[ngrok Linux installation](https://ngrok.com/download/linux)

## Recovery primitives do not form one product contract

Every platform has some recovery primitive, but its creation, consistency, retention, restore, and
cleanup differ:

- AWS exposes EBS snapshots and recommends stopping an instance for a consistent root-volume
  snapshot.
- Azure exposes VM restore points containing restore points for attached disks.
- GCP disk snapshots restore by creating disks rather than overwriting the source disk.
- WSL documents `wsl --export` and `wsl --import` for backup/recovery.
- Lima has snapshot commands, but marks the snapshot family experimental and driver-dependent.
- Proxmox offers `vzdump` and Proxmox Backup Server; storage capabilities and permissions determine
  snapshot behavior. Agentworks' current least-privilege Proxmox role intentionally lacks backup and
  snapshot permissions.

Adding only snapshot creation would leave Agentworks owning chargeable artifacts without a common
restore or cleanup contract. The first transition therefore requires an operator-created external
checkpoint reference. Automatic provider checkpoint lifecycle is deferred until its restore,
retention, cleanup, and permission model can be designed as one owned feature.

Sources:
[AWS EBS snapshot guidance](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-create-snapshot.html),
[Azure VM restore points](https://learn.microsoft.com/en-us/azure/virtual-machines/backup-and-disaster-recovery-for-azure-iaas-disks),
[GCP disk snapshots](https://cloud.google.com/compute/docs/disks/snapshots),
[WSL backup guidance](https://learn.microsoft.com/en-us/windows/wsl/faq),
[Lima snapshot command](https://lima-vm.io/docs/reference/limactl_snapshot_create/),
[Lima experimental features](https://lima-vm.io/docs/releases/experimental/), and
[Proxmox VE administration guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)

Provider consoles are also not uniform or guaranteed. AWS serial console is disabled by default and
has IAM/guest prerequisites; Azure requires boot diagnostics and RBAC; GCP interactive serial access
requires metadata and IAM. The command must show platform guidance rather than label an existing SSH
transport an out-of-band console.

Sources:
[AWS EC2 serial console](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-access-to-serial-console.html),
[Azure Linux serial console](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/serial-console-linux),
[GCP interactive serial console](https://cloud.google.com/compute/docs/troubleshooting/troubleshooting-using-serial-console)

## Existing Agentworks seams

### Release state and provisioning

`VMRow` has no guest release field. `platform_metadata` is deliberately opaque and platform-owned,
so a cross-platform Debian lifecycle fact does not belong there. `ProvisionRequest` is the one
common fully resolved create input, and every manager-built request passes through it immediately
before platform dispatch.

The manager currently wraps every ordinary exception from `platform.create` as a
`ProvisioningError`, which would discard a typed missing-map error's remediation hint. It also ends
the create unwind window as soon as the platform returns and only then persists platform metadata. A
defensive core probe failure detected at that point therefore needs a retained row with its cleanup
identifiers, not row deletion that could orphan the returned backend. The plugin-author guide still
advertises vm-platform contract version 2 and teaches the version 2 request.

Design consequence: add a nullable first-class VM column and a required internal request value. Core
passes its concrete current release; each platform resolves that value through its own map rather
than inferring current. The vm-platform contract version changes so old plugins fail conformance,
while a contract-current platform missing the requested key fails clearly before backend mutation.
The manager preserves those focused errors and independently probes the returned transport instead
of trusting a platform-authored release result. A failed core probe retains backend metadata in a
failed row, and the capability plus plugin-author documentation changes with the contract.

Evidence: `cli/agentworks/db/models.py`, `cli/agentworks/db/converters.py`,
`cli/agentworks/db/database.py`, `cli/agentworks/capabilities/vm_platform/base.py`,
`cli/agentworks/vms/manager/lifecycle.py`, and `cli/agentworks/plugins/README.md`.

### No release probe exists

Current code does not read `/etc/os-release`. Proxmox clones an operator template without validating
its guest OS. Existing rows therefore cannot be backfilled from product history without inventing a
fact.

Design consequence: migration leaves the release unknown, and a shared live probe establishes it.

Evidence: `cli/agentworks/plugins/proxmox/platform.py` and repository-wide search for
`VERSION_CODENAME`/`os-release`.

### Reinit is not an OS upgrade

`reinit_vm` replays Phase B initialization. It never recreates the VM, replaces Debian sources, runs
a distribution upgrade, or owns a reboot. This separation is useful and should remain.

Design consequence: `vm upgrade` is its own manager workflow and invokes Phase B only after Trixie
has been observed.

Evidence: `cli/agentworks/vms/manager/lifecycle.py` and `cli/agentworks/vms/initializer/driver.py`.

### Native transport is not universal recovery

Lima and WSL have strong local native paths. AWS, Azure, and GCP native paths still require working
guest networking and sshd. Proxmox returns no post-create native transport. Ordinary VM operations
use canonical Tailscale SSH and do not silently fall back.

Design consequence: native transport can repair Tailscale after reboot where available, but it is
not the external recovery checkpoint required before upgrade.

Evidence: `cli/agentworks/capabilities/vm_platform/base.py`, each platform's `native_transport`, and
`cli/agentworks/transports/__init__.py`.

### Current backups and detached work are narrower

`vm backup` exports database metadata and workspace files but has no VM restore counterpart. The
detached runner stores status under `/tmp` by default and writes completion after the command exits,
so it cannot represent a command that reboots the guest. VM backup and workspace copy also stage
large archives under `/tmp`.

Design consequences:

- do not promise rollback from `vm backup`;
- add a focused release-upgrade recovery bundle;
- give the upgrade its own persistent systemd-backed stage runner; and
- move size-unbounded archive staging to disk-backed paths.

Evidence: `cli/agentworks/vms/backup.py`, `cli/agentworks/workspaces/manager/copy.py`, and
`cli/agentworks/remote_exec.py`.

## Refuted or not adopted

- **Replace every `bookworm` literal with `trixie`:** wrong for existing Bookworm VMs, vendor suite
  semantics, and persisted history.
- **Let the platform infer the current release:** duplicates product policy and cannot make reinit
  choose the existing VM's release.
- **Backfill all rows to Bookworm:** unprovable for Proxmox and manually changed guests.
- **Expose a release or image selector:** recreates cross-platform inconsistency and contradicts the
  Debian-only product contract.
- **Keep creating Bookworm during the transition:** adds new migration debt after the safe path
  exists.
- **Treat `vm backup` as rollback:** no boot disk, system state, or restore command exists.
- **Create provider snapshots automatically but leave restore/cleanup manual:** Agentworks would own
  costly residual resources without a complete lifecycle.
- **Run apt directly over SSH or under `/tmp`:** a disconnect or Trixie reboot can erase the control
  state.
- **Automatically re-enable every third-party source:** vendor support cannot be inferred from a
  codename.
- **Build a generic multi-release upgrader now:** only one adjacent transition is researched and
  testable; the target profile's upgrade-from-previous policy preserves extension without creating
  an arbitrary release graph.

## Source quality

All external technical sources are primary project, vendor, or provider documentation. Debian's
release notes are authoritative for the distribution transition. Provider catalogs are authoritative
for current image selectors. Third-party repository instructions are authoritative for their suite
values. Agentworks findings come from the current repository rather than issue or review prose.
