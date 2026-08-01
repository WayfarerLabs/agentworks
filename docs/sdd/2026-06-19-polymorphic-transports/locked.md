# Polymorphic transports -- Lockfile

## 2026-06-24

All plan items shipped via PR #130. Two small post-merge follow-ups landed alongside: PR #131
(operation-level tracebacks now land in the per-op `SSHLogger` log instead of the shared
`error.log`) and PR #132 (agent's `~/.agentworks-rc.sh` is written unconditionally to match the
admin pattern). Issue #113 -- SSH ControlMaster on managed Host blocks -- shipped on top of all this
via PR #134; on Linux/macOS it multiplexes the dozens of SSH calls each VM/agent lifecycle op
issues, on Windows it's gated off (OpenSSH bug).

See [plan.md](plan.md) for the full per-phase detail. These specs are accurate as of this date and
are now locked.

## 2026-07-31: Azure attach/detach mechanism retired

The Azure public-IP detach mechanism described in `frd.md`, `hla.md`, and `plan.md`
(`attach_public_ip` / `detach_public_ip` and the detach-on-ready `post_tailscale_ready` hook) was
retired. Driver: Microsoft is retiring default outbound access, which made VMs with a detached
public IP go offline. Azure VMs now keep their public IP for the VM's whole lifetime; the NSG
carries a permanent deny-all-inbound baseline, and SSH ingress happens only through an ephemeral
allow rule scoped to the operator's egress IP, opened for bootstrap and for each native route
(`transient_route`) and deleted after (post-Tailscale, on create failure, and on route exit). The
hook and route seams this SDD introduced are unchanged; only Azure's implementation behind them
changed. Living reference: `cli/agentworks/plugins/azure/platform.py`,
`cli/agentworks/plugins/azure/network.py`, and ADR 0003.
