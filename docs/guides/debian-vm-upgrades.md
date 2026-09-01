# Upgrading a Debian VM

Agentworks creates VMs on one current Debian release. It keeps the immediately previous release
operable and supports adopting it after an operator-led upgrade to current, but Agentworks does not
run the Debian upgrade or create a provider recovery artifact.

For Trixie, the supported operator path starts from Bookworm. A VM two or more recognized releases
behind remains usable on a best-effort basis with warnings, but create a current VM and copy its
data instead of attempting a chained upgrade.

## Understand the boundary

Three systems hold different state:

- the provider holds the VM disks and machine definition;
- Debian holds package, boot, network, and service state inside the guest; and
- Agentworks holds VM declarations plus related agents, sessions, consoles, and workspaces.

A provider snapshot or disk restore covers only the provider side. It does not roll the Agentworks
database back. An Agentworks VM backup covers metadata, owned resources, and selected files, but it
cannot restore a provider VM's boot disk.

Plan recovery for both sides before changing Debian. Agentworks cannot make those systems one atomic
transaction and does not claim automatic rollback.

## Before you start

Use an Agentworks build that recognizes the target release. Confirm the VM's recorded state:

```console
agw vm describe NAME
```

Stop every Agentworks session on the VM and verify the list. A distribution upgrade and reboot will
terminate live shells and processes:

```console
agw session stop --all --vm NAME
agw session list --vm NAME
```

Named console definitions remain in Agentworks, but their live tmux state and console-only processes
will not survive a reboot. Stop application workloads according to their own runbooks.

Create two distinct backups:

1. Run `agw vm backup NAME` for Agentworks metadata, resources, and selected files.
2. Create a provider-native snapshot, image, export, clone, backup, or equivalent that can restore
   the VM and its boot disk on that platform.

Verify the provider artifact and the actual restore procedure. Ensure you have provider console or
other out-of-band recovery access in case SSH or Tailscale does not return. Provider permissions,
storage charges, consistency rules, and restore mechanics remain platform responsibilities.

Read the complete release notes for the exact transition before proceeding. For Bookworm to Trixie,
use Debian's
[upgrade instructions](https://www.debian.org/releases/trixie/release-notes/upgrading.en.html) and
[issues to be aware of](https://www.debian.org/releases/trixie/release-notes/issues.en.html).
Debian's guidance, not this document, is authoritative for package sources, package preparation,
space, upgrade ordering, reboot, and release-specific blockers.

## Perform the Debian upgrade

Follow Debian's procedure directly on the guest. Use a durable terminal and keep the provider
console available. Do not run `vm reinit` in the middle of the distribution transition;
release-aware Agentworks resources should converge only after Debian reports one coherent recognized
release.

After the reboot, verify at least:

- `/etc/os-release` reports the expected Debian codename and version;
- the package database is healthy;
- SSH and Tailscale are reachable;
- required services and workloads start; and
- the recovery artifact remains available until you are satisfied with the result.

Agentworks has no upgrade-resume journal. If Debian fails partway through, use Debian's recovery
guidance and your provider-native artifact. Do not ask Agentworks to adopt a mixed or unrecognized
guest.

## Adopt the live release in Agentworks

Once Debian is healthy, run:

```console
agw vm confirm-release NAME
```

The command connects to that named VM, reads `/etc/os-release`, and shows both the recorded and live
releases. It accepts only a release recognized by the installed Agentworks build. If the values
differ, it asks before changing the database. `--yes` or `-y` skips that confirmation.

`vm describe` may perform its existing live status and resource checks, but its Debian fields show
the last recorded release observation and timestamp. They do not probe the live Debian release or
prove that the record is still current. Run `vm confirm-release` whenever you need to refresh that
observation or suspect the guest changed outside Agentworks.

On a confirmed change, Agentworks performs one local transaction: it records the live release and
marks initialization pending. It does not reinitialize automatically. This is intentional because
the observation remains true even if later convergence fails.

Next run:

```console
agw vm reinit NAME
```

Reinitialization selects release-specific APT resources from the verified live release and converges
Agentworks-managed state. If it fails, repair the reported cause and rerun `vm reinit`. The VM
remains recorded at its truthful live release with initialization pending until convergence
succeeds.

Running `vm confirm-release` when recorded and live releases already match only refreshes the
observation time. It does not change initialization state or require confirmation.

## Restoring the provider artifact

Use the provider's native procedure. Agentworks neither lists nor restores provider snapshots.

After restore, verify the guest before using it. A provider disk image may now contain an older
Debian release while the Agentworks database still contains agents, sessions, consoles, workspaces,
or declarations created after the snapshot. Reconcile those objects and application data explicitly;
`confirm-release` cannot make the database match a historical disk image.

If the restored guest reports a recognized release, adopt the backward change through the same two
commands:

```console
agw vm confirm-release NAME
agw vm reinit NAME
```

Keep or delete the provider recovery artifact according to the platform's retention, billing, and
operational policy only after the restored or upgraded VM is verified.

## What Agentworks deliberately does not do

- choose an operator-specified Debian target;
- run `apt`, rewrite Debian sources, or reboot as an upgrade workflow;
- create, inventory, restore, or delete provider checkpoints;
- stop sessions or workloads automatically;
- roll back the Agentworks database with a VM disk;
- automatically call `vm reinit` after confirming a release; or
- support a direct or chained upgrade from current-2.

Those boundaries keep the durable release record useful without presenting provider recovery and a
remote package transition as a portable atomic operation.
