# Transport Improvements: Proposed Contract and Package Layout

- Status: Design proposal for operator and SSH-developer review, not a shipped API
- Governing requirements: [FRD](frd.md), especially R2-R6, R9-R11
- Architecture and ownership: [HLA](hla.md)

This is the proposed implementation boundary for parallel work. It gives concrete interface shapes
without claiming the shell bootstrap, file transfer, or job protocol LLDs are complete. Agree on
this seam before either effort builds against it. The transport effort owns the common contracts;
the SSH effort owns its new carrier implementation. Neither imports the legacy execution stack.

## Caller contract

Core and plugin consumers import public types from `agentworks.execution` and normally receive an
`ExecutionTarget` from `RunContext`. The following Python-shaped examples propose names and argument
forms, not executable code or a second command language:

```python
target.run(["tool", "--name", name], check=True)
target.script(source, shell=Shell.fixed("bash"), stdin=Input.sensitive(secret_bytes), sudo=True)
target.script(source, shell=Shell.user_default(login=True), cwd=remote_directory)
target.run(["tool"], stdin=Input.live(source_stream), output=Output.stream(out_sink, err_sink))
job = target.start(Script(source, shell=Shell.fixed("sh")), sudo=True)
status = target.observe(job)
result = target.wait(job, deadline=deadline)
target.cancel(job, deadline=cancel_deadline)
target.dispose(job)
```

`run(argv, ...)` is literal execution; `script(source, shell=..., ...)` is shell execution. Both
wait for completion and return `ExecutionResult`.
`start(Command(argv) | Script(source, shell), ...)` uses the same options but returns a
credential-free `JobRef` after launch acknowledgement. A lost acknowledgement produces an
uncertain-start outcome with any safe reconciliation reference, never automatic relaunch. There is
no anonymous `background=True` or caller-written `nohup` requirement.

Shared keyword options are `sudo`, `env`, `cwd`, `stdin`, `output`, `sensitive`, and `deadline`.
Foreground calls also accept `check`; `wait` accepts it when collecting a job result. Elevation is
non-interactive and constrained by the bound identity. Shell defaults are absent unless deliberately
bound by the operation; a script without either an explicit policy or that bound default is
rejected. `Shell` separates interpreter choice from login and interactive startup as specified in
the HLA.

Finite byte input works on every target; omission means EOF, not inherited console input. Foreground
calls may explicitly select `Input.live(source)` for non-terminal piped or duplex work on a channel
with direct live stdio. This does not allocate a PTY. The carrier pumps that source with the
selected output sinks; an unsupported channel refuses before dispatch. Detached launch rejects live
input, since its input must be delivered independently of the initiating connection. Script source
has its own delivery path and never consumes application stdin.

Output defaults to bounded capture, with explicit discard or direct-streaming modes. Effective
sensitivity combines bound environment metadata, an input sensitivity marker, and the request-wide
`sensitive=True` option for caller-supplied source, environment or other payloads. A call can add
protection but cannot downgrade a sensitive bound value. Preparation preserves this designation
through staging and `CarrierIO`; carriers do not infer sensitivity from content. FRD R4 owns the
resulting suppression and authorized live-presentation policy. `env` and `cwd` affect the final
execution identity, not the workstation SSH process. A deadline is one monotonic budget through
preparation and observation; omission follows the explicitly bound operation policy, not a
carrier-selected timeout or retry default.

| Surface                                  | Proposed behavior                                                                                                               |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `features`, identity/route metadata      | Passive values from the bound target; no connection or authority discovery.                                                     |
| `write_file`, `upload`, `download`       | Bytes or explicit local/remote paths, permissions and publication policy; atomic publication only after complete transfer.      |
| `upload_directory`, `download_directory` | Explicit merge/replace choice, confined paths and extraction; no implicit recursive deletion.                                   |
| `observe`, `read_output`, `wait`         | Separate status, bounded cursor-based output reads, and waiting for a known job; observing never deletes its records.           |
| `cancel`, `dispose`                      | Request cancellation with truthful confirmation, or dispose owned terminal-job artifacts; neither guesses authority from a PID. |
| `interactive(invocation, ...)`           | Explicit command or shell invocation with terminal attachment; optional feature, not an implicit login-shell selector.          |

`ExecutionResult` carries available guest exit status, byte output and completeness, and the outcome
facts needed to distinguish failure, timeout, and uncertainty. A typed execution error carries the
same safe result facts when `check=True`; `check=False` exposes them without turning uncertainty
into an ordinary guest exit. Output decoding is explicit and does not normalize raw bytes. Invalid
requests and unavailable optional features are typed refusals regardless of `check`.

The public surface does not expose SSH credentials, provider task IDs, or a carrier constructor.
`RunContext.admin_target()` and `.agent_target()` become `ExecutionTarget | None` at cutover.
`RunContext` stays in `capabilities/base.py`; it is not cloned into the execution package. During
development, test composition supplies targets without changing production context types. The
composition root owns target/resource closure; retaining a target cannot extend its authorized
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

| Value                | Contract                                                                                                                                                                                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `PreparedInvocation` | Literal bootstrap argv, finite input source, and safe diagnostic label. Application shell, final identity, env and cwd are already prepared above the carrier. Any helper interpreter is explicit. Payload-bearing fields have no diagnostic representation. |
| `CarrierIO`          | Capture with explicit bounds, discard, explicit byte-stream sinks, or terminal attachment. Input endpoint and terminal behavior are explicit; sensitive input cannot accidentally inherit a terminal or logging sink.                                        |
| `Deadline`           | Remaining total budget, passed through local startup, dispatch and observation; never restarted for each poll. An explicitly unbounded operation remains distinct from a default.                                                                            |
| `CarrierReport`      | Dispatch evidence (`not_sent`, `sent`, or `unknown`), completion evidence, observed guest status if known, carrier/local status separately, available output with completeness/provenance, and safe diagnostics.                                             |

`sent` means the delivery request was submitted, not that the application started or finished.
`not_sent` requires positive evidence that no remote dispatch could have occurred. Completion is
reported only from evidence about the submitted invocation. A local SSH process terminating is not
by itself proof of guest completion. For nested delivery, the outer report proves only the outer
invocation; the remote Lima adapter cannot manufacture guest status from an ambiguous inner hop.

`CarrierIO` carries the effective sensitivity and any explicitly authorized live presentation.
Finite prepared input and an explicit live input source are mutually exclusive; the latter is
available only for the live-stdio feature or explicit terminal attachment. Neither mode inherits
workstation stdin accidentally.

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

## Filesystem and package layout

These are proposed destination paths, not directories to create in this documentation revision. Use
the permanent name `execution`, not `transports_v2` or a second installed distribution.

```text
cli/agentworks/
  execution/
    __init__.py                 public ExecutionTarget and caller value exports
    models.py                   command, shell, input/output, result and job values
    target.py                   bound public operations and lifetime enforcement
    preparation.py              identity, shell, env/cwd and helper preparation
    files.py                    bounded transfer and publication semantics
    jobs.py                     shared job protocol and observation
    diagnostics.py              safe execution diagnostics, no legacy SSHLogger
    carrier.py                  leaf carrier protocol and carrier-only values
    carriers/
      ssh/
        __init__.py             SSHCarrier and SSHConnection exports
        connection.py           explicit connection value and option policy
        client.py               new SSH/scp process delivery
        trust.py                trust policy and preservation of existing records
        forwarding.py           explicitly owned forwarding resources
      lima.py                   new host-local carrier
      remote_lima.py            new nested delivery using the new SSH carrier
      wsl2.py                   new workstation-local carrier
  plugins/proxmox/execution.py  new QGA adapter, no legacy transport imports
  capabilities/base.py         existing RunContext, updated at cutover

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
`agentworks.harness_setup.runner`, and `agentworks.plugins.proxmox.transport`. The new
implementation and its tests must not import them, subclass their types, call their functions, or
reach them indirectly through shared utilities. This includes old result/error/logger types, aliases
and lazy imports. Merely renaming an import or placing a facade in the new package does not satisfy
independence.

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
explicit owned copies as proposed in #757, preserving complete records, aliases and revocations
without a custom trust parser. Conversion must preserve rollback evidence and resolve concurrent
writers before production switches. The [migration strategy](migration-strategy.md) owns cutover
order and surviving legacy jobs.
