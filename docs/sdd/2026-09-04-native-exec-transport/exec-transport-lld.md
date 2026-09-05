# Native Execution Transport: Low-Level Design

- Status: Design
- Date: 2026-09-04
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Design invariants

1. Canonical operator work uses Tailscale transport and never silently falls back.
2. Every vm-platform implementation returns a native `ExecTransport`.
3. Core native operations depend only on `run`, `describe`, logger, and timeout behavior.
4. Only `vm shell --platform` asks whether the native value is a full `Transport`.
5. `sudo=False` means VM admin authority on every implementation, including QGA.
6. No stdin payload means immediate EOF. Sensitive stdin never appears in observable diagnostics.
7. A Proxmox timeout does not imply cancellation and never triggers blind redispatch.
8. The transport and vm-platform capability contracts remain version 1.

## Type boundary

Conceptual shape in `agentworks/transports/base.py`:

```python
class ExecTransport(abc.ABC):
    default_timeout: int | None = None
    logger: SSHLogger | None = None

    def _resolve_timeout(self, override: int | None) -> int | None: ...

    @abc.abstractmethod
    def describe(self) -> str: ...

    @abc.abstractmethod
    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        tty: bool | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        input_data: str | None = None,
        discard_output: bool = False,
        retries: int | None = None,
        on_retry: Callable[[int, int], None] | None = None,
    ) -> SSHResult: ...


class Transport(ExecTransport):
    def interactive(...): ...
    @abc.abstractmethod
    def _interactive(...): ...
    @abc.abstractmethod
    def copy_to(...): ...
    @abc.abstractmethod
    def copy_from(...): ...
    @abc.abstractmethod
    def call_streaming(...): ...
    def copy_dir_to(...): ...
    def write_file(...): ...
```

The implementation moves the existing execution members into the new base without changing their
signature or default meanings. Full transport implementations change only their declared parent and
imports. The concrete interactive terminal guard and file helpers remain on `Transport`.

`SSHResult`, `SSHError`, and `SSHLogger` stay where they are. Their names predate polymorphic
transports and already serve Lima, WSL2, and remote Lima. This effort documents the compatibility
debt instead of multiplying the diff with aliases or a global rename.

## vm-platform API

Conceptual version-1 contract:

```python
@dataclass(frozen=True)
class ProvisionResult:
    platform_metadata: dict[str, str]
    native_transport: ExecTransport
    tailscale_ip: str | None = None


class VMPlatform(Capability):
    native_shell_unavailable_hint: ClassVar[str] = (
        "This platform has no interactive native shell."
    )

    @abstractmethod
    def native_transport(
        self,
        vm: VMRow,
        ctx: RunContext,
        *,
        config: Config | None = None,
    ) -> ExecTransport: ...
```

The exact hint attribute may preserve its current spelling if that avoids churn. Its responsibility
must become shell-specific. The hook has no default body and no `None` result. Registration
conformance instantiates every bundled class and proves an implementation exists.

The package factory retains its name and lifetime:

```python
def native_transport(
    vm: VMRow,
    platform: VMPlatform,
    config: Config,
    *,
    ctx: RunContext,
    stack: contextlib.ExitStack,
) -> ExecTransport: ...
```

It still enters `platform.transient_route`, builds the transport, validates an SSH-backed target,
and probes `echo ok`. The `None` branch is deleted. The probe uses `tty=False`, a finite timeout,
and no input payload, so stdin is EOF.

## Full-transport narrowing

`vm shell --platform` obtains the same native value and performs one structural branch before any
interactive call:

```python
target = native_transport(...)
if not isinstance(target, Transport):
    raise StateError(..., hint=platform.native_shell_unavailable_hint)
return target.interactive(command, env=env)
```

The canonical shell branch still obtains `transport(vm, config) -> Transport` and needs no check. No
other production caller performs this narrowing. A residual scan and an execution-only fake pin that
claim.

## Proxmox transport

### Module and construction

Place the adapter beside the platform, for example `agentworks/plugins/proxmox/transport.py`. It
depends on the Proxmox API client but the generic transport package does not depend on a plugin.

```python
class ProxmoxExecTransport(ExecTransport):
    def __init__(
        self,
        api: ProxmoxAPI,
        *,
        node: str,
        vmid: int,
        admin_username: str,
        logger: SSHLogger | None = None,
        default_timeout: int | None = None,
    ) -> None: ...
```

`describe()` returns a stable label such as `proxmox:<vmid>@<node>` and never includes the API
token. Constructor validation reuses established node, VMID, and username validators rather than
creating a second naming policy.

### Command renderer

One private pure helper returns the program and argument list accepted by QGA. It never receives
stdin data.

```text
ordinary authority:
  /usr/sbin/runuser -u <admin> -- /bin/bash -lc <scoped command>

root authority:
  /bin/bash -lc <scoped command>
```

The exact `runuser` path is confirmed against the supported Debian images during implementation. If
the stable Debian path differs, the implementation records and tests the confirmed value rather than
adding runtime discovery. Environment keys and values use the existing validated environment and
shell-quoting helper. The command remains one bash string so compound command behavior matches the
existing transport contract.

### API methods

Separate dispatch from polling so ambiguity remains visible:

```python
pid = api.guest_agent_exec(
    node,
    vmid,
    command=argv,
    input_data=payload,
    timeout=remaining,
)
status = api.guest_agent_exec_status(
    node,
    vmid,
    pid=pid,
    timeout=remaining,
)
```

The existing `guest_agent_exec_wait` may become a small composition over these methods for the
bootstrap caller, but the bootstrap flow itself is not redesigned. Every response is shape-checked:

- dispatch data is one integer PID;
- status `exited` is boolean;
- complete exit has either integer `exitcode` or integer `signal` under the provider's valid shape;
- output fields are strings when present; and
- truncation flags are boolean when present.

Unexpected or contradictory data raises a typed transport error with node, VMID, and PID but no
request body or token.

### Input policy

Validation order occurs before API dispatch:

1. reject simultaneous `input_text` and `input_data`;
2. reject `input_text` with `discard_output` under the existing contract;
3. reject `tty=True`;
4. encode the selected payload exactly as the existing transport contract requires;
5. reject more than 65,536 characters in the provider API field; and
6. build the QGA request.

With no payload, omit `input-data`. For `input_data`, return and log complete output normally. For
`input_text`, discard provider stdout and stderr before building a result or logger event.

Provider request tracing in focused tests records only field names and safe metadata for sensitive
mode. Production API exceptions must not render request bodies. If a provider exception could carry
unsafe context, translate it inside the handler and raise the safe transport error only after the
handler has exited, so Python does not retain the provider exception as active context.

### Timeout and polling

Resolve one deadline from the per-call override or transport default. Pass the remaining positive
budget to dispatch and every status request. Poll with the current short interval, capped by the
remaining deadline. Tests inject time and polling instead of sleeping.

Failure before a PID is returned is an ambiguous dispatch unless the provider definitively rejects
the request. Neither case is automatically retried by this adapter. Once a PID exists, a timeout
raises a safe error that carries the PID and says the process may continue. Agentworks performs no
cancel because no supported endpoint exists.

`timeout=None` retains the existing unbounded-call meaning, though native factory and recovery
callers continue supplying their established finite bounds. The implementation does not invent a
global timeout policy in this refactor.

### Result mapping

```text
exited, exitcode=N, complete output
  -> SSHResult(returncode=N, stdout=..., stderr=...)

exited, signal=S, complete output, check=False
  -> SSHResult(returncode=-S, stdout=..., stderr=...)

nonzero or signal, check=True
  -> existing checked transport failure

out-truncated or err-truncated
  -> typed failure; partial output is not returned as complete

discard_output=True
  -> preserve return code; return empty stdout and stderr
```

Logger behavior mirrors other transports: safe command and complete ordinary results are logged;
checked failures use the logger's error path. Sensitive mode does not log result streams. No
automatic retry callback fires.

## Proxmox platform integration

`ProxmoxPlatform.native_transport` reads `node` and `vmid` from the VM row, obtains the
authenticated API through the delivered operation context, and constructs `ProxmoxExecTransport`
with `vm.admin_username`.

`ProxmoxPlatform.create` already proves QGA availability before bootstrap. After successful
bootstrap it returns the QGA transport instead of a Tailscale `SSHTransport`. Core performs live
release attestation and Phase A work through QGA, then discovers or verifies Tailscale and switches
to the canonical transport at the existing boundary.

The existing private bootstrap staging retains its write, execute, cleanup, and interrupt tests. It
may call the split API methods internally if that is a mechanical consequence, but it does not
become a public file-transfer implementation.

## Error normalization

The implementation uses the established transport failure family so current orchestration catches
continue to work. Every Proxmox error includes safe target identity and operation phase where known:

- route or API authentication;
- QGA dispatch rejected or ambiguous;
- QGA status unavailable;
- deadline with PID and possible continued execution;
- invalid or truncated result; or
- checked guest exit.

The implementation does not include command input, provider request bodies, tokens, or captured
output from sensitive mode. `check=False` changes guest exit handling only; it does not turn invalid
provider data, truncation, or transport failure into a successful result.

## Permanent collateral

Implementation updates:

- `capabilities/README.md`, for the required native administrative execution principle;
- `capabilities/vm-platform/README.md`, for exact version-1 obligations and optional rich behavior;
- the published vm-platform capability description;
- `docs/guides/proxmox.md`, for QGA recovery and the interactive console limitation;
- nearby transport and platform docstrings; and
- tests and fixtures that teach the old optional return.

No sample config, CLI reference, command help, completion script, JSON schema, release migration, or
database documentation changes because none of those surfaces changes.

## Testing seams

### Generic contract

- `ExecTransport` abstractness and full `Transport` inheritance.
- Existing concrete full transports still satisfy both contracts.
- An execution-only fake passes create attestation, initialization, repair, rekey, logout, and
  native probe paths.
- No core execution-only path references interactive, streaming, or copy methods.
- `vm shell --platform` accepts full native transport and rejects the execution-only fake before
  interaction.

### Proxmox unit and API wire

- ordinary and root argv rendering, compound commands, environment, and quoting;
- default and overridden timeout;
- no-payload EOF and both stdin modes;
- mutual exclusion, TTY refusal, and 65,536-character provider-field boundary;
- exact request field names without payload exposure;
- PID polling to zero and nonzero exit;
- signal mapping under both `check` values;
- stdout and stderr truncation refusal;
- malformed dispatch and status shapes;
- QGA unavailable, request failure, timeout before PID, and timeout after PID;
- no redispatch or retry callback after ambiguity;
- ordinary logging and sensitive log, result, exception, and cause absence; and
- bootstrap staging cleanup remains unchanged.

### Platform conformance

- every version-1 platform overrides the required hook;
- `ProvisionResult.native_transport` accepts the narrower fake;
- full platforms return their existing concrete transport;
- Proxmox existing-VM and create results return `ProxmoxExecTransport`; and
- capability docs and fixtures contain no optional-native exception.

### Live validation

After 0.18.0 and after explicit implementation authorization:

- exercise one full native platform for create, recovery, and native shell regression;
- exercise Proxmox create-time release attestation through QGA;
- make Tailscale unavailable on an expendable Proxmox VM, then prove rejoin and rekey through QGA;
- prove canonical work does not fall back when Tailscale remains unavailable;
- prove `vm shell --platform` gives Proxmox console guidance; and
- inspect the operation log and failure surfaces for a known canary secret.

If no authorized Proxmox environment is available, the implementation cannot claim the Proxmox
acceptance criteria from mocks alone. The handoff records the missing live evidence for operator
disposition rather than relabeling it as complete.
