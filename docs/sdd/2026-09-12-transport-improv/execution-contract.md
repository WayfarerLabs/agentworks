# Transport Improvements: Proposed Contract and Package Layout

- Status: Post-proof design revision; public API and lifecycle profiles are not shipped
- Governing requirements: [FRD](frd.md), especially R2-R6, R9-R11
- Architecture and ownership: [HLA](hla.md)

This is the proposed implementation boundary for parallel work. It gives concrete interface shapes
without claiming the shell bootstrap, file transfer, or job protocol LLDs are complete. Transport
settles this seam with SSH implementation input, then the efforts pass the
[small-contract proof gate](plan.md) before broad parallel implementation. The transport lead is the
sole owner of the carrier contract and acceptance criteria. SSH owns its new carrier implementation
and raises feasibility concerns to transport; it does not maintain a second contract. Unresolved
feasibility still blocks proof acceptance, and requirement changes return to the operator. Neither
implementation imports the legacy execution stack. The buffered proof is accepted; it does not
demonstrate the broader public API or lifecycle guarantees proposed here.

The [active proof LLD](proof-lld.md) records the buffered implementation subset, shared framing,
native adapter placement and evidence gaps. The proposed public and live-I/O interfaces below are
not claims that the initial proof package implements them.

## Delivery stages and permission activation

The [2026-09-19 ruling](frd.md#operator-rulings-2026-09-19) makes production adoption additive:
first expose the complete new surface alongside legacy RunContext access, then migrate consumers in
separate PRs, then physically delete legacy. New code never calls old execution packages.
`RunContext.admin_execution_target()` and `.agent_execution_target()` are the permanent new
accessors. Existing `admin_target()` and `agent_target()` keep their legacy types and behavior until
deletion; there is no union return type, runtime stack selector or fallback between stacks.

Permission/grant language below specifies the **post-removal contract**. During coexistence, do not
withhold interfaces, reject calls or advertise isolation on the basis of new recipient grants,
including the new core file ceiling. Record intended actions, paths, elevation and profiles during
consumer migration; these are review inputs for later activation, not enforced permissions. Existing
legacy checks remain unchanged on legacy calls. This does not add a public permissions-disable
switch or a shadow authorization service. Isolated tests can prove the future denial behavior, but
production enforcement and any reliance on it wait for the removal gate.

Operational invariants apply from the first new-stack release: bound identity/route and lifetime,
explicit shell/elevation/profile selection, actual guest OS permissions, SSH trust, sensitivity,
safe filesystem object handling and truthful outcomes. A requested MANAGED profile must deliver its
advertised protections even during coexistence; it does not confine other calls through the legacy
API. Readiness's no-staging restriction remains an operation contract, not a deferred recipient
permission. No claim of a file-only or profile-required recipient boundary is valid while legacy
access remains.

## Caller contract

Core and plugin consumers import public types from `agentworks.execution` and receive a scoped
`ExecutionTarget` view from `RunContext`. The view provides optional execution and file interfaces;
execution actions remain independently granted. These examples assume the owning operation has
checked that `execution` was supplied. They propose names and argument forms, not executable code or
a second command language:

```python
execution.run(Command(["tool", "--name", name]), profile=Protection.DIRECT, check=True)
execution.run(
    Script(source, shell=Shell.BASH),
    profile=Protection.MANAGED,
    stdin=Input.sensitive(secret_bytes),
    sudo=True,
)
execution.run(
    Script(source, shell=Shell.USER_DEFAULT, login=True),
    profile=Protection.MANAGED,
    cwd=remote_directory,
)
job = execution.start(
    Script(source, shell=Shell.SH),
    profile=Protection.MANAGED,
    lifetime=Lifetime.INDEPENDENT,
)
status = execution.observe(job)
result = execution.wait(job, deadline=deadline)
execution.stop(job, deadline=stop_deadline)
execution.dispose(job)
```

`run(Command(argv) | Script(source, shell=...), ...)` waits for completion and returns
`ExecutionResult`. `start` accepts the same invocation and options but returns a credential-free
`JobRef` after launch acknowledgement. A lost acknowledgement produces an uncertain-start outcome
with any safe reconciliation reference, never automatic relaunch. There is no anonymous
`background=True` or caller-written `nohup` requirement.

Shared keyword options are `profile`, `lifetime`, `sudo`, `env`, `cwd`, `stdin`, `output`,
`sensitive`, and `deadline`. `run` also accepts `check`; `wait` accepts it when collecting a job
result. Elevation is non-interactive and requires guest authority; after permission activation it
also requires the bound elevation grant. A VM admin account alone then does not authorize the API's
`sudo=True` option. This does not block sudo invoked inside an otherwise allowed direct command
under an account that already has that guest authority. Scripts explicitly select `Shell.SH`,
`Shell.BASH` or `Shell.USER_DEFAULT`; `Script` options `login` and `interactive` separately select
startup behavior. `profile` is also explicit. The [lifecycle design](execution-lifecycle-lld.md)
defines independent invocation, observation, I/O, lifetime, protection and identity choices, their
defaults and invalid combinations. Waiting does not require direct execution: `run` can wait for
work launched inside a managed boundary.

Finite byte input works on every target; omission means EOF, not inherited console input. `run` may
explicitly select `Input.live(source)` for non-terminal piped or duplex work on a channel with
direct live stdio. This does not allocate a PTY. The carrier pumps that source with the selected
output sinks; an unsupported channel refuses before dispatch. Independent lifetime rejects
caller-owned live pipes: input and output must survive the initiating connection. Script source has
its own delivery path and never consumes application stdin. Initial `start` uses only EOF/finite
target-delivered input and target-owned capture/discard output; caller-owned live pipes and terminal
endpoints are refused before dispatch. A later `attach` call can borrow a terminal for a supported
durable endpoint without owning the job's lifetime.

Output defaults to bounded capture, with explicit discard or direct-streaming modes. Effective
sensitivity combines bound environment metadata, an input sensitivity marker, and the request-wide
`sensitive=True` option for caller-supplied source, environment or other payloads. A call can add
protection but cannot downgrade a sensitive bound value. Preparation preserves this designation
through staging and `CarrierIO`; carriers do not infer sensitivity from content. FRD R4 owns the
resulting suppression and authorized live-presentation policy. `env` and `cwd` affect the final
execution identity, not the workstation SSH process. A deadline is one monotonic budget through
preparation and observation; omission follows the explicitly bound operation policy, not a
carrier-selected timeout or retry default.

| Surface                                                                       | Proposed behavior                                                                                                                       |
| ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `features`, identity/route metadata                                           | Passive values from the bound target; no connection or authority discovery.                                                             |
| `read_file`, `stat`, `write_file`, `upload`, `download`                       | Bounded bytes or explicit paths, metadata and publication policy; file reads are separately granted.                                    |
| `upload_directory`, `download_directory`                                      | Explicit merge/replace choice, confined paths and extraction; no implicit recursive deletion.                                           |
| `update_json`, `list_directory`, `ensure_directory`, `set_metadata`, `remove` | Structured updates, bounded inventory and filesystem lifecycle under core-approved paths/actions; no command grant required.            |
| `observe`, `read_output`, `wait`                                              | Separate status, bounded cursor-based output reads, and waiting for a known job; observing never deletes its records.                   |
| `stop`, `dispose`                                                             | Request owned-workload termination with truthful confirmation, or dispose terminal-job artifacts; neither guesses authority from a PID. |
| Terminal I/O and `attach(job, terminal=...)`                                  | Optional terminal launch/attachment, distinct from lifetime, shell startup and authorization to launch additional work.                 |

`ExecutionResult` carries available guest exit status, byte output and completeness, and the outcome
facts needed to distinguish failure, timeout, and uncertainty. A typed execution error carries the
same safe result facts when `check=True`; `check=False` exposes them without turning uncertainty
into an ordinary guest exit. Output decoding is explicit and does not normalize raw bytes. Invalid
requests and unavailable optional features are typed refusals regardless of `check`.

### File operations and bound policy

File calls accept paths/data and explicit metadata/publication options, not shell source, remote
callbacks or arbitrary tool arguments. Proposed forms below assume a file-only target with the
necessary pre-bound grants; `sudo=True` requests the granted implementation privilege, not a new
grant. The machine roots below already exist in 0.19.0's core policy. User settings and tmux paths
are existing consumers whose narrower file grants must be reviewed during cutover, not blanket home
or `/run` access. `user_files` is the ordinary-user view and `settings_path` is that user's resolved
`.claude/settings.json`; both come from authorized composition/domain inputs. The owning session
operation has already established that `stale_socket_path` belongs to it and has no live tmux
server:

```python
files.ensure_directory("/etc/claude-code", owner="root", group="root", mode=0o755, sudo=True)
files.write_file("/etc/claude-code/CLAUDE.md", instruction_bytes, expected=revision, sudo=True)
user_files.update_json(settings_path, settings, strategy="merge-overwrite", create=False)
files.ensure_directory("/opt/agentworks/artifacts", owner="root", group="root", mode=0o755, sudo=True)
files.remove(stale_socket_path, expected_type="socket", expected=socket_revision, sudo=True)
```

Exact option types belong in the file LLD. The intended behaviors are:

- `read_file`/`stat` return bounded content or metadata only under the respective read grant. The
  read/stat result can carry a revision for `write_file(..., expected=revision)` and conditional
  removal. `list_directory` bounds entries/depth and refuses unsafe traversal; no generic remote
  predicate or arbitrary command is accepted. Exact snapshot/limit types belong in the LLD.
  Regular-file transfers never open FIFOs/devices or follow unapproved links.
- `write_file`/`upload` publish complete bytes with explicit create/replace and metadata rules;
  neither implicitly creates parents nor inherits unrestricted chmod/chown authority.
- `update_json` preserves the shipped settings vocabulary: explicit `replace`, `merge-overwrite`,
  `merge-preserve` or `skip-existing`. Merge recurses into objects; arrays/scalars follow the
  winning side, and JSON null is a value, not deletion. Source validation always applies; replace
  need not parse old bytes, skip preserves existing bytes, and merge parses the old document.
  Missing files and create permission remain distinct from an existing empty/invalid file. These are
  not RFC 7396 semantics. No new patch/deletion language is required for initial delivery.
- `ensure_directory` distinguishes absent, matching and conflicting object types; it never replaces
  a conflicting object implicitly. Changing metadata needs its own grant. New objects have safe
  initial permissions. There is no FIFO or socket-creation API in the initial contract.
- `set_metadata` is bounded by approved owner/group/mode values. `remove` defaults to one object of
  the expected type; recursive deletion and approved-root removal need separate explicit authority.
  Directory extraction checks every entry and does not introduce link escapes.

Every mutation uses the immutable core ceiling intersected with recipient path/action and
metadata/elevation grants. Known denials raise `AuthorizationError` before staging or dispatch;
destination-side object checks enforce FRD R7 at use time, under the
[file-safety ruling](frd.md#file-safety-and-guest-runtime-rulings). Those checks do not promise
containment of malicious processes already running as the target user. Public reads have their own
path grants. The mutation ceiling here covers the bound destination filesystem; local download
publication also needs the owning operation's explicit local destination, not an implied workstation
sandbox.

Core entries distinguish an exact file, descendants of an approved root, and explicit root creation
or removal. Core alone supplies trusted identity-based path expansion. A caller-controlled path,
environment, symlink or derived view cannot widen it. An exact-file grant does not confer arbitrary
sibling writes: private staging/publication/lock names are internal authority with owned cleanup,
not a public parent grant. Helpers and carrier optimizations must honor the same boundary.

Mutation results report changed/unchanged and safe publication evidence, with typed conflict,
failure or uncertainty. A lost acknowledgement is not permission to repeat a merge or deletion.
Merge's internal read is authorized by its operation, but returns neither the old document nor a
content diff without a separate read grant. All mutation methods participate in the file LLD's
cooperating-writer protocol. Its external-writer limits, metadata preservation, path/mount trust
assumptions, cleanup and failure behavior must be proven in the file-only acceptance slice.

Generated-section edits and TOML serialization stay in the resource domain, using authorized
read/snapshot and conditional publication, not caller-supplied remote callbacks. Shared pure merge
logic may be copied into the new implementation where useful. The
[migration inventory](migration-strategy.md) owns the disposition of the shipped `NativeFiles`
facade and its callers.

### Permission-scoped access after legacy removal

The proposed view has two passive accessors. Their interfaces use the operation vocabulary above;
the view itself has no forwarding `run`, `upload`, or other all-authority convenience methods.

| View accessor                                       | Exposed operations when granted                                                                                                                   |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `execution()` returning `ExecutionAccess` or `None` | `run`, `start`, observation/output/wait, `stop`, `attach` and disposal; actions, profiles, lifetime, I/O and elevation are independently granted. |
| `files()` returning `FileAccess` or `None`          | R7 file/JSON/directory operations, with independently bound read, mutation, metadata and removal grants.                                          |

These are small typed interfaces over shared execution mechanics, not new transport subclasses or a
generic permissions registry. Withhold a whole interface when none of its actions is granted. For a
partially granted interface, expose its bound allowed actions as passive metadata and reject a
method lacking a grant with `AuthorizationError` before preparation, local/remote file I/O or
dispatch when denial is knowable from the bound grant, regardless of `check`. Filesystem-dependent
confinement checks occur at the destination before the protected mutation. Do not dynamically delete
Python methods or rely on callers checking metadata to enforce the restriction. Exact grant value
representations belong in the LLD.

The fine-grained values intentionally establish the future permission-ready contract now; they are
not evidence of a shipped registration/consent mechanism. Core constructs them directly, with no
policy DSL or redundant registry. A file-only view need not receive every path in the core ceiling.

The composition root binds recipient, identity, route, permitted actions and elevation once before
delivery. Core composition binds file grants within the core allowlist at activation. Future
registration requests and user approval can select narrower grants, but that workflow is out of
scope. A plugin-supplied name, `OperationScope`, or request flag cannot authorize it. No public
accessor returns the unrestricted implementation or carrier. Derived environment/shell views
preserve or narrow grants and lifetime. Grant selection and checks are distinct from channel
features and guest OS permissions; authorized recovery composition still receives all required
operations.

Checks govern the requested public action, not its private implementation steps. A granted upload
may use internal command delivery for staging without exposing `ExecutionAccess`; a granted script
may stage source without exposing `FileAccess`. Internal helpers cannot be requested as a back door
to arbitrary execution through a file-only interface. Validate that boundary, not that no internal
command was used. As FRD R9 states, an arbitrary foreground or detached execution grant already
conveys the execution account's guest authority, including filesystem and configured sudo powers;
these in-process views are not a plugin sandbox. MANAGED supplies lifecycle ownership, not hostile
target-user containment. Authorize the requested public action: `run` using shared launch/wait
machinery does not require a public `start` grant. Existing-job operations recheck current authority
and the bound job profile; possession of a reference is not a grant.

The public surface does not expose SSH credentials, provider task IDs, or a carrier constructor.
`RunContext.admin_execution_target()` and `.agent_execution_target()` return
`ExecutionTarget | None`, with each supplied view scoped to that recipient after activation. Context
composition retains a non-sensitive absence reason distinguishing unavailable lifecycle state from
withheld authority; the LLD specifies its representation. An absent interface is not a claim that
the carrier cannot implement it. `RunContext` stays in `capabilities/base.py`; it is not cloned into
the execution package. During coexistence, the new accessors sit beside unchanged legacy accessors.
The composition root owns target/resource closure; retaining a target cannot extend its authorized
lifetime. A later job observer receives a newly authorized target.

## Carrier contract

The mandatory internal carrier primitive is synchronous from the caller's perspective. QGA may poll
internally and SSH may drain subprocess pipes concurrently; the common layer need not manage either
kind of provider handle. Jobs use the shared host-side job protocol above this seam, not a second
carrier-specific detached API.

```python
class Carrier(Protocol):
    features: ChannelFeatures

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
    ) -> CarrierReport: ...
```

This is a private adapter-author seam, not a plugin alternative to `ExecutionTarget`. A platform
plugin can implement it but ordinary capability consumers cannot use it to bypass bound policy.
Optional terminal/live-streaming behavior is selected explicitly through `CarrierIO` and refused
before dispatch when absent from the channel's one immutable feature description.

| Value                | Contract                                                                                                                                                                                                                                      |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PreparedInvocation` | Literal bootstrap argv, with no stdin field. Application shell, final identity, env and cwd are already prepared above the carrier. Any helper interpreter is explicit. Payload-bearing fields have no diagnostic representation.             |
| `CarrierIO`          | One explicit input choice: EOF, finite source, live source, or terminal endpoint. Output is bounded capture, discard, explicit byte-stream sinks, or terminal presentation. Carries effective sensitivity and authorized presentation policy. |
| `Deadline`           | Remaining total budget, passed through local startup, dispatch and observation; never restarted for each poll. An explicitly unbounded operation remains distinct from a default.                                                             |
| `CarrierReport`      | Dispatch evidence (`not_sent`, `sent`, or `unknown`), completion evidence, observed guest status if known, carrier/local status separately, available output with completeness/provenance, and safe diagnostics.                              |

`sent` means the delivery request was submitted, not that the application started or finished.
`not_sent` requires positive evidence that no remote dispatch could have occurred. Raw completion
records the observed remote command-chain exit or signal. It can include destination account-shell
startup or refusal before the prepared bootstrap runs; it does not independently prove bootstrap or
application execution. A local SSH process terminating is not by itself proof of guest completion.
For nested delivery, outer completion cannot establish an inner guest outcome; the remote Lima
adapter cannot manufacture guest status from an ambiguous inner hop.

Shared preparation/outcome interpretation owns the evidence needed for a public application result.
Captured guest streams require valid framing. Suppressed or discarded output supplies no framing
proof, and neither raw zero nor absent retained bytes alone establishes application success. The
buffered PoC exposes internal evidence only; it does not implement or waive the public-result gate.

### Input and stream ownership

All carrier input lives in `CarrierIO`, as one choice rather than fields that conflict across
objects. EOF closes the carrier-owned input channel immediately. A finite source supplies a bounded
byte sequence and then EOF. A live source requires direct live stdio support, and a terminal
endpoint requires terminal support. Neither inherits workstation stdin accidentally. Terminal
attachment selects terminal presentation, not an application shell, and cannot claim separate
byte-exact guest stdout/stderr. Input/output pairing and sensitivity checks occur before dispatch.

The caller owns sources, sinks and terminal endpoints it supplies; the carrier borrows them for the
duration of `execute` and never closes them. The carrier alone consumes the selected input for that
attempt, and owns/closes the pipes and other local delivery resources it creates. EOF on a source
closes the outgoing input channel, not the caller's stream; observation continues until completion
or the operation deadline. Returning or raising leaves no background pump using a borrowed stream.
The shared preparation layer owns its temporary finite sources and closes them after the carrier
finishes. There is no hidden rewind, reuse, or retry of a consumed input source.

Input pumping and output draining are concurrent where the carrier requires it. Flow control must
bound buffering without deadlocking duplex commands. Supplied live sources and sinks must satisfy a
bounded-cancellation contract; an arbitrary blocking callback cannot be advertised as honoring a
deadline. The proof must settle the concrete stream shape, including short writes and EOF, before
the two implementations proceed independently.

A source/sink failure is a local I/O failure with safe partial execution evidence and explicit
output completeness. Stop pumping, perform bounded local cleanup, and report the failure; do not
retry, report successful overall execution, or claim that cleanup stopped the guest. If guest
completion was independently observed, retain that fact alongside the I/O failure. Control-flow
interruption still propagates as described below. No failed sink becomes a silent output discard.

Public script source and application stdin remain distinct. Preparation may encode a bootstrap input
carrying both, or use permitted staging, but only `CarrierIO` supplies the carrier input and the
script must receive its own application bytes/EOF. The proof must demonstrate the selected
mechanism, including sensitive-input suppression and a readiness path that stages nothing.

Each call makes at most one dispatch attempt. Idempotent status polling is allowed; reconnecting and
resending the invocation is not. On timeout or connection loss, the carrier returns the available
partial evidence after bounded local cleanup. Stopping the local SSH process does not claim remote
cancellation. Operational exceptions must retain the same safe partial report; request validation
may fail before dispatch. Only the owning operation can authorize a new attempt when it knows
repetition is safe.

Control-flow interruption, including `KeyboardInterrupt`, propagates after bounded local cleanup,
regardless of `check`. Safe partial evidence may accompany it but must not convert it to an ordinary
returned result or checked-command error. The owning operation's interrupt rollback must still run;
remote cancellation remains a separate explicit action.

For the buffered PoC, the operator accepted deferring guest cancellation on 2026-09-17. Live
deadline tests confirmed that ordinary guest processes and bootstrap descendants can remain running
after local observation ends. This is a measured limitation, not just an uncertain termination
report. The PoC has no cancellation handle or guest reaper; it must not back production operations
until shared workload ownership and cancellation are implemented and validated. Do not turn a local
deadline into an implicit guest kill or replay an uncertain command. The later execution/job
lifecycle must provide explicit cancellation of owned ordinary descendants without depending on
either carrier maintaining its original connection.

Captured bytes are not silently truncated: limits produce explicit incomplete-output evidence.
Common helpers arrange bounded transfer/spooling for required large data, while readiness targets
refuse implicit staging. Live sinks are drained without unbounded buffering; terminal output is
combined rather than presented as separate streams. OpenSSH client diagnostics can share stderr with
the remote program, so an adapter must identify mixed provenance rather than assert it is pure guest
stderr. Neither arbitrary stderr text nor status 255 establishes a specific connection fault. The
common layer must satisfy FRD R4's distinct guest-stream contract, not relabel a mixed carrier
stream as guest stderr. Its LLD must settle separation/framing and readiness-compatible behavior
before implementation; the low-level evidence model is not a weaker public output contract.

### SSH binding

The operator's OpenSSH minimum is 8.5. Before accepting the proof, the SSH design records exactly
which binaries and execution locations this covers, including workstation and platform-host clients
and any provider-launched inner clients. Server compatibility is recorded separately; do not infer a
new server-version requirement from a client-side check.

Proposed construction is `SSHCarrier(connection: SSHConnection)`. The connection is a new immutable
value with explicit host/port, account, configured identity and independently selected agent,
host-key lookup identity, trust-file policy, and permitted connection options. It contains resolved
inputs, not a VM or global configuration loader. Construction and feature inspection perform no I/O.
Route activation, credential resolution and any separately requested forwarding lifetime belong to
the composition root, not to `execute` or `RunContext` accessors.

The SSH implementation owns installed OpenSSH invocation, isolated client configuration, trust
enforcement, byte-safe subprocess I/O, and carrier bootstrap encoding. It serializes prepared argv
through the supported remote account shell without selecting the application's interpreter or adding
sudo/login wrappers. The shell LLD must specify supported account-shell/bootstrap combinations and
refusal behavior, including startup hooks that execute before the payload.

There is no SSH completion-envelope protocol in this proposal. The
[OpenSSH client manual](https://man.openbsd.org/ssh.1) describes process status and command
delivery; in particular, OpenSSH status 255 remains ambiguous unless independent shared
execution/job evidence establishes the guest outcome. No new SSH library, reconnect manager, or
connection pool is implied. Optional SCP acceleration uses the same new connection/trust option
builder and remains beneath common file publication semantics; it is not necessary to implement the
mandatory carrier seam.

### SSH-backed platform-host binding

The same `SSHCarrier(SSHConnection)` can back a platform operation's host target or a guest target.
It accepts no Lima configuration or VM-platform discriminator. Platform composition binds the host
identity, environment/shell policy and lifetime using the common target machinery; no guest identity
is needed for pre-creation management work. Remote Lima is the first adapter to consume this
pattern, not its owner or the route through which another platform must obtain host access.

Platform adapters own management-tool invocation, any inner guest hop, and application of reusable
SSH policy to those paths. Host completion proves only the host invocation; a guest outcome needs
evidence from the inner delivery. Shared host files/jobs use actual host identity and supported host
userspace. Reuse this composition rather than introducing another SSH runner or a generic
virtualization framework.

## Filesystem and package layout

These are proposed destination paths, not directories to create in this documentation revision. Use
the permanent name `execution`, not `transports_v2` or a second installed distribution.

```text
cli/agentworks/
  execution/
    __init__.py                 scoped target, access interfaces and caller values
    models.py                   command, shell, input/output, result and job values
    target.py                   bound execution mechanics and target view
    access.py                   execution/file access with independently bound action restrictions
    profiles.py                 core protection guarantees and exact profile grants
    preparation.py              identity, shell, env/cwd and helper preparation
    files.py                    transfer, JSON updates, inventory, metadata and publication semantics
    file_policy.py              core allowlist, scoped file grants and confinement policy values
    jobs.py                     shared launch, lifetime, observation, stop and evidence
    systemd.py                  private Linux managed-boundary mechanism, after lifecycle proof
    diagnostics.py              safe execution diagnostics, no legacy SSHLogger
    carrier.py                  leaf carrier protocol and carrier-only values
    carriers/
      _subprocess.py            bounded local process I/O; carrier owns evidence interpretation
      ssh/
        __init__.py             SSHCarrier and SSHConnection exports
        connection.py           explicit connection value and option policy
        client.py               new SSH/scp process delivery
        trust.py                trust policy and preservation of existing records
        forwarding.py           explicitly owned forwarding resources
      lima.py                   new host-local carrier
      remote_lima.py            first platform consumer of reusable SSH host access
      wsl2.py                   new workstation-local carrier
  plugins/proxmox/execution.py  new QGA adapter, no legacy transport imports
  capabilities/base.py         existing RunContext, additive new accessors, then legacy removal

cli/tests/
  execution/                   public behavior, helpers, dependency isolation
    carriers/ssh/              independent new SSH fixtures and carrier tests
  plugins/test_proxmox_execution.py
```

This is one contract module and concrete implementations, not a class hierarchy for each directory.
Split implementation files further only when their size/responsibility earns it. Adapter-neutral
conformance cases live under `tests/execution`; provider tests invoke those cases plus provider
limits. Copy/adapt useful fixtures into the new test tree instead of importing legacy test setup or
factories. New tests must not need the old implementation to compute expected results.

Dependency direction is `callers -> execution public API -> target/helpers -> carrier protocol`.
Concrete carriers depend on that leaf protocol, not on target implementation or `RunContext`.
Composition roots in existing resource operations construct concrete carriers and bind targets; the
public package does not eagerly import every carrier or provider plugin. The Proxmox adapter may use
the plugin's retained API client, provided its dependency closure does not load legacy execution
code. That condition applies to plugin package initialization too. Today the Proxmox package eagerly
imports its platform, which imports the old transport. The independent build must disentangle that
import path while preserving existing plugin registration and production behavior; loading the new
adapter by bypassing package initialization in tests would not prove independence.

## Independence and removal boundary

The retirement set starts with `agentworks.transports`, `agentworks.ssh`, `agentworks.remote_exec`,
`agentworks.harness_setup.runner`, `agentworks.native_files`, and
`agentworks.plugins.proxmox.transport`. The new implementation and its tests must not import them,
subclass their types, call their functions, or reach them indirectly through shared utilities. This
includes old result/error/logger types, aliases and lazy imports. Merely renaming an import or
placing a facade in the new package does not satisfy independence.

Ordinary retained project facilities such as the base error taxonomy and terminal restoration may be
reused after a dependency audit. This does not require copying all of `agentworks`.
`ssh_config.py`'s operator-facing config generation and `ssh_identity.py`'s identity parsing are not
automatically retired because their names contain SSH; inventory their continuing consumers and
assign their retained or replacement home explicitly. The new carrier must not load connection
policy through the legacy execution runner. Shared data formats do not imply shared runtime code.

Copied working code is a starting point, not a compatibility promise. Record provenance in the
implementation commit, remove legacy dependencies, adapt its semantics, and carry over useful
behavioral tests. In particular, do not copy implicit shell choice, automatic command replay, or
newline-normalizing output into the new contract.

Before integration, run the new-stack suite with the retirement modules unavailable in an isolated
test environment, including package import and provider construction. At final cutover physically
delete the retirement set and prove installed-package startup, production imports, core/plugin
workflows and supported platform tests still pass. A static import check can complement that test
but cannot prove indirect or runtime-loaded independence on its own. Expand the removal inventory
when another legacy dependency is found; do not whitelist a bridge to make the check pass.

State migration is separate: configuration and trust preservation code belongs to the new SSH
package, recognizes existing data directly, and neither resets known-host evidence nor invokes old
code. An already-owned trust file can be reused when policy permits; imported operator trust uses
explicit owned copies as proposed in #796, preserving complete records, aliases and revocations
without a custom trust parser. Conversion must preserve rollback evidence and resolve concurrent
writers before production switches. The [migration strategy](migration-strategy.md) owns cutover
order and surviving legacy jobs.
