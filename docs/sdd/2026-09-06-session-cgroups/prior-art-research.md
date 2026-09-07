# Session Cgroups: Prior-Art Research

- Date: 2026-09-06.
- Scope: source/documentation research for the [HLA](hla.md), not live validation.

## Findings and design consequences

Cgroup v2 membership is inherited on fork and survives executable replacement and terminal changes.
Moving a parent does not move existing descendants. The kernel exposes live group population and a
whole-tree kill operation that handles concurrent forks. These facts support launch-before-user-code
containment, group-level completion evidence, and rejection of retroactive parent-only migration.
[Linux 6.1 cgroup v2 documentation](https://www.kernel.org/doc/html/v6.1/admin-guide/cgroup-v2.html).

Systemd supports transient services, cgroup ownership, and delegation. Services launch their own
processes; scopes register processes launched elsewhere. Delegating a subtree to a UID gives that
UID access to its management files, which does not separate a trusted controller from hostile
workloads sharing the UID. Prefer a system-owned service running its payload as the agent user.
[Systemd delegation](https://systemd.io/CGROUP_DELEGATION/).

Bookworm's systemd supports whole-group TERM followed by KILL after a stop timeout. Its service
model can anchor lifetime to a main process. This supports independent cleanup when the tmux runtime
ends; lifetime-until-last-descendant would instead let a rogue descendant keep a run alive.
[Bookworm systemd.kill](https://manpages.debian.org/bookworm/systemd/systemd.kill.5.en.html),
[Bookworm systemd.service](https://manpages.debian.org/bookworm/systemd/systemd.service.5.en.html).
The actual stop implementation must still be tested under concurrent forking on both releases.

Debian's release comparison lists Bookworm's kernel/systemd baseline as 6.1/252 and Trixie's as
6.12/257. No Trixie-only feature is necessary for the proposed cgroup foundation. WSL2 supplies its
own kernel, so runtime feature checks remain necessary. This establishes feasibility of the
foundation, not compatibility of a security profile that has not yet been built.
[Debian release comparison](https://www.debian.org/releases/trixie/release-notes/whats-new.en.html).

Unix socket peer credentials describe connection-time identity; descriptors can be passed between
processes. They are useful input to an identity lookup but not a complete per-request permission
protocol. The identity LLD must close attribution and PID-reuse races without assuming modern
peer-pidfd options are present on Bookworm.
[Linux Unix socket API](https://man7.org/linux/man-pages/man7/unix.7.html).

## Refuted or do not rely on

- Environment variables, session names, and numeric PIDs are not authenticated run identity.
- Terminal/process-group cleanup and parent-death signals do not enumerate a durable hostile
  descendant set. Use cgroup ownership rather than growing process-tree traversal.
- "Outside the session group" is not a privilege boundary between processes with the same UID.
- A cgroup namespace alone does not block access to all outside execution services.
- Killing tmux does not prove detached descendants gone. See the current teardown at
  `cli/agentworks/sessions/manager/_lifecycle.py:269` at baseline
  `b22cc49c984aa91e8205e035c5766b5a2aede90a`.
- Root-owned groups do not prevent submission to another execution server. Current sibling sockets
  and writable agent SSH authorization are concrete paths the HLA must address.
- PID namespaces may complement isolation, but do not alone provide the proposed durable session/run
  registry and lifecycle integration. They are not ruled out as part of the proof gate.

## Open research

Prove a compatible restriction of same-user execution channels; select the tmux service anchor and
startup readiness flow; design a race-safe socket lookup on Bookworm; and demonstrate a safe legacy
transition. The [plan](plan.md) gates implementation on these results. No live VM or adversarial
experiment was performed for this checkpoint.

## Source quality

| Source                             | Quality and angle                                              | Limit                                                                                     |
| ---------------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Kernel 6.1 documentation           | Primary; membership, migration, kill, and delegation semantics | Does not establish a particular VM's configuration.                                       |
| Systemd upstream delegation        | Primary; service ownership and manager integration             | Current document includes features newer than Bookworm; consult release-specific manuals. |
| Debian Bookworm systemd manuals    | Primary packaged documentation; supported baseline behavior    | Documented semantics need adversarial runtime verification.                               |
| Debian release notes               | Primary; distribution comparison                               | WSL2 and customized kernels need direct feature probes.                                   |
| Linux man-pages Unix socket API    | Primary API documentation; credentials and descriptor transfer | A protocol still needs its own race and trust analysis.                                   |
| Repository baseline and issue #715 | Local implementation evidence and prior problem report         | Issue discussion is input; it supplies no authority or live proof.                        |
