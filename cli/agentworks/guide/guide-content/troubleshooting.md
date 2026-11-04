---
description: Narrow an Agentworks failure to configuration, readiness, state, or connectivity.
index-order: 40
---

# Troubleshooting

Start with the exact error from the command that failed. Run `agw doctor` once for a broad check of
the workstation, configuration, dependencies, and database; then move to the smallest relevant
surface instead of changing several things at once.

For a disabled or not-ready resource, use `agw resource show KIND/NAME` to inspect its focused
condition, direct relationships, current live uses, and attributable diagnostics together. Use
`agw resource explain KIND` or `agw resource explain KIND/NAME` when the question is what a kind or
capability accepts.

## VM and SSH connectivity

`agw vm describe NAME` shows the recorded power state, Tailscale address, recent events, current
resolved declaration, lifecycle evidence, and structural drift. Its hardware evidence is the
recorded provisioning request, not provider-observed realized hardware. `agw doctor` reports SSH
identity drift, orphaned instance records, malformed known records, and well-formed records from a
newer release. `agw vm verify-connection NAME` tests the canonical admin connection without starting
the VM. If that succeeds, `agw vm shell NAME` opens the Agentworks-managed SSH path.

For raw SSH, the default alias is `ssh awvm--NAME`. Run `agw config sync-ssh-config` if the
generated entry is stale; installations may configure a different alias prefix.

A message that the OpenSSH client is unsupported means `ssh` on the workstation is missing or older
than 8.5; `agw doctor` shows which client Agentworks resolved, and on Windows only the native
Win32-OpenSSH is used. A message that a VM cannot run Agentworks requests means `python3` or its
standard library is missing there; `agw vm recover NAME` installs it.

A message that a VM has no recorded host key is fixed with `agw vm import-host-keys NAME`; see its
`--help` for a VM with no matching entry. On a running VM, `agw vm recover NAME` can instead read
the keys through the platform and record them once you confirm their fingerprints against the VM's
console.

When the failure appears to be below SSH, use `tailscale status` to confirm the workstation is
connected and can see the VM, then `tailscale ping HOST` with the Tailscale address from
`agw vm describe NAME`. `agw vm logs NAME` shows the VM's recent SSH logs.

When a VM does not answer after `agw vm start`, or Tailscale itself needs repair, run
`agw vm recover NAME` on the running VM. It works through the platform's own channel, without
Tailscale, and changes the VM: it restores your SSH key, installs `python3` if missing, restarts a
broken SSH server where the channel does not use it, and rejoins Tailscale with the VM template's
auth key when the node needs a login, then waits for SSH. A step that fails stops it and names
`agw vm logs NAME`. If you would rather not change the VM, delete and recreate it instead.
`agw vm shell NAME --platform` remains an interactive escape hatch where the platform offers one.

## Git authentication

First inspect the selected `git-credential` and its provider with `agw resource show` and
`agw resource explain`. A secret source reports failures during resolution or optional runup. A
`gh-cli` or `az-cli` source receives a read-only readiness check in that user's login/interactive
shell during enabled runup, but a warning does not block helper installation. The check can run
before later install/profile steps, so verify again after initialization when those steps may add
the CLI. Confirm that the matching CLI is on that user's `PATH` and authenticated as the intended
identity. Azure DevOps also requires that identity to have access to the configured organization and
repository. Final credential acquisition still happens only when the target user's Git operation
invokes the helper.

Fixing CLI authentication takes effect on the next Git operation because the helper reacquires each
time. After changing a manifest or a template's `git_credentials`, reinitialize the VM admin or
agent so Agentworks can rebuild the complete managed state. Removing every declared credential and
reinitializing removes Agentworks-owned credential state.

After one change, rerun the narrow check that exposed the problem. Use
`agw guide show concept-reporting-bugs` when the failure needs to be reported.
