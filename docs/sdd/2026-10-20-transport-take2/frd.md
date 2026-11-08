# Transport Take 2: Functional Requirements

- Status: Requirements baseline accepted by the operator on 2026-10-20 and amended on 2026-10-23;
  HLA and plan follow. No design or implementation is claimed.
- Started: 2026-10-20
- Effort: `transport-take2`
- Supersedes: [transport-improv](../2026-09-12-transport-improv/locked.md) and
  [ssh-connection-contracts](../2026-09-05-ssh-connection-contracts/locked.md), whose FRDs are input
  to this one, not requirements of it.

## Purpose

After a platform finishes provisioning, every managed VM looks and behaves the same, and Agentworks
reaches all of them through one platform-agnostic transport: OpenSSH over the tailnet. Developers
run commands and scripts, move files, attach terminals and start detached work through that one
transport without knowing which platform built the VM.

Platforms keep only what is genuinely theirs: lifecycle, provider networking, availability holds and
whatever native access they need for their own provisioning. Core requires one native capability
from a platform, a way to recover canonical SSH access when it is broken; a platform may also offer
an interactive native shell.

## Why a new effort

The predecessor efforts treated every platform's native channel as a full execution target and
rebuilt the execution stack beside the old one, reaching into provider and platform code. Supporting
the tightest native channel limits (a Proxmox guest-agent request size, a Windows command-line
length) for every operation, recording guest and provider effects on the workstation, and gating
exposure on complete validation of every workflow produced a large unmerged stack with no shippable
increment. The operator paused both efforts and directed this reset.

## Operator rulings

Recorded verbatim. Context lines in brackets state the question each answer responded to.

### 2026-10-20

On the principle behind the reset:

> Tbh, I'm a bit unclear on why this got so big. I feel like we're making this more complex than it
> needs to be. Can you do an evaluation on that? And please try to think outside of the box. Given
> the way agentworks is set up, we can do things like having our own launcher program/helper scripts
> that are maintained on each VM, etc. And the whole principle is that after the platform is done,
> all VMs look and behave exactly the same. To the extent that isn't true, we should strive to make
> it so, not just paper over any differences.

[The lead asked five questions: allow an installed Agentworks program on the VM; make python3 and
systemd provisioning-time requirements; recalibrate the predecessor's requirements (build for real
callers rather than R11's absent-caller rule, defer the general job API, move file race hardening to
the permissions effort, allow reuse and incremental replacement instead of R10's no-legacy rule, and
replace the everything-validated-first gate with a thin slice); whether macOS Remote Lima is in
scope; and whether to write a new SDD.]

> 1. Yes, allow the Agentworks guest program. Maybe come up with a better name, though.
> 2. Yes, make python3 and systemd provisioning-time requirements, but maybe keep them in phase B if
>    possible (at least python3) to converge existing VMs?
> 3. Yes, agreed.
> 4. Yes, macOS remote lima is my primary platform. 100% in scope. I'm truly confused on why this
>    complicates things, though.
> 5. New SDD, lock the old one in the process.

On the native transport:

> Also, I think we're on the same page here, but I think part of the original flaw was wanting to
> have a full-blown native Transport.
>
> There's very little Agentworks itself needs to do via the native transport. It's basically just
> whatever it takes to recover Tailscale SSH. A native shell would be nice but it's not required and
> it certainly doesn't need to support seeding env vars, etc.
>
> Ofc platforms have their own native needs but we should leave that up to them. They know best.
> Imo, a standard interface there is just asking for trouble.
>
> The full-blown Transport should really only be over Tailscale SSH, at which point it really,
> really should be platform agnostic.

[The lead noted that the canonical path is installed OpenSSH to the VM's tailnet address, not
Tailscale's SSH server; that recovery also covers the admin's authorized key; that python3 can then
stay in Phase B only; and that core still needs one narrow recovery capability from each platform.
The operator answered:]

> Yeah, this all sounds great.

On the follow-up effort and resource limits:

[The lead recommended a separate, lean effort for session cgroups, reversing the 2026-09-18 ruling
that folded them into transport, and asked whether resource limits belong in it.]

> Yeah, that makes sense. And we've had a ton of issues with filesystems filling up so I do think
> resource limits are part of it.

[Asked what fills the disks:]

> The disks were filling up from a variety of sources but mostly subagent worktrees and caches. It
> was all "normal", though, in the sense that it was just agents and subagents doing their thing.
> Your generic wording is perfect. We'll figure that out in the follow up effort.

On locking the SSH effort as well:

> 1. Yes

On the name of the installed VM program:

> agw-atc it is. Fwiw, we also have our preflight and runup checks. That's about as far as I want to
> lean into that metaphor, though. I agree on keeping the vocab precise and neutral.

On building in parallel and how consumers reach the new transport:

> So are you sure about updating in place?
>
> It does still seem to me that building a new context-like object in parallel with the existing
> RunContext and enforcing no sharing of logic would get us to a cleaner place and wouldn't be that
> much extra work. But please push back if you disagree?

[The lead agreed with the parallel build and no shared logic, scoped to the execution layer, and
recommended new accessors on the existing `RunContext` rather than a second context object.]

> Ok. And I'm agreed on the accessor approach btw. Honestly I confused RunContext for Transport.

On deferring the installed program:

[The lead recommended introducing `agw-atc` in the follow-up containment effort, since nothing in
this effort needs an installed program.]

> As to your stuff, this all makes sense and I'm ok deferring agw-atc until we really need it.

On the review questions and delivery of this baseline:

[The lead asked five review questions (keep trust on first use for new VMs; move the Debian release
check onto SSH; defer the file allowlist; accept provider-native recovery or recreation for a broken
cloud SSH server; let the operator's agent sign for the configured identity) and three about the PR
(draft or ready; labels; whether open PR #831 merges first).]

> 1. Agreed. It generally happens via Agentworks during provisioning. The risk seems quite small.
> 2. Agreed. This should be close to the first thing that happens on the common path.
> 3. Agreed.
> 4. 100% agreed. While we should strive to clean up our messes, it's always going to be possible
>    for the machine to be broken enough that we can't fix it. At some point it's ok for Agentworks
>    to throw its hands up and ask the operator to fix the machine.
> 5. Agreed.
> 6. Ready
> 7. Let's do transport-take2 for the sdd
> 8. It should merge first I think. I'll work with the saga lead on that.

### 2026-10-23

On amendments from the published review of this baseline:

[The lead recommended five amendments, beyond rebasing after PR #831:

1. On Windows, support only the native OpenSSH for Windows client and refuse MSYS and Cygwin builds
   before dispatch, which the published review reported the operator had directed on 2026-10-18.
2. Restore, a sentence each, six predecessor guarantees dropped without a ruling: a typed refusal
   when an optional feature is absent; no administrative authority for an agent-bound target through
   native access; no silent blanking of interactive output by a secret-bearing environment; JSON
   null never meaning deletion, with malformed configuration rejected; no lost updates from
   cooperating writers; and passive accessors. List CA and revocation preservation, and the rules on
   modifying operator files, as not carried forward unless something uses them today.
3. Have platform provisioning perform the first Tailscale join and report the tailnet address,
   keeping R7 recovery only, with each platform declaring whether its native channel depends on the
   guest's SSH server.
4. Pass the 2026-09-18 protection-profile ruling and the 2026-09-19 containment ruling to the
   follow-up effort.
5. Have the lead check what waves 5 and 7 of the saga need before the operator rules on whether they
   depend on this effort.]

> 831 just merged.
>
> For the rest, I agree with your recommendations.

Applying the second, the lead found that Agentworks already writes its managed entries into operator
SSH configuration, so R8 keeps only the narrower rule that the trust import never modifies its
source file. Tailscale logout at deletion stays on the R7 capability, as in the 2026-10-20 baseline,
so R7 states it separately from recovery.

On two R8 questions raised by the project review of the amendments:

[The lead asked whether to adopt an Agentworks-owned, VM-bound host-key store, with brand-new VMs'
keys written only there and the managed `awvm--<vm>` SSH aliases verifying against it so plain `ssh`
keeps working, rather than keep writing the operator's address-keyed `known_hosts`. The operator
chose:]

> Adopt it (Recommended)

[The lead asked whether R8's rule that Agentworks' SSH ignores operator configuration should cover
only the transport's connections to VMs, leaving a platform's own SSH to its host or to a cloud VM's
public address free to honor operator configuration, as the Remote Lima host hop does today. The
operator chose:]

> VMs only (Recommended)

### Rulings carried forward

These predecessor rulings still hold, quoted from the transport-improv FRD.

On guest cancellation (2026-09-17):

> Yeah, I'm confortable deferring cancellation for now.

<!-- cspell:ignore confortable concious -->

On shell selection (2026-09-18):

> So totally agreed on the shell constants.

On staged delivery (2026-09-19). The three stages still hold, but the new surface no longer lands
ahead of its consumers: R10 lands it together with its first migrated consumers.

> In my (possibly naive) way of thinking, this implementation work ends with expanding RunContext to
> have this new surface in parallel with the old. This shouldn't break anything.
>
> Then there's another PR (or set of them) to migrate everything from old to new within RunContext
> (as well as direct calls if they exist). Finally there's a PR to rip out the old.

On permission timing (2026-09-19):

> Yeah, we're merely laying the groundwork for permissions. They shouldn't be enforced or otherwise
> relied upon until the old is removed. The only thing is that the consumers should make concious
> choices about what they should use for each operation.

The installed-OpenSSH minimum of 8.5, specified by the operator for the predecessor, also holds.

### Rulings superseded or handed on

The 2026-09-18 ruling placing cgroup work in transport is superseded by the 2026-10-20 rulings
above. Two containment rulings pass to the follow-up containment effort and do not bind this one:
the 2026-09-18 ruling that the execution boundary is an escalating set of protection profiles, and
the 2026-09-19 ruling that containment is an execution option rather than a session feature,
recorded in the saga's `target-state.md` and marked there as superseded by this reset. The follow-up
takes the session-cgroups snapshot preserved in the transport-improv directory as input.

## Scope

In scope:

- The VM contract every platform delivers, and its verification and convergence.
- One SSH transport for all post-provisioning execution, files, terminals, forwarding and detached
  work.
- The single native recovery capability core requires from each platform, replacing today's general
  native transport.
- Delivery of execution and file access to consumers through `RunContext`.
- Migration of every existing consumer, and removal of whatever the migration leaves unreachable.
- Amending ADR 0014 (SSH `SetEnv` delivery) and ADR 0025 (release probing over the native transport)
  where this effort retires the mechanisms they describe.

Outside this effort:

- `agw-atc`, session containment, resource limits and any cgroup requirement on the VM. These belong
  to the follow-up effort, which has their first callers.
- Permission enforcement, a core file-mutation allowlist and race-based path hardening. These belong
  to the future permissions effort.
- A standard interface for platforms' own native needs, Tailscale's SSH server, new cloud execution
  carriers such as provider run-command services, configuration-management frameworks, schedulers or
  workflow engines.

Explicitly not carried forward from the predecessors, beyond the recalibrations recorded on
2026-10-20: preserving CA policy and revocations, whether held in operator SSH configuration
(including separate KRL files) or as markers in known-hosts files. Agentworks configures neither
today; R8's import reports such markers rather than carrying them over.

## Users and outcomes

| User                     | Desired outcome                                                                                                                  |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| Core or plugin developer | One execution and file vocabulary that behaves identically on every VM.                                                          |
| Platform author          | Deliver a VM that passes the contract, plus one recovery capability and optionally a native shell; nothing else is standardized. |
| Operator                 | VMs that behave the same everywhere, and a reliable path back to SSH when it breaks.                                             |

## Requirements

### R1. One VM contract

After create or reinit, every managed VM satisfies one contract regardless of platform: a recognized
Debian release, systemd as init, the admin account and its sudo policy, Tailscale joined, OpenSSH
reachable on the tailnet, python3, and the base tools the transport depends on. Core checks the
contract over SSH at create and reinit, with the Debian release check close to the first step on the
common path. A VM that fails a check reinit can fix is not reported as ready, and the operator gets
an actionable diagnosis. Release support keeps the existing policy: current and previous releases
are ordinarily operable, and older recognized releases continue best effort with a warning.

Platform provisioning performs the VM's first Tailscale join, installs the admin's authorized key
and reports the VM's tailnet address, using whatever native access the platform needs. Core's work
on a new VM starts over SSH at that address.

A difference between platforms is fixed in provisioning or initialization, not carried as a branch
in core. Reinit converges an existing VM onto the contract. Differences inherent to a platform
(lifecycle and power, provider networking, availability holds such as WSL2 idle shutdown) stay
inside that platform's plugin.

### R2. One platform-agnostic transport

All Agentworks execution, file transfer, terminal attachment, port forwarding and detached work
after provisioning travels over installed OpenSSH to the VM's tailnet address. That transport
contains no platform-specific behavior. Ordinary operations never silently fall back to a native
channel. Requested forwarding has an explicit lifetime and reports setup failure. An optional
platform feature the platform does not offer, such as a native shell, is refused with a typed,
actionable error before credentials are resolved or anything is mutated.

A platform host, such as a Remote Lima host running macOS, is not a VM and is outside the VM
contract. A platform may reuse the SSH connection machinery for its own host access or native
channel; what it runs there is the platform's concern.

### R3. Commands and scripts

Literal arguments reach the guest exactly, including empty arguments, whitespace and shell special
characters, and literal execution applies no shell startup implicitly. Scripts select a fixed
interpreter (`sh`, `bash` or the execution user's default shell) explicitly, with login and
interactive startup as separate explicit choices. The default shell is the destination identity's,
never the workstation's, and a missing interpreter fails without substitution. A script and its
stdin are separate inputs.

Execution uses the bound identity; explicit elevation covers the whole operation, and elevation the
identity lacks is a permission failure, never a reroute to another identity or to native access, so
an agent-bound operation never gains administrative authority. Environment and working directory
apply to the whole operation in the target identity. Environment values arrive byte-exact, including
percent signs, quotes, `${...}` and newlines, on every supported workstation and OpenSSH client.

### R4. Input, output and results

Finite stdin, including sensitive stdin, is supported. Sensitive input, environment and script
content never appear in command arguments, logs, exceptions or retained artifacts. Buffered results
keep stdout, stderr and exit status distinct, without workstation newline conversion.

Results distinguish a guest command's exit status, including 255, from a failure to deliver or
observe the command. When completion cannot be established, the result says so rather than guessing.
A deadline bounds the local wait and does not imply remote termination. An ambiguous mutating
command is never dispatched again automatically; an explicitly read-only observation may retry.

Live streaming and terminal attachment remain available for sessions, consoles and interactive
commands. Binding a secret-bearing environment never silently suppresses an interactive operation's
output.

### R5. Files

Callers can upload and download bytes exactly, publish a file atomically on the destination
filesystem with its mode and ownership applied before publication, merge JSON configuration while
preserving unrelated values and shipped merge policies, and move the directory trees current
workflows need. A JSON merge keeps literal null values rather than treating them as deletion, and
rejects malformed existing configuration rather than replacing it. Read-modify-write never silently
loses updates from cooperating writers: a publish conditioned on the destination still holding what
the caller read checks and publishes atomically with respect to those writers; hardening against
hostile path races remains permissions work. Ordinary and elevated placement need no hand-written
copy, chmod or sudo wrappers. Failures and partial progress are reported honestly; cleanup touches
only what the operation created.

### R6. Detached work

Work that must outlive its connection survives disconnect. Its VM stays available while the
launching command or a later observing command runs, including through WSL2 idle shutdown; this
effort adds no hold that outlives every command. A later invocation can observe or resume a launch
by naming its recorded identity. An independent new request always launches fresh and never reuses
an earlier result because its command or paths match. Interrupting the command that launched the
work stops it, as interrupting a backup does today. This serves existing callers, starting with VM
backup; a general job API and general cancellation stay deferred.

### R7. Native recovery

Each platform supplies one recovery capability: run a core-owned script as root, once, without
depending on Tailscale. It takes no part in a successful create, which R1 assigns to provisioning.
It accepts finite sensitive stdin under R4's secrecy rules and returns the exit status and bounded
output, distinguished from delivery failure. A script may deliberately drop its own connection, as a
Tailscale rekey can; core then verifies the outcome over SSH where the script restores access.

Recovery restores canonical SSH access: Tailscale join, rejoin and rekey, the admin's authorized
key, and host-key trust for an existing VM that has lost its record, re-established from a key read
through the capability and confirmed by the operator. Each platform declares whether its native
channel depends on the guest's SSH server. Where it does not, recovery also repairs that server.
Where it does, as with SSH to a cloud VM's public address or `limactl shell`, a broken SSH server
needs provider-native recovery or recreation. On any platform, when recovery cannot restore access,
Agentworks stops and asks the operator to repair or recreate the machine.

Core also uses the capability for Tailscale logout when it deletes a VM. Logout removes the tailnet
path, so its result is the script's reported status, or indeterminate when the connection ends
first, never a later check over SSH. Apart from recovery, logout and the optional native shell, core
makes no use of a native channel.

The capability has no environment seeding, file API, detached work or streaming. Core invokes it
only explicitly. An interactive native shell is optional and platform-declared, and it does not seed
the operation environment. Everything else a platform does natively, including its own provisioning,
stays private to that platform.

### R8. SSH policy and trust

Every SSH and scp process the transport launches to a VM ignores operator and system SSH
configuration, offers only the configured identity, from its key file or the operator's default
agent, and requires the approved OpenSSH minimum. A platform's own SSH to its host or to a cloud
VM's public address is that platform's private access and may honor the operator's configuration, as
the Remote Lima host connection does today. Linux, macOS and Windows workstations are supported. On
Windows the supported client is the native OpenSSH for Windows build; MSYS and Cygwin builds,
including the one bundled with Git for Windows, are refused before dispatch.

Agentworks keeps VM host keys in its own store, bound to the VM rather than its address, so a
tailnet address change after rejoin or rekey still verifies against the VM's known key. The SSH
configuration entries Agentworks manages for its VMs verify against the same store, so the
operator's own `ssh` to a managed VM keeps working.

Existing trust lives today in the operator's known-hosts file, keyed by address and populated on
first use. It enters the Agentworks store only by an explicit import, which reads the default
known-hosts file or one the operator names, copies unmarked host-key records, hashed or not, binds
each to the VM whose address it matched, and never modifies the source file. Import reports any CA
or revocation marker that applies to a VM and imports no key for that VM, leaving it to R7's
re-establishment. Trust is never reset or silently widened. A missing record or unknown key never
makes an existing VM new. Only a VM Agentworks is creating has its host key accepted on first use,
at core's first connection after provisioning, and that key is written only to the Agentworks store.

### R9. Delivery to consumers

Capabilities receive execution and file access for admin and agent identities as separate
`RunContext` accessors, so each consumer chooses deliberately what it uses. Core code that uses a
transport directly today, outside a capability stage, moves to the same new transport without being
routed through `RunContext`. Constructing a context and calling its accessors connects to nothing,
starts no VM, opens no route and resolves no secret; that happens only when an operation runs. An
agent-bound accessor never yields administrative authority (R3). Fine-grained grants and their
enforcement are future permissions work.

### R10. Parallel build, migration and removal

The new transport is built alongside the current one. It does not import, wrap, subclass or call,
directly or indirectly, the current execution modules: `agentworks.transports`, `agentworks.ssh`,
`agentworks.remote_exec`, `agentworks.harness_setup.runner`, `agentworks.native_files` and
`agentworks.plugins.proxmox.transport`. Its tests pass with those modules unavailable. Useful code
and tests may be copied and adapted. The new surface lands together with its first migrated
consumers and their live evidence, never ahead of any caller. Remaining consumers move in batches,
each complete and live-validated on the platforms and workstations it affects. Migration is
internal: there is never an operator-selectable transport version or a second supported public
execution API.

The effort ends with:

- core's current SSH execution and command rendering, and the general `native_transport` platform
  method, deleted;
- core's remaining native uses outside R7, such as discovering a new VM's tailnet address in the
  first initialization phase, moved off the native channel;
- each platform's native access retained only as that platform's private code plus the R7
  capability;
- the predecessors' proof packages under `cli/agentworks/execution/` adopted into the new transport
  or deleted, with the unusable native carrier proof deleted;
- no consumer using a native channel as a general transport, and no core code building its own `ssh`
  or `scp` command lines outside the new transport and platforms' private access.

Incident-derived behavior in the current stack (closed stdin instead of a forced terminal, Windows
stdin newline handling, the Git for Windows toolchain dependency, and similar) is inventoried,
starting from the incident-derived behavior inventory in the
[transport-improv migration strategy](../2026-09-12-transport-improv/migration-strategy.md), and
re-proved against the new transport before the code carrying it is removed.

### R11. Evidence

Each landing carries live evidence for the platforms and workstations it affects. Deleting the old
transport requires evidence for every supported pairing of platform and workstation across local and
Remote Lima (Remote Lima on macOS being the operator's primary platform), WSL2, Proxmox, AWS, Azure
and GCP, from Linux, macOS and Windows workstations. Missing live coverage goes to the operator for
disposition and is never counted as passing.
