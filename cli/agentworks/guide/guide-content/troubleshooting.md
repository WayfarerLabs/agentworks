---
description: Narrow an Agentworks failure to configuration, readiness, state, or connectivity.
index-order: 40
---

# Troubleshooting

Start with the exact error from the command that failed. Run `agw doctor` once for a broad check of
the workstation, configuration, dependencies, and database; then move to the smallest relevant
surface instead of changing several things at once.

For a disabled or not-ready resource, use `agw resource list --kind KIND --include-disabled` and
`agw resource explain KIND/NAME` to separate enablement from missing configuration or host
requirements.

## VM and SSH connectivity

`agw vm describe NAME` shows the recorded power state, Tailscale address, and recent events.
`agw vm verify-connection NAME` tests the canonical admin connection without starting the VM. If
that succeeds, `agw vm shell NAME` opens the Agentworks-managed SSH path.

For raw SSH, the default alias is `ssh awvm--NAME`. Run `agw config sync-ssh-config` if the
generated entry is stale; installations may configure a different alias prefix.

When the failure appears to be below SSH, use `tailscale status` to confirm the workstation is
connected and can see the VM, then `tailscale ping HOST` with the Tailscale address from
`agw vm describe NAME`. `agw vm logs NAME` shows the VM's recent SSH logs.

If Tailscale itself needs repair, `agw vm shell NAME --platform` provides a platform-native recovery
path where the selected platform supports one. It is an explicit recovery tool, not the routine
connection path.

After one change, rerun the narrow check that exposed the problem. Use
`agw guide show concept-reporting-bugs` when the failure needs to be reported.
