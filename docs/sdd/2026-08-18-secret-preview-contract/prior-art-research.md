# Prior art: intent-aware, value-free secret preview

- Date: 2026-08-18
- Scope: TTY capability, provider authentication modes, value containment, and backend-owned probes
- Sources: current Agentworks code plus primary language and vendor documentation

## Executive summary

The useful prior art supports a narrow conclusion: terminal attachment can answer whether stdin is
usable, but it cannot answer whether an external provider will involve a person. 1Password itself
supports both desktop-app authentication and service-account authentication for the same `op read`
operation. The backend is therefore the component with enough knowledge to interpret caller intent.

The provider documentation also does not offer a value-free existence API for the native secret
reference path Agentworks uses. A backend-owned read followed by immediate discard is the most
truthful probe. The safe abstraction is consequently a value-free return type and a strict boundary,
not a claim that no plaintext ever exists inside provider code.

## Findings and design consequences

### A TTY describes a stream, not the operator

Python documents `stdin` as the stream used for interactive input and describes console behavior in
terms of `isatty()`. That makes TTY state an appropriate guard for code that reads stdin. It says
nothing about a desktop application, mobile approval, network identity, or service account.

Design consequences:

- TTY availability is an execution fact, separate from operator-impact authority.
- Prompt must require a usable terminal before reading.
- A non-TTY process does not by itself block OnePassword or any other out-of-band backend.

Source: [Python `sys` documentation](https://docs.python.org/3/library/sys.html#sys.stdin)

### One provider command can use materially different authentication paths

1Password's desktop-app integration can authenticate CLI commands with a fingerprint, face, Apple
Watch, Windows Hello, device password, or system authentication. The documentation says entering a
CLI command can trigger that authentication. Separately, 1Password service accounts authenticate the
CLI through `OP_SERVICE_ACCOUNT_TOKEN`, and `op read` is a supported command in that mode.

Design consequences:

- `onepassword` cannot truthfully be classified once as interactive or non-interactive.
- The backend may infer a known unattended mode from provider-specific ambient facts.
- Source config may classify otherwise uncertain app authentication according to the operator's
  impact preference.
- The impact decision belongs beside the `op` invocation, not in core's TTY test.

Sources:

- [Use the 1Password desktop app to sign in to 1Password CLI](https://www.1password.dev/cli/app-integration)
- [Use service accounts with 1Password CLI](https://www.1password.dev/service-accounts/use-with-1password-cli)

### The native lookup path is a value-returning read

1Password documents `op read` as the way to read a secret reference in scripts. It returns the
secret for use by the invoking command. The documented path does not provide a separate
existence-only operation with equivalent addressing and authentication behavior.

Design consequences:

- A definitive OnePassword preview may need to perform the real bounded read.
- The backend must convert the native result directly into a closed preview answer and discard the
  value before returning.
- A core adapter around a value-returning legacy backend is not equivalent, because plaintext has
  already crossed the backend contract boundary.
- Preview and resolution should share one private acquisition and failure-classification path so
  they do not disagree about provider behavior.

Sources:

- [1Password CLI `read`](https://www.1password.dev/cli/reference/commands/read)
- [Load secrets into scripts](https://www.1password.dev/cli/secrets-scripts)

### Agentworks already has the right containment primitives

The current OnePassword backend owns a bounded subprocess seam, removes native stderr before errors
cross the boundary, and classifies authentication, mapping, connectivity, timeout, and external
failure into closed types. The current resolution layer also keeps values out of `ResolutionOutcome`
and uses redacted representations. These are stronger foundations than a new generic probe helper.

Design consequences:

- Extend the source-client boundary instead of adding provider awareness to core.
- Reuse the existing timeout and failure normalization for preview.
- Remove the redundant backend remediation selection. A closed failure detail is enough for core to
  choose a command-specific hint.
- Preserve sentinel-based leak tests across values, exceptions, representations, logs, and machine
  output.

Sources:

- `cli/agentworks/plugins/onepassword/backend.py` at baseline `c01263d0`
- `cli/agentworks/capabilities/secret_backend/client.py` at baseline `c01263d0`
- `cli/agentworks/secrets/outcomes.py` at baseline `c01263d0`

### Preflight is already a consumer of preview

The operation preflight path runs
`preflight_all -> require_predicted_refs -> predict_resolution -> preview_operation_resolution`. Its
current preview is pure and optimistic, but it still decides whether a missing mapping or refused
interactive source makes preflight fail.

Design consequences:

- Preview cannot be designed only for `secret describe`; preflight semantics must change with it.
- Preflight remains an impossibility screen. It requests non-disruptive work, rejects a definitive
  `no`, and accepts `maybe` rather than converting uncertainty into a false failure.
- The later value-bearing resolution boundary remains authoritative and must still run before
  consuming mutations.

Source: current Agentworks orchestration and preview modules at baseline `c01263d0`

### The secret-backend version is registration-only

The original constructed-singleton contract declared version `1`. The August 8 class-based rewrite
changed the descriptor and all three in-tree implementations to `2`. At the current baseline that
integer is read only by exact registration conformance; it is not stored in config, manifests,
resource graph data, or machine output. No external secret-backend implementation exists.

Design consequences:

- The new atomic rewrite can reset the descriptor, implementations, author documentation, and tests
  to `1` without a persisted-data migration or compatibility branch.
- The reset is an intentional re-baseline of a shipped internal-only extension point, not a claim
  that the historical version-1 and version-2 shapes never existed.
- Versions of other capability kinds remain independent and unchanged.

Sources:

- `cli/agentworks/capabilities/secret_backend/kinds.py` and conformance code at baseline `c01263d0`
- Git history for `c10c7bb7` (`feat(secrets): define class-based backend contract`)

## Refuted or not adopted

- **TTY as interaction policy:** valid only for stdin access and disproved by out-of-band app
  authentication and service-account modes.
- **One static backend interaction flag:** cannot describe provider state that varies by source,
  authentication mode, and invocation.
- **Separate preview and probe methods:** creates two answers that can drift. One method with an
  impact allowance makes the backend own the best available answer.
- **Requested certainty or `allow_maybe` input:** duplicates operator impact as a second policy
  dimension. Callers interpret the result instead.
- **Core-side resolve and discard:** violates the intended value boundary even if the CLI never
  renders the value.
- **Provider remediation or free-form messages:** unnecessary authority and a potential secret or
  control-character channel. Closed details let core derive safe, contextual guidance.
- **Readiness as existence:** readiness is offline host support and cannot prove a per-secret remote
  lookup.

## Open questions the sources do not settle

- 1Password does not expose enough pre-invocation information to distinguish a cached app session
  from a biometric or password request in every environment. The initial source config must classify
  app authentication as one unit rather than promise biometric-only detection.
- No external Agentworks secret-backend plugins exist. The contract can therefore be rewritten with
  every implementation in one atomic change and no compatibility layer.
- Future providers may offer a metadata-only existence API. The contract permits its use but does
  not require it when the provider's real read is the only authoritative probe.

## Source quality

| Source                                 | Quality                | Angle used                                                     |
| -------------------------------------- | ---------------------- | -------------------------------------------------------------- |
| Python standard-library documentation  | Primary language docs  | TTY and stdin scope                                            |
| 1Password desktop-app integration docs | Primary vendor docs    | Out-of-band and biometric authentication                       |
| 1Password service-account docs         | Primary vendor docs    | Known unattended authentication for `op read`                  |
| 1Password `read` and scripting docs    | Primary vendor docs    | Native value-returning lookup path                             |
| Agentworks source at `c01263d0`        | Primary implementation | Current preview, preflight, timeout, and containment contracts |
