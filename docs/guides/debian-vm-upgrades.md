# Upgrading a Debian VM

Agentworks creates VMs on one current Debian release and supports one in-place transition: the
immediately previous release to the current release. `agw vm upgrade NAME` selects that transition
from Agentworks' release registry. It does not accept a target release, skip releases, downgrade, or
run a chained upgrade.

A VM two or more releases behind remains usable on a best-effort basis, with support warnings, but
cannot enter this workflow. Create a current VM and copy its workspaces and data instead.

## Before you start

Stop every Agentworks session on the VM and resolve any package-manager work already in progress.
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

Create a recovery artifact that can boot outside Agentworks before authorizing package changes.
Depending on the platform, that can be a provider snapshot, WSL export, Proxmox backup, or
equivalent. Know how to reach the provider console or rescue environment and how to restore the
artifact. Agentworks records the reference you supply, but it does not create, validate, retain, or
restore the artifact.

```console
agw vm upgrade build-1 --checkpoint snapshot-2026-08-29
```

Omitting `--checkpoint` prompts for the reference. The command separately asks you to attest that
the named artifact exists and that console or rescue access was tested. Supplying the option does
not skip that attestation. The existing `vm backup` and the Debian recovery bundle created by the
command are data-recovery artifacts, not checkpoints that can boot a VM.

## What the command does

The workflow has three authorization points:

1. It records the external checkpoint reference and asks you to attest that the artifact and
   independent recovery access exist.
2. It completes the ordinary VM backup and Debian recovery bundle, then asks permission to bring the
   source release fully current without changing suites.
3. It reopens the VM operation boundary, repeats the complete preflight, highlights material drift,
   shows the final plan, and asks permission to switch Debian suites.

After the second confirmation, Agentworks preserves and disables the existing APT source files,
writes the target release's canonical Debian sources, runs the documented minimal and full upgrades,
and verifies the target kernel is installed on guest-kernel platforms. It uses the installed target
udev rules to predict post-reboot interface names and refuses to reboot when a rename could strand
the VM. WSL2 instead records and verifies its provider-managed interface names across the clean
systemd shutdown and distribution restart.

The reboot is a separate durably recorded action. Agentworks requires a changed boot ID, verifies
the predicted interface names, and first tries the canonical Tailscale SSH route. If that route does
not return and the platform provides a native transport, it uses that route to rejoin Tailscale with
the key resolved before mutation. Proxmox has no post-create native transport, so its recovery path
uses the Proxmox console and the recorded backup.

As soon as `/etc/os-release` proves the target release, Agentworks records it in the database. It
then verifies the package database, target APT convergence and sources, running guest or WSL
provider kernel, systemd, sshd, Tailscale, and Agentworks identities before rerunning release-aware
VM initialization. Automatic APT timers are restored to their prior states only after the healthy
target and initialization complete. A source-safe early failure also attempts restoration. If that
restoration cannot be verified, all known timers remain stopped during reconfiguration and the VM
gets a durable repair-required event.

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

Use the retained `upgrade.log`, Debian package logs, recovery bundle, checkpoint reference, and
platform console when the command reports `repair-required`. If no canonical or native route works,
do not start another upgrade process. Repair connectivity through the console or restore the
external checkpoint. A target release already proved by the guest remains recorded even when later
health checks or initialization still need repair.
