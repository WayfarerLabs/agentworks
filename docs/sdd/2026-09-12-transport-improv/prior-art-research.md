# Transport Improvements: Prior Art

- Inspected: v0.19.0 inventory on 2026-09-16; lifecycle research added 2026-09-18
- Scope: Design input, not live provider validation

## Findings

### Shared preparation above delivery

PyInfra's connector documentation separates command preparation from connector execution. Its
utilities handle shell, privilege, environment, and directory wrapping, while connectors implement
execution and file transfer. The documented wrapper receives host and state objects. This supports
centralizing preparation, but adopting the connector API would introduce framework coupling and
still require Agentworks-specific delivery adapters.

Decision: borrow the separation of responsibilities; do not add a PyInfra dependency for this
effort. Evaluating its provisioning operations would be a separate product decision.

Sources: [connector API](https://docs.pyinfra.com/en/3.x/api/connectors),
[API integration](https://docs.pyinfra.com/en/3.x/api/index.html).

### Guest-agent execution is asynchronous and bounded

QEMU documents guest execution as dispatch with arguments/input followed by status observation.
Status reports exit or signal and output-truncation flags. That interface does not provide an
interactive terminal or a guest-exec cancellation operation. File access is a separate protocol; its
existence in QEMU does not prove a provider exposes every endpoint.

Decision: preserve optional interaction, bound provider payloads, and separate waiting from
cancellation. Managed cancellation must be a guest-job operation with ownership checks, not an
invented QGA cancel endpoint. Large file/script support needs demonstrated staging over the actual
Proxmox carrier.

Source: [QEMU guest-agent protocol](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html).

### Current Agentworks implementation

Snapshot: v0.19.0, repository commit `e440a28c49935df722e4e80685ef12f6d8247ff8`. The release-impact
audit compares the original `7c744828` baseline to this revision.

| Evidence                                                                                                          | Design consequence                                                                     |
| ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `cli/agentworks/transports/base.py:36` defines narrow execution and a wider full transport                        | Replace caller-facing tiers with common required operations and explicit optional I/O. |
| `cli/agentworks/capabilities/base.py:161` delivers full transports through `RunContext`                           | Include context construction and consumers in the migration.                           |
| `cli/agentworks/harness_setup/runner.py:40` wraps sudo and forwards the entire transport API                      | Prepared execution defaults belong on the common target.                               |
| `cli/agentworks/remote_exec.py:47` combines detached launch, polling, resume, and cleanup                         | Separate job launch, observation, and disposal.                                        |
| `cli/agentworks/transports/ssh.py:323` retries subprocess timeouts when attempts remain                           | Do not assume a timeout proves non-execution.                                          |
| `cli/agentworks/transports/base.py:332` copies directly to a remote destination despite an atomic-write docstring | Specify remote publication semantics explicitly.                                       |
| `cli/agentworks/plugins/proxmox/transport.py:22` imposes a 65,536-character input limit                           | Required scripts/files need bounded transfer beyond one request.                       |
| `cli/agentworks/capabilities/vm_platform/wsl2.py:371` holds power through a workstation process                   | Detached process lifetime and platform power lifetime need separate contracts.         |

The native cloud builders in `plugins/aws/platform.py:694`, `plugins/azure/platform.py:791`, and
`plugins/gcp/platform.py:571` return public-IP SSH targets. Native access therefore bypasses
Tailscale, but it still relies on SSH in the guest.

`cli/agentworks/capabilities/vm_platform/lima.py:551` builds a placement-host SSH target; `:616`
passes it to `run_detached` for provisioning before guest creation, and `:704` calls `kill_detached`
during rollback. Shared execution/job mechanics therefore need to represent the actual execution
host and preserve supported host userspace, including macOS. Guest-only identity and Debian tool
assumptions would miss an existing caller.

Issue #788 and the now-closed, unmerged PR #789 were read as problem and implementation evidence.
The PR demonstrates the need to separate native recovery from canonical repair, while its proposed
limited native CLI path motivates shared execution semantics. Its published test reports are
external evidence, not runs performed for this draft.

### Coordination and caller evidence

The reduced
[SSH FRD](https://github.com/WayfarerLabs/agentworks/blob/2694d31afeaffe32841c995a552c5223add26689/docs/sdd/2026-09-05-ssh-connection-contracts/frd.md)
and
[HLA](https://github.com/WayfarerLabs/agentworks/blob/2694d31afeaffe32841c995a552c5223add26689/docs/sdd/2026-09-05-ssh-connection-contracts/hla.md)
in PR #757 cover explicit connections, configuration isolation, trust preservation, and buffered
execution consolidation while preserving the current interfaces. Outcome redesign and reconnect are
deferred. That published plan describes consolidation, not an independent replacement. Subsequent
operator direction requests a new SSH stack too, allowing copied code but forbidding dependencies on
legacy execution packages. The revised proposal keeps connection mechanics with that effort and
assigns the common execution/context contract here; both build independently against the
[proposed seam](execution-contract.md). In feedback relayed by the operator after reviewing
`0a4c3746`, the SSH developer supports that assignment and recommends proving the seam first,
unifying input ownership, and assigning platform integration to the transport effort. The operator
directs this revision and clarifies that Remote Lima is the first consumer of generalized SSH-backed
platform access. This is design input and direction, not runtime evidence or a claim that the SSH
artifacts have already been updated.

The [plan](plan.md) therefore gates broad parallel work on an end-to-end buffered proof and a
bounded QGA case, followed by reconciliation of both SDDs. The operator specifies OpenSSH 8.5 as the
minimum; the proof inventory must record the floor's applicable executable locations and server
compatibility. The new input/stream ownership contract and platform-host composition are described
in the contract and HLA, without adding a virtualization framework or claiming exact SSH exit/drop
classification.

The two in-tree production `run_detached` calls are remote Lima provisioning and backup. Lima sets
`reuse_completed=False` at `capabilities/vm_platform/lima.py:623`; backup creates a fresh directory
at `vms/backup.py:348` before calling the helper at `:360`. Neither is an intentional
cross-invocation completed-result consumer. This narrows migration obligations; it does not remove
the future developer requirement for explicit job references and later observation. Existing callers
are evidence about today's migration, not a ceiling on the new interface.

The published review of PR #795 identified duplicated policy prose and optional-feature
declarations. The revision leaves sensitive-output policy in FRD R4, combines placement-host
architecture, and uses one channel-feature description for early checks and opened targets.
Implementation conformance still checks actual behavior; there is no second declaration to
synchronize.

The [OpenSSH server manual](https://man.openbsd.org/sshd.8) documents execution through the
account's shell and account/server hooks before the requested command. This is why the shell policy
separates Agentworks-controlled payload/helper preparation from carrier bootstrap: an inner wrapper
cannot prevent hooks that have already run. Readiness does not request startup evaluation or depend
on its side effects, but the API does not certify arbitrary account hooks as read-only.

### File-only configuration updates

[RFC 7396](https://www.rfc-editor.org/rfc/rfc7396) defines JSON Merge Patch: object members merge,
null removes a member, and arrays/non-object values replace rather than merge element by element. It
works on values, not text layout, and does not offer a literal-null setter through an object member.
The shipped `capabilities/harness_integration/settings.py:78-91,166-175` instead supports replace,
merge-overwrite, merge-preserve and skip-existing, with literal JSON null and whole-value arrays.
Decision: preserve those semantics in the proposed API rather than silently interpreting null as
deletion. RFC 7396 was considered and rejected for this migration, not assigned to an LLD to
rediscover the incompatibility. TOML values and generated-section editing remain domain logic; none
of these formats supplies authorization, writer coordination or publication mechanics.

The operator's 2026-09-15 direction adds whole-file and structured provisioning, privileged
placement and core-reviewed exact-file/subtree mutation locations. On 2026-09-16 the operator
removes FIFO creation from the initial contract after confirming that current runtime objects are
sockets. The contract/HLA place enforcement above delivery and at destination-side mutation.
Registration-time requests and user approval remain deferred. These are design requirements, not
evidence that existing helpers confine paths or that approved configuration cannot cause later
execution.

### SSH coordination reference

Inspected on 2026-09-19: [PR #796](https://github.com/WayfarerLabs/agentworks/pull/796) at
`167980abf80aae11691e2a3f4f699db40ebf9f5f`, especially its
[HLA](https://github.com/WayfarerLabs/agentworks/blob/167980abf80aae11691e2a3f4f699db40ebf9f5f/docs/sdd/2026-09-05-ssh-connection-contracts/hla.md).
This is the single current coordination pin; earlier pins in the buffered proof record remain
historical evidence. The independent carrier supersedes #757's legacy consolidation. Its buffered
PoC is accepted and merged on main at `19629607`; full implementation and migration remain Phase 2.
Shared file semantics, profiles and lifecycle remain above SSH delivery. The OpenSSH 8.5 floor
concerns builder-owned clients, not sshd; provider-inner clients need their own inventory. This
inspection is design reconciliation, not new runtime evidence or ownership of the SSH artifacts.

### Shipped file helpers and release impact

PR #825 is included in the inspected 0.19.0 baseline. `cli/agentworks/native_files.py:192-238`
defines `ROOT_FILE_DIRECTORIES` and `root_native_path`, allowing elevated operations beneath
`/etc/claude-code`, `/etc/codex` and `/opt/agentworks/artifacts`, with distinct directory-root
handling. `NativeFiles` (`:258-305`) is still a facade over legacy Transport; ordinary operations do
not have the same core location ceiling. The new FileAccess therefore replaces a concrete shipped
module, not an unimplemented idea. Transport owns replacement and deletion, including the policy;
the [migration inventory](migration-strategy.md) records consumer-by-consumer disposition.

The helper's held-directory/no-follow traversal (`:82-126`), staged ownership/mode/extended
attributes and replacement (`:149-179`) are useful implementation inputs. Its expected-hash check at
`:140` precedes unlink/rename, and the fingerprint is not a complete file/metadata identity.
Existing tests that edit before the helper runs do not prove the check-to-publication window. The
earlier general warning about check-then-rename now has this specific migration example; it is not a
claim that the shipped helper is a naive prefix check. The successor's proposed machine transaction
lock still needs cross-identity feasibility evidence; this source inspection does not supply it.

`artifacts/publication.py:106-217` distinguishes whole-file publication with explicit modes from
generated-section updates that preserve existing access metadata. The generated-section tests at
`tests/artifacts/test_generated_sections.py:190-264` exercise ordinary extended attributes, POSIX
access ACLs, copy refusal, and replacement without newly inheriting the parent's default ACL. They
are evidence about the shipped mechanism, not proof of absent-file creation behavior or that every
readable security attribute should survive replacement. Under the
[file safety ruling](frd.md#file-safety-and-guest-runtime-rulings), the successor preserves
direct-write-equivalent owner/mode/ACL semantics for every existing regular-file replacement,
retains inherited behavior for creation, and refuses unsupported required cases before publication.
It does not use create metadata to reset an existing whole file, blanket-clear new ACLs, or blindly
copy security attributes that an ordinary content write would clear.

The release adds artifact publication and generated sections, per-facet activation maps, persistent
session/run identity and guarded restart consent. At the inspected revision Python3 is an
initialization package, not a universal early-bootstrap prerequisite. Later operator direction
selects it for the new-guest early apt package list; that supersedes only the new-guest provisioning
disposition. Existing recovery, macOS hosts, and no-staging readiness remain separate. These affect
target composition, metadata/conflict semantics, observation, rollback and test coverage. Preserve
domain behavior while replacing runner delivery and public staging access; do not delete
`artifacts/` or preserve a second file facade.

There are no FIFO-creation consumers in the inspected `cli/agentworks` tree. Actual runtime roots
are `sessions/tmux.py:36,66`'s agent/admin tmux socket directories. Directory/mode management and
confirmed stale-socket removal migrate; tmux owns socket creation. Examples now use shipped roots,
not `/opt/agentworks/harnesses` or a fictional session event pipe.

The fine-grained recipient grant shape is an explicit future-facing contract choice. Today
`RunContext` passes through targets (`capabilities/base.py:226-233`); the new stack introduces small
bound grant shapes so core callers and later plugins need not receive the whole core ceiling. Under
the [2026-09-19 ruling](frd.md#operator-rulings-2026-09-19), these remain groundwork during released
old/new coexistence: production enforcement and security reliance wait for legacy removal. This is
not evidence of an existing consent system, nor permission to build one during replacement.

### File confinement investigation, 2026-09-19

An independent caller audit at `8239c97a` confirms that required destinations include user-owned
home directories, group-writable workspace roots, and tmux socket directories. See
`harness_setup/lifecycle.py:190-199,229-238`, `workspaces/backends/vm.py:78-83`, and
`sessions/tmux.py:263-302,355-383` under `cli/agentworks/`. Absolute configuration-directory
overrides are also shipped behavior, documented in
[`native-harness-setup`](../../guides/native-harness-setup.md). Restricting the implementation to
root-owned configuration trees would therefore omit required workflows.

A local unprivileged probe on Linux 6.1.0-52 arm64 reproduced two deterministic operation sequences
in an owned temporary directory. It opened an approved directory with `O_DIRECTORY | O_NOFOLLOW`,
moved it beneath an outside sibling, then created a file through the held descriptor: publication
occurred at the moved location. Separately, it opened a regular file, checked that its link count
was one, added an outside hard link, and called `fchmod` on the held descriptor: the outside alias
acquired the changed mode. Fresh-inode replacement afterward preserved that alias's old bytes. These
are mechanism counterexamples, not a live-carrier test or a demonstrated exploit of the proposed
helper, which is not implemented. All fixture objects were removed by the temporary-directory owner.

Linux [`openat2`](https://man7.org/linux/man-pages/man2/openat2.2.html) constrains path resolution;
it does not make a later operation through a held descriptor an atomic absolute-path check. The
[Landlock documentation](https://docs.kernel.org/userspace-api/landlock.html) describes rules that
can follow relocated hierarchies, existing-descriptor rights, and metadata operations such as
`chmod`, `chown`, and extended-attribute changes that it does not restrict. Inference: adding
Landlock around this helper would not by itself establish the file contract's complete path and
metadata confinement. This investigation did not run Landlock or prove an alternative on macOS.

These dated probes remain useful counterexamples to claims of absolute namespace confinement or
same-inode equivalence. They did not test the proposed implementation and do not prove one. The
later [file safety ruling](frd.md#file-safety-and-guest-runtime-rulings) explicitly excludes a
malicious process already running as the authorized target user; compromise at that identity is
already outside the useful file guarantee. Hostile same-user ancestor moves and hard-link additions
are therefore no longer production gates. The design still validates untrusted requests and paths,
refuses observed links and unsupported object types, handles opened objects conservatively, cleans
up only operation-owned names, binds the authorized identity/elevation, and excludes guest root. The
threat-boundary change does not remove simultaneous legitimate CLI writers, so the proposed
cross-identity transaction lock remains an unproved production gate. Accidental and non-cooperating
changes remain subject to the documented conflict and uncertainty rules.

### Managed foreground work and session containment

The upstream systemd v252 manual describes transient services, synchronous pipe/PTY operation and
waiting for completion. This supports foreground managed execution without equating cgroups with
detachment. Service and scope launch have different parent/environment behavior; default start
acknowledgment is not proof that the application executed. Agentworks still needs its own launch and
outcome evidence.

Sources: [systemd-run v252](https://github.com/systemd/systemd/blob/v252/man/systemd-run.xml),
[systemd kill policy v252](https://github.com/systemd/systemd/blob/v252/man/systemd.kill.xml).

Kernel cgroup v2 documentation specifies inherited membership, subtree population observation and
subtree kill. Moving a parent does not move its existing descendants. Decision: launch inside the
owned boundary, distinguish main-process exit from emptiness, and do not certify legacy sessions by
moving a surviving parent. These primitives do not by themselves stop same-user indirect execution
through an outside service.

Source: [kernel cgroup v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html).

The #770 [source snapshot](inputs/session-cgroups-frd-2c406948.md) is indexed by implementation
destination in the [lifecycle design](execution-lifecycle-lld.md). The full source, not the short
index, carries identity, whole-run termination, independent lifetime, escape resistance, usable
sessions, compatibility and trusted VM-side identity lookup. It is not evidence that a particular
same-UID or per-run-user mechanism is approved or works. Transport now owns the proposed shared
lifecycle implementation; foreign-artifact disposition still requires an explicit handoff.

These sources justify the design direction, not platform acceptance. Exact installed versions,
privilege/FD behavior, account startup, secret exposure, macOS host jobs and WSL2 lifetime need the
plan's proofs before enabling profiles. Linux systemd features cannot be inferred from a distro
name.

### Lifecycle test-bed gaps

For macOS mechanism selection, do not equate a launchd job with a Linux cgroup. Apple's
[launchd property-list manual](https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5)
describes remaining-process cleanup by shared process-group ID, not arbitrary descendant ancestry.
XNU's [event header](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/event.h)
explicitly marks `NOTE_TRACK`, `NOTE_TRACKERR` and `NOTE_CHILD` unsupported since macOS 10.5. A
process-group wrapper or a copied BSD fork-tracking example therefore does not establish the
required detached-descendant and verified-emptiness promises. This source review rules out those
particular shortcuts, not every possible macOS supervisor. The host lifecycle mechanism and its
native proof remain open; no private kernel API, privileged service or weaker profile is selected.

The
[complete tester report on #830](https://github.com/WayfarerLabs/agentworks/pull/830#issuecomment-5739120200)
validates the design documents, not the unimplemented profiles. Its inventory at this checkpoint is:

- WSL2 has no live coverage in this effort. It needs the Tier 2 Windows bed, not the Tier 1 host
  used for workstation SSH testing.
- macOS was tested as an SSH client workstation, never as a placement host running jobs. Local Lima
  and SSH-backed macOS host execution need their own applicable host-path evidence.
- Bookworm/Trixie buffered execution passed, but kernel/systemd versions and lifecycle behavior were
  not measured. Collect those versions and cases before claiming cgroup compatibility.
- Authorized PVE 8.4.21 and 9.2.11 beds are single-node. No cluster or migration behavior is
  implied; a new multi-node test would need a separate inventory and authorization.

The same report confirms surviving guest work after observation timeout and a detached child after
reported main completion. Those support separating waiting, exit and cleanup. Account-shell lookup
and intermediary-refusal measurements inform honest outcome interpretation; they do not implement
R7's run-membership identity lookup. Future live charters must name workstation and target axes,
resource/cleanup limits and any newly required beds rather than inherit nonexistent coverage.

## Terminal bootstrap prior art

The operator directed using existing bootstrap-to-interactive patterns as the starting point. A
same-terminal proof therefore precedes committing to a separate staging operation. This is an
implementation investigation, not a relaxation of sensitivity, terminal ownership or platform
requirements.

| Source                                                                                                                                                                                                       | Observed mechanism                                                                                                                                                  | Consequence for this effort                                                                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Kitty Python bootstrap at `719c61a1`](https://github.com/kovidgoyal/kitty/blob/719c61a12bf192bdaaf8dc3e26acb4f853dfd7fa/shell-integration/ssh/bootstrap.py)                                                 | Disables echo before requesting setup data over the controlling TTY, reads a marker-delimited base64 transfer, applies setup, restores echo and executes the shell. | Useful phase ordering, not a drop-in transport. Its terminal-emulator integration and file extraction are not requirements here.                                  |
| [Pexpect endpoint API](https://pexpect.readthedocs.io/en/stable/api/pexpect.html) and [echo-race discussion](https://pexpect.readthedocs.io/en/stable/commonissues.html#timing-issue-with-send-and-sendline) | Separates automated credential entry from human interaction; documents sending before echo is disabled as a disclosure race.                                        | Require an explicit ready acknowledgment rather than a timing delay or merely seeing a prompt. Local no-echo state alone does not establish the remote PTY state. |
| [OpenSSH 8.5 session setup](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/ssh.c#L2051) and [resize delivery](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/channels.c#L4429)         | Session setup uses stdin; resize reads the channel's input descriptor with a terminal ioctl.                                                                        | Piping a prelude into `ssh -tt` does not by itself preserve terminal sizing. Prove the local endpoint mechanism with SSH before selecting shared terminal types.  |

These are primary implementation/documentation sources, not platform acceptance results. Kitty's
bootstrap is GPLv3; this investigation reuses the conceptual pattern, not its source code. Its
handling of already-echoed leading data is not a secrecy guarantee we can adopt. Base64 is transport
encoding, not encryption or redaction.

The [carrier I/O experiment](carrier-io-lld.md#same-terminal-preparation-experiment) uses separate
payload-ready and interactive-ready phases, exact bounded reads, and no payload EOF. The proof must
demonstrate no secret reflection, preservation of the first interactive input, terminal restoration
and failure before application launch on malformed/truncated input. Client-side relay, real SSH,
supported workstation coverage and honest application-start evidence remain open; none is
established by studying this prior art.

## Early Python investigation, 2026-09-19

At the time of this audit the operator authorized investigation, not installation or a new runtime
requirement. The audit uses implementation head `ae444f53` and recommended a standard-library helper
with an explicit early prerequisite. The later
[guest runtime ruling](frd.md#file-safety-and-guest-runtime-rulings) approves `python3` in the
new-guest early apt package list and requires the helper to support Bookworm's distribution Python.
The subsequent host ruling requires preinstalled Python 3.11 or newer on macOS platform hosts, with
clean missing/version/Xcode-shim diagnostics and no installation prompt. Neither ruling approves
implicit runtime installation during readiness or settles existing-guest recovery. Python removes
some shell-level parsing and process-control difficulties; it does not establish file safety or
prove that an application entered its executable.

### Availability and adoption

Implementation paths below are relative to `cli/agentworks/`.

- `capabilities/vm_platform/cloud_init.py:16` omits Python from `PROVISIONING_PACKAGES` and adds it
  in Phase B through `INIT_SYSTEM_PACKAGES`. The shared bootstrap installs its supplied package list
  at `capabilities/vm_platform/bootstrap_script.py:117`. This inspection identified the narrow
  new-VM integration point. The later ruling authorizes adding `python3` there; implementation and
  validation are still required, and this says nothing about an existing VM or platform host.
- Existing-VM repair needs its own native bootstrap entry point, independent of the helper it
  installs. `native_files.py:241` currently recommends reinit when Python is absent, while
  `vms/manager/lifecycle.py:712` requires provisioned state and a valid Tailscale connection for
  reinit. That advice is insufficient for native recovery when canonical access is broken. The
  replacement must not call legacy transport code to solve this dependency.
- SSH-backed platform access is a separate adoption case. Lima's current readiness check at
  `capabilities/vm_platform/lima.py:193` checks workstation tools, not a host Python prerequisite.
  New guest packages cannot supply the runtime for host jobs executed before guest creation. The
  subsequent ruling requires a preinstalled compatible interpreter on macOS hosts. Its detection and
  supported host behavior still need validation; readiness must not install a package manager or
  Python implicitly, including triggering the Xcode developer-tools installation prompt.
- Package installation requires an available package source. An offline target lacking Python cannot
  acquire it merely because the carrier works. Installation failure must remain an explicit
  bootstrap failure, with an actionable recovery path, not an automatic canonical-route fallback.

The distribution packages observed during the audit use Python 3.11 on
[Bookworm](https://packages.debian.org/bookworm/python3) and Python 3.13 on
[Trixie](https://packages.debian.org/trixie/python3). The later ruling adopts the compatibility
consequence: the helper must support Bookworm's maintained distribution `python3`, rather than copy
the workstation CLI's runtime floor onto the guest. This observation did not itself install or
validate the package.

### Local mechanism evidence

A disposable probe on Debian 12, Linux arm64, CPython 3.11.2 used a constant `python3 -I -S -B -c`
helper and a capped binary request envelope. The
[interpreter options](https://docs.python.org/3/using/cmdline.html) isolate Python environment and
module lookup, skip site initialization, and disable bytecode writes. They do not sanitize the
operating-system loader environment or certify arbitrary helper code as read-only.

The measured cases separated short shell source on an inherited pipe from application stdin,
round-tripped all 256 byte values, and kept stderr separate. Ordinary exits 0, 1, 125, 126, 127,
143, and 255 remained distinct from a nonexistent executable. Request markers were absent from the
bounded completion record. A timed-out child group was killed and reaped in this local case.

This is not carrier or platform acceptance: the probe used Linux `/proc/self/fd`, buffered output,
short input, and an extra local status pipe. It did not exercise QGA, SSH, macOS, a guest,
elevation, large transfers, or detached ownership. It created no guest staging files and installed
nothing, but that does not prove the full no-staging readiness path.

One counterexample is decisive: killing the child with SIGKILL in `preexec_fn` allowed `Popen` to
return, followed by wait status -9, although exec never occurred. Therefore neither `Popen` return
nor EOF on its internal close-on-exec error pipe is positive application-start evidence. The
preparation design must resolve that gate without synthesizing `STARTED` or reserving legitimate
application exit values as launcher errors.

A follow-up source review suggests a narrower foreground proof: an audited native CPython fork/exec
path with no `preexec_fn`, an intact exec-error channel, and a genuine ordinary wait result for the
owned child may establish completion retrospectively. It need not invent an earlier `STARTED` event.
This is an inference from the
[child-launch implementation](https://github.com/python/cpython/blob/v3.12.12/Modules/_posixsubprocess.c),
not a guarantee of arbitrary subprocess configurations. It proves only the directly executed
process, not script-body entry or descendant completion. Detached launch still needs positive
acknowledgment, and signal death remains uncertain without independent start evidence.

The actual-wait qualification matters:
[CPython's wait implementation](https://github.com/python/cpython/blob/v3.12.12/Lib/subprocess.py)
substitutes zero when child status is unavailable. Local CPython 3.12.13 probes reproduced this with
ignored `SIGCHLD` and with a competing reaper. A separate probe reset `SIGCHLD` before launch and
obtained exact-PID ordinary wait statuses for exits 0, 42, and 255; a second wait raised
`ChildProcessError`. Missing wait evidence must remain unknown. This candidate requires a fixed,
single-reaper helper and proof on supported interpreter builds before changing the preparation gate.

A further local probe on Debian's CPython 3.11.2 recovered all 256 normal exit values through an
exact-child native wait. Missing executable, missing working directory and non-executable object
raised before returning a process. Adversarial fixtures demonstrated the exclusions: pre-exec
callbacks can exit normally without application entry, and a deliberately broken exec-error writer
can make a failed launch appear to exit 255. These fixtures are not production callback options. The
[3.11 native launch implementation](https://github.com/python/cpython/blob/v3.11.2/Modules/_posixsubprocess.c#L658-L703)
and
[parent error-pipe handling](https://github.com/python/cpython/blob/v3.11.2/Lib/subprocess.py#L1797-L1902)
support this bounded inference. It excludes `posix_spawn`; the
[documented WSL/QEMU exception](https://docs.python.org/3.11/library/subprocess.html#popen-constructor)
is one reason not to generalize across launch implementations. Bookworm's measured fork/exec choice
is not a promise about every future interpreter. The
[preparation candidate](preparation-lld.md#retrospective-completion-candidate) keeps eager launch,
signal ambiguity and native-platform acceptance separate.

### Identity-neutral file locking

Advisory locking attaches to a kernel object rather than a username. Linux permits exclusive `flock`
on a read-only descriptor; duplicate and forked descriptors share its lifetime, and separate opens
of the same inode contend. A local ext4 experiment on a temporary mode-0555 directory confirmed
contention through aliases, bounded non-blocking acquisition and last-close release after fork. It
did not lock the workstation root or exercise another identity. Sources:
[Linux flock](https://man7.org/linux/man-pages/man2/flock.2.html) and
[util-linux directory locking](https://man7.org/linux/man-pages/man1/flock.1.html).

Apple's
[flock contract](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/flock.2.html)
and [XNU dispatch](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_descrip.c)
support a common vnode lock domain, but filesystem implementations and security policy can refuse
it. Generic XNU directory support is not live APFS acceptance. Network filesystems also change Linux
semantics; in particular an exclusive NFS lock can require write access. Do not infer a portable
lock from the Python function's availability.

The candidate remains one fixed, protected Agentworks lock inode shared by every helper identity,
not a per-effective-user cache. Root ownership is not intrinsically required: a stable host-account
namespace can suffice if all bound identities can open the same inode and other in-scope identities
cannot replace it. Core setup must preserve that inode throughout helper use. Provisioned Debian
state and the actual macOS host namespace still need selection and cross-identity tests; a new admin
installation or host prerequisite would need operator direction. Locking `/` avoids new state but
shares an unrelated third-party lock namespace, so it is not selected. This investigation does not
close the file LLD's transaction-lock gate.

### Decisions still required

The new-guest package and preinstalled macOS host runtime choices are settled, but their
availability, diagnostics, and helper compatibility still need implementation evidence. Settle the
bootstrap path for existing native-recovery targets. Then prove no-staging invocation and exact
result interpretation through the actual carriers, including launch interruption. Readiness must
refuse a missing prerequisite rather than install Python implicitly.

The [OS module documentation](https://docs.python.org/3/library/os.html) describes
platform-dependent descriptor APIs and Linux-only extended-attribute APIs. A Python helper therefore
still needs a proved macOS metadata implementation. It also does not prove unique-sibling atomic
publication, observed-link refusal, required owner/mode/ACL behavior, or cooperative conflict
handling and cross-identity locking on supported filesystems. Those remain implementation acceptance
gates within the revised threat boundary.

### Local process startup and interruption

Private review of the shared workstation pump at `38c3fa2b` distinguished two startup limitations.
The
[Python subprocess documentation](https://docs.python.org/3.12/library/subprocess.html#subprocess.run)
states that initial process creation cannot be interrupted on many platform APIs. The pump checks
the same deadline before and after construction; it does not reset that budget. An OS call taking
longer than the remaining budget is not evidence that the pump granted a fresh observation timeout.
This is not a hard real-time guarantee over process creation.

Local launch interruption is a separate, demonstrated ownership gap. On Linux CPython 3.12.13, a
parent sent real SIGINT while the pump launched a valid 1.5 MB argument vector. The traceback placed
the interruption in CPython's `self.pid = _fork_exec(...)`, before `Popen` returned. The child wrote
its PID and remained alive after the interruption propagated. The probe driver independently killed
and reaped that exact child. This did not replace `Popen` or its private launch function. The large
argument vector widened the test window; ordinary carrier argv frequency and Windows/macOS behavior
were not measured.

The existing SSH pump shares this construction pattern, so extraction did not introduce the gap.
Neither implementation can claim launch-interruption conformance from post-construction cleanup
tests. A supported ownership mechanism still needs design and host-specific proof; no supervisor or
weaker interruption contract is selected by this finding. The production gate remains open.

A follow-up Linux experiment at `cc40bca2`, independently repeated by the lead, explored deferred
SIGINT without changing production code. A temporary non-raising handler recorded the signal; an
injected checkpoint raised `KeyboardInterrupt` only after the pump entered its guarded region. The
handler remained non-raising during cleanup and was restored before propagation. In each run, the 50
and 200 microsecond timing groups produced 16 live baseline orphans in 16 trials, versus 16 cleaned
and reaped candidate children. An outer process adopted each baseline orphan and verified direct
ownership before killing and reaping it. A separate cleanup-time signal case also reaped its child
before propagating. A native child's observed SIGINT mask/disposition remained unchanged.

This is mechanism evidence, not an implementation choice. The fixture wrapped process creation to
record returned PIDs and injected the checkpoint through a deadline adapter; neither belongs in a
production API. A custom handler's identity was restored but its behavior was not preserved, and
installing the temporary handler from a worker thread failed. The handler remains process-global
despite lexical scope. A launch that never returns would defer Ctrl-C indefinitely; repeated
signals, cleanup-failure evidence and supported Windows/macOS behavior remain unresolved. The
[Python signal documentation](https://docs.python.org/3.12/library/signal.html#note-on-signal-handlers-and-exceptions)
explains why asynchronous exceptions can interrupt ownership transitions. Any selected solution
needs explicit interrupt ownership and safe checkpoints, not an incidental signal-handler change
inside a carrier. No application-wide cancellation policy is adopted by this experiment.

## Native adapter audit, 2026-09-19

The existing finite-input carrier boundary permits non-production adapter proofs without selecting
the new helper runtime. It does not authorize production wiring or settle the sink extension.

The
[Lima v2.0.3 shell implementation](https://github.com/lima-vm/lima/blob/f1e8803c3c475c176a5ff94cf145762281b51419/cmd/limactl/shell.go#L204-L229)
adds login startup even with an explicit `--shell`. The same version
[returns without dispatch](https://github.com/lima-vm/lima/blob/f1e8803c3c475c176a5ff94cf145762281b51419/cmd/limactl/shell.go#L107-L121)
when the instance is stopped and start was not requested. Thus a thin `limactl shell` wrapper
neither honors our preparation-controlled startup policy nor turns local zero into evidence of
dispatch. Native Lima needs provider discovery composed with the shared SSH policy and delivery, or
another proved mechanism; no legacy wrapper should be copied as the new carrier.

The
[WSL command parser at a366853f](https://github.com/microsoft/WSL/blob/a366853fa06b46b0797a5d359321a870a6aafce0/src/windows/common/WslClient.cpp#L1803-L1842)
supports a direct-exec path using Windows argument parsing instead of the user's shell. This makes
`--exec` a candidate for literal prepared argv, not proof of byte fidelity or trustworthy guest
status. Windows live cases must cover empty/quoted arguments, binary pipes, provider diagnostics,
exit 255, interruption, and VM power lifetime. Reusing the current bounded subprocess pump requires
coordination with its SSH owner: non-SSH adapters should neither import SSH-private I/O nor grow a
second copy of it. These are implementation inputs, not completed adapter acceptance.

The private `carriers/wsl2.py` candidate uses that literal `--exec` path and the transport-owned
shared pump. The same pinned source initializes WSL client failure to -1 at lines 1554-1557, returns
the service launch result at lines 654-693, and maps caught failures to -1 at lines 1923-1927. The
integer alone does not establish an exact guest exit: the pinned
[Linux init](https://github.com/microsoft/WSL/blob/a366853fa06b46b0797a5d359321a870a6aafce0/src/linux/init/init.cpp#L2050-L2063)
normalizes ordinary exits but sends a signaled child's raw wait status unchanged. Normal exit 15 and
SIGTERM therefore both produce 15. The
[Windows VM worker](https://github.com/microsoft/WSL/blob/a366853fa06b46b0797a5d359321a870a6aafce0/src/windows/common/interop.cpp#L574-L615)
forwards that number and returns 1 if the status channel closes without an exit message.

The corrected private mapping uses observed statuses 0 through 255 as candidate dispatch evidence,
with typed completion only for zero. Nonzero values retain raw local status and outcome uncertainty,
not a fabricated exit-code kind; negative, absent and out-of-range values leave dispatch uncertain
as well. Shared preparation still owes independently proved application results for every required
exit value and signal. This limitation does not redefine the public contract or complete WSL
conformance. Unit tests do not establish these distinctions against supported shipped clients. The
carrier remains outside production composition pending the live cases above and the shared
launch-interruption ownership gate.

## macOS Python prerequisite

The approved host prerequisite is preinstalled Python 3.11 or newer, not implicit developer-tools
installation. Apple's
[command-line tools FAQ](https://developer.apple.com/library/archive/technotes/tn2339/_index.html)
describes the `/usr/bin` developer-tool shims. Python's
[macOS documentation](https://docs.python.org/3.13/using/mac.html) identifies `/usr/bin/python3` as
the Apple development-tools runtime and documents the installer links under `/usr/local/bin`.
Neither source proves that running a particular host's system path is harmless, so the candidate
does not execute that path to inspect it.

[Homebrew installation documentation](https://docs.brew.sh/Installation) identifies its Apple
Silicon and Intel prefixes; its
[runtime documentation](https://docs.brew.sh/Language-Runtimes-and-Packages) locates `python3` in
the prefix's `bin` directory. This supports the two fixed discovery candidates, not discovery of
arbitrary custom installations. An explicit bound path handles those installations.

The
[Bash conditional-expression reference](https://www.gnu.org/software/bash/manual/bash.html#Bash-Conditional-Expressions)
defines `-ef` as device/inode equality and states that file tests follow symlinks. This suggests a
small alias check against the known shim without a custom symlink resolver. It remains an inference
pending proof with the shipped macOS shell. The
[Python command-line reference](https://docs.python.org/3/using/cmdline.html) supplies the isolated,
no-site, no-bytecode invocation flags. Those flags do not prove the complete helper performs no
writes; the inline readiness test must establish that separately.

The [preparation candidate](preparation-lld.md#darwin-inline-prerequisite-candidate) records the
selection, refusal and observation rules. Native macOS installation, shim-alias, no-prompt and
no-write evidence remain outstanding. No Linux fixture substitutes for those measurements.

## Lima provisioning and VM-runtime ownership

Source audit of Lima v2.2.0 (`de0816ea4bdc5267b428ab21025889b8dd785526`) separates completion of
provisioning commands from the lifetime of the VM they create. This is upstream source evidence, not
a live macOS result or a new minimum Lima version.

Without an existing autostart registration, ordinary `limactl start` launches its host agent in a
new process group, watches its readiness events, then returns while that host agent remains running.
With `--foreground`, it instead replaces the start process with the host agent through `exec`.
Lima's own launchd template uses this foreground form; ordinary start of an instance registered with
the user LaunchAgent requests launchd bootstrap instead. Explicit foreground start bypasses that
registration, so migration must not introduce competing lifetime owners. Sources:
[start](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/instance/start.go#L238-L422),
[process group](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/executil/opts_others.go#L12-L15),
[foreground exec](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/instance/start_unix.go#L20-L48),
and
[LaunchAgent template](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/autostart/launchd/io.lima-vm.autostart.INSTANCE.plist#L5-L23).

For VZ, the VM lives inside the host agent and its recorded VM PID is the host agent's PID. QEMU is
a separate child in another process group; the host agent retains and waits for it. Killing the host
agent alone therefore has different implications for these two drivers. Sources:
[VZ runtime](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/vz/vm_darwin.go#L49-L105) and
[QEMU launch](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/qemu/qemu_driver.go#L282-L443).

The current Agentworks remote-create wrapper combines `limactl create` and ordinary `limactl start`
in one detached shell. Applying generic main-exit descendant cleanup to that wrapper could terminate
the intentionally surviving VM runtime. The candidate migration separates bounded create/readiness
operations from a resource-owned job anchored by `limactl start --foreground`. It adds no exception
allowing arbitrary descendants of a finished job to survive. The VM owner, not the carrier, must
retain and reconcile that lifetime through start, stop, restart, rollback and delete.

This candidate still needs supported-version and native-host proof: readiness independent of process
completion, disconnect survival, abrupt host-agent loss with QEMU descendants, stale references and
unrelated-process survival. Lima's recorded-PID check uses signal zero rather than a process-birth
identity, so it is not itself proof of the transport's stale-reference guarantee. Sources:
[stop](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/instance/stop.go#L26-L167),
[delete](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/instance/delete.go#L17-L36), and
[PID validation](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/store/instance.go#L200-L235).
Neither `--foreground` nor launchd establishes every MANAGED guarantee; the macOS mechanism gate
remains open.

### Darwin ownership feasibility

The follow-up source audit distinguishes ordinary process-group cleanup from inherited ownership
that survives ordinary background detachment. Apple's
[launchd guidance](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
requires managed programs not to daemonize and recommends avoiding `setsid`. The published
[launchd manual](https://github.com/apple-oss-distributions/launchd/blob/launchd-842.92.1/man/launchd.plist.5#L371-L375)
describes cleanup of the job's process group, not arbitrary descendants that create other groups.
That source is historical; it does not prove current implementation details or establish a modern
coalition-backed public lifecycle contract.

Darwin coalitions have the relevant kernel shape: inherited membership survives fork and exec,
membership cannot change after creation, and identifiers are not reused. However, Apple's
[XNU coalition overview](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.121.6/doc/observability/coalitions.md)
reserves their creation, termination, reaping and explicit spawn placement for launchd or kernel
tests. These are not a supported general-client substitute for cgroups. Moreover, coalition
termination is not itself a kill operation. A private coalition controller is not selected here.

The evidence therefore does not support advertising generic MANAGED on macOS through a plain
launchd/process-group implementation. This is a public-mechanism gap, not proof that every possible
Darwin supervisor is impossible. The lead recommends retaining the unchanged MANAGED guarantees
where they can be proved and investigating a narrower, explicit cooperative resource-lifetime
contract for macOS placement-host workflows. That would change the current independent-job/profile
requirement and needs operator disposition before implementation. Lima foreground ownership remains
a candidate for preserving required VM workflows, not proof of generic descendant emptiness. No
requirement is waived by this research, and no native macOS result is claimed.

## Claims not relied upon

- A common API makes every backend interactive.
- Timeout means a guest process stopped, or makes repeated dispatch safe.
- PyInfra supplies the missing Proxmox carrier or Agentworks context policy.
- A provider PID is a durable, safely cancellable job identity.
- A detached process keeps WSL2 running after its workstation hold is released.
- QEMU protocol support proves Proxmox endpoint availability on every supported major.
- An absent old-stack caller means a credible core/plugin workflow should be excluded.
- A shared transport API can select an application shell implicitly from its carrier.
- An approved pathname makes arbitrary configuration contents non-executable.
- Path-prefix validation or a preflight symlink check establishes mutation-time confinement.
- Atomic file replacement prevents lost updates or makes a directory tree transactional.
- Withholding public exec prevents trusted file helpers from using internal command delivery.
- Cgroup ownership alone prevents hostile code from launching through outside same-user services.
- Foreground, PTY and operation-bound lifetime are the same choice.
- A stronger profile may silently replace the caller's requested execution or lifetime policy.

## Open evidence

The next design pass needs live feasibility evidence for bounded transfer and managed jobs over
Proxmox, including the supported provider majors, and detached behavior on WSL2. It must establish
the minimal guest tools before proposing an exact bootstrap implementation.

| Source                               | Quality                                      | Limitation                                                                      |
| ------------------------------------ | -------------------------------------------- | ------------------------------------------------------------------------------- |
| PyInfra official documentation       | Primary upstream API/design documentation    | Does not validate an Agentworks integration.                                    |
| QEMU official protocol reference     | Primary upstream protocol documentation      | Proxmox is a separate carrier with its own endpoint and permission constraints. |
| Agentworks code at the pinned commit | Direct implementation evidence               | Describes current code, not the proposed target behavior.                       |
| Issue #788 and PR #789               | Published problem, diff, and review evidence | Neither authority nor independently reproduced live validation.                 |
