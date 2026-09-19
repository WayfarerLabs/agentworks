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
`:140` precedes unlink/rename without a remote cooperating-writer lock, and the fingerprint is not a
complete file/metadata identity. Existing tests that edit before the helper runs do not prove the
check-to-publication window. The earlier general warning about check-then-rename now has this
specific migration example; it is not a claim that the shipped helper is a naive prefix check.

The release adds artifact publication and generated sections, per-facet activation maps, persistent
session/run identity and guarded restart consent. It also makes Python3 an initialization package,
not a universal early-bootstrap prerequisite. These affect target composition, metadata/conflict
semantics, observation, rollback and test coverage. Preserve domain behavior while replacing runner
delivery and public staging access; do not delete `artifacts/` or preserve a second file facade.

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

The accepted FRD excludes guest root, not hostile target-user processes. Cooperating-writer locks
cannot constrain an unrelated process that can rename an ancestor or create an alias. Running the
helper without elevation avoids adding root authority but does not establish a narrower recipient's
path grant. The
[file LLD's confinement gate](file-operations-lld.md#confinement-and-filesystem-mechanics) therefore
remains open. Preserve it unless the operator explicitly changes the threat boundary; neither the
future permission-enforcement date nor the in-process plugin trust limitation waives safe object
handling. A solution retaining hostile same-user actors must establish enforced namespace/identity
restrictions for these required writable paths, not merely repeat validation.

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

## Early Python investigation, 2026-09-19

The operator authorized investigation, not installation or a new runtime requirement. This audit
uses implementation head `ae444f53`. The recommendation is to pursue a standard-library helper with
an explicit early prerequisite, subject to the adoption and proof decisions below. Python removes
some shell-level parsing and process-control difficulties; it does not establish confinement or
prove that an application entered its executable.

### Availability and adoption

Implementation paths below are relative to `cli/agentworks/`.

- `capabilities/vm_platform/cloud_init.py:16` omits Python from `PROVISIONING_PACKAGES` and adds it
  in Phase B through `INIT_SYSTEM_PACKAGES`. The shared bootstrap installs its supplied package list
  at `capabilities/vm_platform/bootstrap_script.py:117`. Moving the package earlier is a narrow
  new-VM integration point, not proof that every existing VM or platform host has it.
- Existing-VM repair needs its own native bootstrap entry point, independent of the helper it
  installs. `native_files.py:241` currently recommends reinit when Python is absent, while
  `vms/manager/lifecycle.py:712` requires provisioned state and a valid Tailscale connection for
  reinit. That advice is insufficient for native recovery when canonical access is broken. The
  replacement must not call legacy transport code to solve this dependency.
- SSH-backed platform access is a separate adoption case. Lima's current readiness check at
  `capabilities/vm_platform/lima.py:193` checks workstation tools, not a host Python prerequisite.
  New guest packages cannot supply the runtime for host jobs executed before guest creation.
  Requiring an explicitly provisioned interpreter on macOS hosts needs an operator decision and its
  own validation; readiness must not install a package manager or Python implicitly.
- Package installation requires an available package source. An offline target lacking Python cannot
  acquire it merely because the carrier works. Installation failure must remain an explicit
  bootstrap failure, with an actionable recovery path, not an automatic canonical-route fallback.

The distribution packages currently use Python 3.11 on
[Bookworm](https://packages.debian.org/bookworm/python3) and Python 3.13 on
[Trixie](https://packages.debian.org/trixie/python3). A helper compatible with the 3.11 standard
library could use both distributions' maintained packages without copying the workstation CLI's
runtime floor onto the guest. This is a compatibility proposal, not an instruction to install an old
upstream release or a selected minimum.

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

### Decisions still required

Before selecting this substrate, settle the authorized bootstrap/adoption path for new guests,
existing native-recovery targets, and supported platform hosts. Then prove no-staging invocation and
exact result interpretation through the actual carriers, including launch interruption.

The [OS module documentation](https://docs.python.org/3/library/os.html) describes
platform-dependent descriptor APIs and Linux-only extended-attribute APIs. A Python helper therefore
still needs a proved macOS metadata implementation. It does not close the file LLD's
ancestor-rename, hard-link, cross-identity locking, or mount-confinement questions. Those remain
independent acceptance gates.

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
