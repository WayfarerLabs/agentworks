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

The two hooks this SDD introduced now take the op-start `RunContext`:
`transient_route(vm, ctx, *, config=None)` and `post_tailscale_ready(vm, ctx)`, alongside
`native_transport(vm, ctx, *, config=None)` and a matching keyword-only `ctx` on the
`agentworks.transports.native_transport` factory. (The `#341` `secure_failed_vm` hook, added the day
after this and covered in the next entry, takes `ctx` for the same reason.) The hooks' shape,
lifecycle, and the `transient_route` / `post_tailscale_ready` asymmetry this SDD's HLA argues for
are otherwise unchanged; only the parameter list moved. The reason is that Azure's route open/close
and its transport builder are backend calls, so on a site with explicitly configured credentials
they need the same scoped secret delivery every op gets, with no ambient fallback. See the
2026-07-30 addendum in `docs/sdd/2026-07-01-vm-sites/locked.md` for the full note.

## 2026-07-31: Azure attach/detach mechanism retired

The Azure public-IP detach mechanism described in `frd.md`, `hla.md`, and `plan.md`
(`attach_public_ip` / `detach_public_ip` and the detach-on-ready `post_tailscale_ready` hook) was
retired. Driver: Microsoft is retiring default outbound access, which made VMs with a detached
public IP go offline. Azure VMs now keep their public IP for the VM's whole lifetime; the NSG
carries a permanent deny-all-inbound baseline, and SSH ingress happens only through an ephemeral
allow rule scoped to the operator's egress IP, opened for bootstrap and for each native route
(`transient_route`) and deleted after (post-Tailscale, on create failure via the new
`secure_failed_vm` hook, and on route exit). The hook and route seams this SDD introduced are
unchanged; only Azure's implementation behind them changed, and the `ctx` threading from the
2026-07-30 addendum above carries through to the NSG calls. Living reference:
`cli/agentworks/plugins/azure/platform.py`, `cli/agentworks/plugins/azure/network.py`, and ADR 0003.

## 2026-08-02: `interactive()` is now concrete over an abstract `_interactive()`

`hla.md` presents `interactive()` as one of the `Transport` ABC's abstract members. It is now a
concrete template method on the ABC that wraps a new abstract `_interactive()`, which is what the
four transports implement. The surface callers use is unchanged: `interactive(command, env=...)`
still returns the process exit code and still does not raise on remote-command failure.

Driver: an interactive attach hands the operator's local terminal to a remote full-screen program
(tmux), which reconfigures it with DECSET sequences and only undoes them on a clean detach. A
connection that dies mid-attach leaves the operator's terminal holding mouse reporting, the
alternate screen, and raw-mode line discipline. The fix has to wrap every interactive path, and
there are five attach call sites across consoles, sessions, and the agent/VM shells. Making the
wrapper the concrete member means no call site and no future transport can bypass it. See
`cli/agentworks/terminal.py` and the ABC-level tests in `cli/tests/transports/test_abc.py`.
