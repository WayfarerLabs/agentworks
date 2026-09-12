# Transport Improvements: Prior Art

- Inspected: 2026-09-12
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

Snapshot: repository commit `7c744828184ccb0ad9ffd90a8a02226384fb384e`.

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

Issue #788 and PR #789 were read as problem and implementation evidence. The PR demonstrates the
need to separate native recovery from canonical repair, while its proposed limited native CLI path
motivates shared execution semantics. Its published test reports are external evidence, not runs
performed for this draft.

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
[proposed seam](execution-contract.md). The SSH developer still needs to review and reconcile the
changed delivery approach in their own SDD.

The two in-tree production `run_detached` calls are remote Lima provisioning and backup. Lima sets
`reuse_completed=False` at `capabilities/vm_platform/lima.py:623`; backup creates a fresh directory
at `vms/backup.py:345` before calling the helper at `:357`. Neither is an intentional
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

## Claims not relied upon

- A common API makes every backend interactive.
- Timeout means a guest process stopped, or makes repeated dispatch safe.
- PyInfra supplies the missing Proxmox carrier or Agentworks context policy.
- A provider PID is a durable, safely cancellable job identity.
- A detached process keeps WSL2 running after its workstation hold is released.
- QEMU protocol support proves Proxmox endpoint availability on every supported major.
- An absent old-stack caller means a credible core/plugin workflow should be excluded.
- A shared transport API can select an application shell implicitly from its carrier.

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
