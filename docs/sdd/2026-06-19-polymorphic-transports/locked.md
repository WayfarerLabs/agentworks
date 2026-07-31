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

## Addendum: 2026-07-30 (issue #199)

The two hooks this SDD introduced now take the op-start `RunContext`: `transient_route(vm, ctx)` and
`post_tailscale_ready(vm, ctx)`, alongside `native_transport(vm, ctx, *, config=None)` and a
matching keyword-only `ctx` on the `agentworks.transports.native_transport` factory. The hooks'
shape, lifecycle, and the `transient_route` / `post_tailscale_ready` asymmetry this SDD's HLA argues
for are otherwise unchanged; only the parameter list moved. The reason is that Azure's attach/detach
and its transport builder are backend calls, so on a site with explicitly configured credentials
they need the same scoped secret delivery every op gets. See the 2026-07-30 addendum in
`docs/sdd/2026-07-01-vm-sites/locked.md` for the full note.
