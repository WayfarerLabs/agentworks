# Prior Art: Debian Release Transition

- Date: 2026-08-28
- Amended: 2026-09-01
- Scope: Debian release identity and operator upgrade guidance, current provider images, and
  Agentworks lifecycle seams

## Executive summary

Debian documents a direct Bookworm-to-Trixie upgrade, including preparation, package transitions,
reboot, and recovery. That establishes a valid operator path, but it does not make a cross-provider
upgrade orchestrator small or safe. Agentworks would need to own package-manager interruption,
remote recovery, provider snapshot semantics, and relational state that a disk restore cannot roll
back coherently.

The corrected design uses the narrower parts that generalize safely:

- one ordered Debian release registry;
- Trixie-only creation through platform release maps;
- core live release attestation and durable observation;
- release-keyed APT values;
- explicit `vm confirm-release` after an operator changes or restores a guest; and
- separate `vm reinit` convergence.

Agentworks does not ship `vm upgrade` or a checkpoint product. Provider backup and recovery remain
operator responsibilities. The substantial checkpoint research performed during this effort is a
rejected design record, not a reason to retain the abstraction.

## Debian's supported path

### Direct upgrades are release-specific

Debian supports upgrading to Trixie from Debian 12 Bookworm and requires the source system to be
fully current first. Its notes cover backups, remote recovery access, package database checks,
unofficial sources, package removals, disk capacity, a minimal upgrade, a full upgrade, and reboot.

Design disposition:

- Agentworks documents only the supported previous-to-current operator path.
- The ordered profile contains release identity, not Debian package commands or a transition graph.
- A future promotion adds its profile and mappings, while operators follow that release's own notes.
- Legacy current-2 guests are not bricked, but Agentworks recommends a new current VM plus data copy
  instead of a chained upgrade.

Source:
[Debian Trixie upgrade notes](https://www.debian.org/releases/trixie/release-notes/upgrading.en.html)

### Recovery is platform and workload specific

Debian recommends a full backup, or at least irreplaceable data and configuration, and an
out-of-band recovery route for remote systems. A new kernel or network configuration can make SSH
unavailable. Debian also identifies package and workload cases requiring operator judgment.

Design disposition:

- The operator creates and verifies a provider-native recovery artifact before upgrading.
- Agentworks does not label its metadata/workspace backup as a bootable rollback.
- Agentworks does not automate package, source, debconf, or conffile decisions.
- After external success, Agentworks observes the fact and converges its own declarations.

Sources:
[upgrade preparation](https://www.debian.org/releases/trixie/release-notes/upgrading.en.html#preparing-for-the-upgrade),
[issues to be aware of](https://www.debian.org/releases/trixie/release-notes/issues.en.html)

### Trixie changes relevant to ordinary Agentworks operation

The release notes identify several creation and initialization risks:

- `/tmp` becomes tmpfs by default;
- DSA SSH keys are removed;
- sshd no longer reads `~/.pam_environment` by default;
- `/etc/sysctl.conf` is no longer read by `systemd-sysctl`, while drop-ins remain supported;
- network interface names can change; and
- Debian is moving toward deb822 APT sources.

Agentworks already writes sysctl drop-ins. Its size-unbounded backup and workspace-copy archives did
stage under `/tmp`, so those move to private disk-backed `/var/tmp` paths. The remaining items are
covered by creation and initialization certification rather than an upgrade state machine.

Source:
[Trixie issues to be aware of](https://www.debian.org/releases/trixie/release-notes/issues.en.html)

### Support remains relative

Debian publishes regular-support and LTS dates, but those dates do not define a second Agentworks
support clock. Agentworks has one current release, supports ordinary operation and the documented
operator upgrade path for previous, and treats older recognized guests as best effort with warnings.

Sources: [Bookworm release information](https://www.debian.org/releases/bookworm/),
[Trixie release information](https://www.debian.org/releases/trixie/)

## Official Trixie image selectors

Official Trixie artifacts exist for every bundled platform. Proxmox differs because it has no
provider-wide catalog: the operator builds and maps a template, while core still verifies the cloned
guest.

| Platform | Trixie selector                                                    | Product mapping                      |
| -------- | ------------------------------------------------------------------ | ------------------------------------ |
| AWS EC2  | SSM path below `/aws/service/debian/release/13/latest`             | release then architecture            |
| Azure VM | `Debian:debian-13:13-gen2:latest`                                  | complete image record and disk floor |
| GCP GCE  | project `debian-cloud`, families `debian-13` and `debian-13-arm64` | release then architecture            |
| WSL2     | Docker Official Image `debian:trixie`                              | release tag, cache, and diagnostics  |
| Lima     | Debian 13 generic cloud images                                     | release then architecture            |
| Proxmox  | operator-built template                                            | `template_vmids.trixie`              |

Sources:

- [Debian AWS Trixie images](https://wiki.debian.org/Cloud/AmazonEC2Image/Trixie)
- [Debian Azure images](https://wiki.debian.org/Cloud/MicrosoftAzure)
- [Google Compute Engine operating-system details](https://cloud.google.com/compute/docs/images/os-details)
- [Docker Official Debian image](https://hub.docker.com/_/debian/)
- [Debian Trixie cloud images](https://cloud.debian.org/images/cloud/trixie/latest/)
- [Lima templates](https://lima-vm.io/docs/templates/)
- [Proxmox VE administration guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)

Design consequences:

- core sends one concrete release and exposes no selector;
- each platform owns the mapping from that release to its native artifact;
- a missing mapping fails before mutation and never falls back; and
- core independently reads `/etc/os-release` from the created guest.

## Third-party APT values

A host release and a vendor repository suite are different facts. Release-aware resources must use
the vendor's published value rather than replace every occurrence of `bookworm` with `trixie`.

| Source         | Bookworm value | Trixie host value | Disposition                                       |
| -------------- | -------------- | ----------------- | ------------------------------------------------- |
| HashiCorp      | `bookworm`     | `trixie`          | release map                                       |
| tofuutils/tenv | `bookworm`     | `trixie`          | release map                                       |
| ngrok          | `bookworm`     | `bookworm`        | reviewed vendor exception, dated in resource YAML |
| GitHub CLI     | `stable`       | `stable`          | release-neutral scalar                            |
| NodeSource     | `nodistro`     | `nodistro`        | release-neutral scalar                            |

Sources: [HashiCorp installation](https://developer.hashicorp.com/terraform/install),
[tofuutils Cloudsmith setup](https://cloudsmith.io/~tofuutils/repos/tenv/setup/),
[ngrok Linux installation](https://ngrok.com/download/linux)

The initializer selects from the VM's verified observed release. A map is explicit policy and may
intentionally contain the same suite for two host releases.

## Provider recovery research and rejection

The six platforms expose materially different recovery primitives:

- AWS snapshots an EBS volume and restores a root volume through an instance task;
- Azure snapshots and swaps managed OS disks;
- GCP snapshots, creates a new disk, and reattaches a boot disk;
- WSL exports and destructively re-registers a distribution;
- Proxmox exposes QEMU snapshot and rollback only when storage supports it; and
- Lima's behavior depends on its driver. Lima 2.2.0 VZ does not implement native snapshots, while
  clone-based recovery introduces host-placement, additional-disk, incomplete-clone, and atomic
  rename concerns.

Sources:
[AWS EBS snapshots](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-creating-snapshot.html),
[AWS root-volume replacement](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/replace-root.html),
[Azure OS disk swap](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/os-disk-swap),
[GCP snapshot restore](https://cloud.google.com/compute/docs/disks/restore-snapshot),
[WSL lifecycle commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands),
[Lima snapshot create](https://lima-vm.io/docs/reference/limactl_snapshot_create/),
[Lima VM types](https://lima-vm.io/docs/config/vmtype/),
[Lima 2.2.0 VZ snapshot implementation](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/vz/vz_driver_darwin.go#L565-L578),
[Proxmox administration guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)

A common create/list/restore/delete API can hide method names, but it cannot make these operations
equivalent. It also cannot roll back Agentworks relational state. Restoring a VM disk after agents,
sessions, consoles, workspaces, or declarations changed would restore only one side of a distributed
state boundary.

That mismatch is decisive. A general checkpoint object is not valuable enough by itself, and using
it only to justify an assisted upgrade makes the upgrade much larger. The shipping design therefore
removes all checkpoint capability methods, provider implementations, database models, and CLI
commands. Provider-native recovery remains an explicit operator prerequisite.

## Existing Agentworks seams

The retained design drafts on existing boundaries:

- `ProvisionRequest` is the one shared input to every vm-platform create;
- `ProvisionResult.native_transport` gives core an independent attestation route;
- `VMRow` and the database repository hold durable observed state;
- the existing initialization status already represents pending convergence;
- Phase B already applies declarative APT resources;
- named VM boundaries activate and connect to one selected VM; and
- doctor is designed as a bounded readiness report, not a fleet-wide remote scanner.

The new surface is therefore one narrow command, `vm confirm-release`, rather than an upgrade
subsystem. It separates observing an external change from converging Agentworks state and avoids
claiming atomicity across provider recovery, Debian package state, and the Agentworks database.

## Rejected alternatives

### Continue creating Bookworm

Rejected. It creates new legacy debt after Trixie is stable and makes later promotions harder.

### Let platforms decide current

Rejected. Different plugin versions could silently create different Debian releases. Core sends the
concrete value and verifies it.

### Expose OS, release, or image selection

Rejected. It creates multiple product truths and conflicts with the Debian-only, one-current-release
manifesto.

### Automatically adopt release drift

Rejected. A changed live release is material operator state. Ordinary operations fail with a clear
path to explicit confirmation.

### Have `confirm-release` call `reinit`

Rejected. If reinit fails, the release observation is still true. Atomic observation plus pending
initialization represents that outcome without pretending the remote operation and local database
can be one transaction.

### Add a new `reinit required` VM state

Rejected. Existing `InitStatus.PENDING` already represents incomplete convergence and has
established retry behavior.

### Scan every VM from doctor

Rejected. It turns a local diagnostic into a potentially slow, activating network fan-out and can
wait on unavailable WSL or remote VMs. Live observation belongs to an explicit named command.
