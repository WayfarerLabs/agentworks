# Transport Improvements: Prior Art

- Inspected: 2026-09-16 against v0.19.0; original investigation began 2026-09-12
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

The SSH replacement design in [PR #796](https://github.com/WayfarerLabs/agentworks/pull/796) at
`2494f6e2` supersedes the historical #757 design discussed above. Its independent carrier and
proof-first assignment match this SDD; shared file semantics and the core allowlist remain with
transport, not the SSH carrier. Its OpenSSH 8.5 floor concerns builder-owned clients, not sshd;
provider-inner clients need their own inventory. Design alignment is not runtime proof.

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
bound grants so core callers and later plugins need not receive the whole core ceiling. This is not
evidence of an existing consent system, nor permission to build one during transport replacement.

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
