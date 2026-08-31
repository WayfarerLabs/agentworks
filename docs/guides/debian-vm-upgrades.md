# Upgrading a Debian VM

Agentworks creates VMs on one current Debian release and supports one in-place transition: the
immediately previous release to the current release. `agw vm upgrade NAME` selects that transition
from Agentworks' release registry. It does not accept a target release, skip releases, downgrade, or
run a chained upgrade.

A VM two or more releases behind remains usable on a best-effort basis, with support warnings, but
cannot enter this workflow. Create a current VM and copy its workspaces and data instead.

## Before you start

Stop every Agentworks session on the VM and resolve any package-manager work already in progress:

```console
agw session stop --all --vm NAME
agw session list --vm NAME
```

The upgrade fails before package planning if a session is still running, broken, or cannot be
verified, and names the affected sessions. A broken session may require `--force`; Agentworks does
not stop sessions automatically as part of this read-only gate.

If the VM has named consoles, the command also warns that the required reboot ends their live tmux
state and any console-only shell processes. The named console definitions and session membership
survive, and the next `agw console attach` rebuilds the aggregate view.

The upgrade uses the VM's canonical SSH identity. If the VM predates SSH identity tracking, run one
successful `agw vm reinit NAME` while the configured key still works before starting the upgrade. If
it no longer works, restore the configured public key through a supported platform recovery
transport or provider-native recovery tooling, then reinitialize.

The command checks the recorded and observed Debian releases, package-database health, package
holds, kernel meta-package where the guest owns its kernel, OpenSSH version, APT pins and mixed
suites, modified package configuration files, partition-aware disk estimates, package removals,
third-party repositories, and transition-specific Debian release-note blockers. WSL2 uses the
Microsoft-managed WSL kernel, so Agentworks verifies that provider kernel instead of requiring a
Debian `linux-image` package inside the distribution.

The target package plan uses isolated scratch APT state and canonical target sources; it does not
load the guest's APT configuration fragments, hooks, preferences, or source files. Space estimates
are aggregated by the actual filesystems backing `/`, `/var`, the package cache, and `/boot`, so
shared mounts are counted together and separate root/`/var` filesystems each carry a conservative
installed-growth allowance.

Agentworks creates one managed, recoverable checkpoint immediately before the first package
mutation. It generates the checkpoint name, stops the VM for an offline capture, verifies the
provider artifact, and starts the VM again. The ordinary `vm backup` and Debian recovery bundle
remain separate data-recovery artifacts.

Each VM has one operational checkpoint slot. A checkpoint left by a completed upgrade is retained
until you explicitly delete it. Before starting a later upgrade, inspect and either retain or delete
that checkpoint:

```console
agw vm list-checkpoints --vm build-1
agw vm delete-checkpoint build-1
```

The checkpoint list separates provider lifecycle state from current restore eligibility. `ready`
means the provider artifact completed. `available` means live provider inventory still proves that
artifact, the current Agentworks declarations match the captured state, and managed restore is
allowed. `declarations-changed` means restore is blocked until you restore the matching Agentworks
database and declarations or replace the checkpoint. An interrupted restore reports
`resume-required`. If provider evidence cannot be proved, `vm describe` reports `unavailable` with a
diagnostic and ordinary checkpoint listing refuses the inconsistent inventory.

An unrelated checkpoint blocks a fresh upgrade instead of being replaced. A checkpoint for the same
release transition may be reused after a cancellation before package mutation or after an explicit
restore, provided the guest, database, upgrade journal, provider artifact, and captured desired
state still agree. Reuse output includes the original creation time so the operator can judge the
age of the retained recovery point.

### Provider prerequisites

Checkpoint creation must work before Agentworks mutates Debian. Existing least-privilege cloud
credentials may need checkpoint permissions added:

- AWS needs EBS snapshot create, describe, tag, and delete access; root-volume replacement task
  create and describe access; and volume describe, delete, `ec2:CreateTags`, and `ec2:DeleteTags`
  access. See
  [EBS snapshot creation](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-creating-snapshot.html)
  and
  [EC2 root-volume replacement](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/replace-root.html).
- Azure needs snapshot read, write, and delete; managed-disk read, write, and delete; and VM read,
  write, start, deallocate, and instance-view access. See
  [Azure OS disk swap](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/os-disk-swap).

GCP and Proxmox permissions are covered in their platform guides. Lima must use a driver that
supports snapshots, and WSL2 must have enough local storage for exports. Provider snapshots and the
emergency disk or export retained by destructive-restore platforms consume storage, quota, and
provider charges until `agw vm delete-checkpoint NAME` succeeds.

## What the command does

The workflow has two authorization points:

1. It first checks the database checkpoint slot and live provider checkpoint inventory. After that
   read-only check passes, it completes the ordinary VM backup and Debian recovery bundle, creates
   and verifies the managed offline checkpoint, then asks permission to bring the source release
   fully current without changing suites.
2. It reopens the VM operation boundary, repeats the complete preflight, highlights material drift,
   shows the final plan, and asks permission to switch Debian suites.

A fresh upgrade and a resume paused before suite switching require an interactive terminal. A
non-interactive invocation may continue a later stage whose required authorization is already
durable.

The command announces every preflight group and post-reboot verification stage. Long durable package
actions are shown as tracked source-update, source-switch, minimal-upgrade, and full-upgrade work,
so an expensive remote operation does not look stalled even when package processing takes time.

After the second confirmation, Agentworks preserves and disables the existing APT source files,
writes the target release's canonical Debian sources, runs the documented minimal and full upgrades,
and verifies the target kernel is installed on guest-kernel platforms. It uses the installed target
udev rules to predict post-reboot interface names and refuses to reboot when a rename could strand
the VM. WSL2 instead records and verifies its provider-managed interface names across the clean
systemd shutdown and distribution restart.

The reboot is a separate durably recorded action. Agentworks requires a changed boot ID, verifies
the predicted interface names, and first tries the canonical Tailscale SSH route. If that route does
not return and the platform provides a native transport, it uses that route to rejoin Tailscale with
the key resolved before mutation. Proxmox has no post-create native transport, so reconnect repair
uses the Proxmox console. The managed checkpoint is retained for an explicit restore if forward
repair is not appropriate.

As soon as `/etc/os-release` proves the target release, Agentworks records it in the database. It
then verifies the package database, target APT convergence and sources, running guest or WSL
provider kernel, systemd, sshd, Tailscale, and Agentworks identities before rerunning release-aware
VM initialization. Automatic APT timers are restored to their prior states only after the healthy
target and initialization complete. A source-safe early failure also attempts restoration. If that
restoration cannot be verified, all known timers remain stopped during reconfiguration and the VM
gets a durable repair-required event. Successful completion names the retained checkpoint, warns
that provider storage charges may continue, and shows the command that deletes it.

## Resume and recovery

Durable state lives on the guest at:

```text
/var/lib/agentworks/debian-upgrades/{source}-to-{target}/
```

The directory is root-owned and contains the plan, atomic state, original source files, upgrade
script, log, and one non-blocking lock. Unsafe symlinks, ownership, or permissions on journal-owned
paths stop the workflow for repair. Package actions run in a detached systemd unit. Losing the local
process or SSH connection does not erase intent or start a second package manager; attempt
identities also fence an older invocation from overwriting a newer retry.

Run the same command after an interruption:

```console
agw vm upgrade build-1
```

Agentworks inspects the active unit, native package locks, journal, and action postcondition. It
advances work already proved complete, safely retries only a retryable action, or stops with manual
repair guidance. Once suite switching starts, repair proceeds forward to the target release. There
is no automatic downgrade or provider rollback.

Use the retained `upgrade.log`, Debian package logs, recovery bundle, managed checkpoint, and
platform console when the command reports `repair-required`. If no canonical or native route works,
do not start another upgrade process. Repair connectivity through the console or explicitly restore
the checkpoint:

```console
agw vm restore-checkpoint build-1
```

Restore is destructive and requires the VM and all its Agentworks sessions to be stopped. Before
provider mutation, Agentworks verifies that the current effective VM declarations match the state
captured with the checkpoint. It then restores the provider artifact, briefly starts the VM to
attest and record the live Debian release, marks initialization for reconciliation, and returns the
VM to stopped state. Run `agw vm reinit build-1` before relying on guest convergence. Restore
retains the checkpoint; only `vm delete-checkpoint` frees the slot.

Checkpoint deletion normally proves provider cleanup before Agentworks releases the slot. If
provider inventory or cleanup remains unavailable, `agw vm delete-checkpoint NAME --force`
explicitly makes Agentworks forget the checkpoint so the VM is not permanently trapped in a failed
lifecycle. This does not prove or perform provider cleanup. The command warns with the known
provider identifier when one was recorded; late, incomplete, emergency, or additional provider
artifacts may remain and continue billing. Inspect and remove them with the provider's native tools.
Ordinary deletion never takes this escape path. `agw vm delete NAME --force` uses it only after its
checkpoint cleanup attempt fails, then continues the already-explicit forced VM deletion.

A target release already proved by the guest remains recorded even when later health checks or
initialization still need repair.
