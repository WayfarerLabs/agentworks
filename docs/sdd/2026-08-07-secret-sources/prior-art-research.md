# Prior Art: Secret Sources

- Date: 2026-08-08
- Scope: configured secret stores, client lifetime, timeout cleanup, and KV lookup boundaries
- Sources: primary project and language documentation only

## Executive summary

The useful prior art points in one direction: model a configured store as a named resource that
selects exactly one provider, keep a secret's remote key separate from store configuration, and
bound any authenticated client to the operation that uses it. Agentworks should adopt that shape
without importing controller-specific machinery such as background reconciliation, persisted
credentials, refresh loops, or dynamic-secret leases.

The research also reinforces the FRD's scope boundary. External Secrets distinguishes configured
stores from generators, and Vault distinguishes KV reads from leased dynamic credentials. A
`secret-source` should remain a read-only KV store. Credential minting belongs to the consuming
credential domain.

## Findings and design consequences

### A configured store is an instance of one provider

The External Secrets Operator's `SecretStore` is a named configured resource that maps to exactly
one external API instance. Its provider block owns connection and authentication configuration;
secret consumers refer to the store and provide remote keys separately. Its ready condition also
belongs to the configured store rather than to the provider kind.

Design consequences:

- `secret-backend` is the implementation kind and `secret-source` is the configured instance.
- A source selects exactly one backend through a tagged capability block.
- Per-source account and transport settings live on the source. Per-secret mappings remain lookup
  addresses.
- Readiness folds on the source using the selected backend class and the source config.

Source: [External Secrets Operator SecretStore](https://external-secrets.io/main/api/secretstore/)

### Dynamic credentials and KV reads have different lifecycles

Vault attaches leases, renewal, and revocation to dynamic secrets, while noting that the KV engine
does not issue leases. That is evidence against stretching a KV source to cover credential creation.
A minting request carries creation policy and lifecycle semantics that a lookup address does not.

Design consequences:

- Source mappings contain addresses only.
- Resolution outcomes do not grow lease or renewal fields in this wave.
- Future minted credentials remain git-credential variants, consistent with the FRD boundary ruling.

Source: [Vault lease, renew, and revoke](https://developer.hashicorp.com/vault/docs/concepts/lease)

### Client lifetime must be explicit and bounded

Vault clients may hold renewable authentication state, and the External Secrets store model may hold
provider connection configuration. Even though Agentworks ships only stateless env-var, prompt, and
CLI-backed 1Password implementations today, the abstraction should not require future clients to
become process-global singletons.

Design consequences:

- The backend registry stores implementation classes, not constructed objects.
- Resolution constructs a source-bound backend only when that source has an attemptable secret.
- The backend opens at most one client for its batch, and the caller closes it before advancing to
  the next source.
- No client, authentication state, or resolved value survives the operation.

Sources: [Vault token API](https://developer.hashicorp.com/vault/api-docs/auth/token),
[Vault lease, renew, and revoke](https://developer.hashicorp.com/vault/docs/concepts/lease)

### Timeout ownership belongs at the external boundary

Python's `subprocess.run(..., timeout=...)` kills and waits for the child before raising
`TimeoutExpired`. This is stronger than wrapping a blocking call in a thread, whose work may
continue after the caller reports a timeout. The 1Password CLI is already invoked through one
subprocess seam, so that seam can enforce and translate the deadline without leaking a child.

Design consequences:

- A backend with non-human blocking I/O declares timeout config appropriate to its external
  boundary; backends without such I/O do not invent a generic timeout.
- One monotonic budget covers every non-human blocking boundary in client preparation, resolution,
  and close; implementations enforce the remaining time at each interruptible boundary.
- Native timeout exceptions translate once into the typed `timeout` outcome.
- Cleanup runs on success, failure, and timeout; cleanup failure never masks the primary outcome.

Source: [Python subprocess documentation](https://docs.python.org/3/library/subprocess.html)

### Native secret references should stay native lookup addresses

The 1Password CLI reads a field by an `op://` secret reference and supports suppressing the output
newline. This fits the source model directly: the source owns account selection, while the
per-secret mapping remains the native `op://` address. Agentworks should validate that address but
must never persist or render the resolved value.

Design consequences:

- The new 1Password source config owns its optional account selector.
- The permanent 1Password mapping model is an `op://` reference.
- The current `{account, reference}` mapping is accepted only by the release-scoped compatibility
  path and receives an exact rewrite warning.
- Resolution continues to use explicit argv and `--no-newline`.

Source: [1Password CLI `read`](https://www.1password.dev/cli/reference/commands/read)

## Refuted or not adopted

- **Controller reconciliation:** External Secrets continuously synchronizes Kubernetes resources.
  Agentworks resolves once at an operation boundary and does not persist values, so refresh loops,
  status controllers, and target-secret ownership are not applicable.
- **Dynamic-secret leases:** useful for generated credentials, but not part of a read-only KV source
  and explicitly outside this wave.
- **Process-global clients:** convenient for connection reuse, but incompatible with bounded
  authentication state, deterministic cleanup, and the existing no-cross-invocation-cache rule.
- **Thread-only timeout wrappers:** they can report timeout without stopping the underlying work.
  Each backend must enforce its deadline at an interruptible boundary instead.

## Open research questions

- A future network SDK may expose separate connect, request, and close timeouts. The first backend
  that needs those distinctions should propose an additive source-config shape based on that SDK's
  actual contract.
- Interactive terminal input has no portable forced-timeout mechanism in the current prompt
  implementation. The prompt source therefore runs only with explicit interaction permission; its
  human wait is not put behind an unsafe worker-thread timeout.

## Source quality

| Source                                     | Quality               | Angle used                                           |
| ------------------------------------------ | --------------------- | ---------------------------------------------------- |
| External Secrets Operator SecretStore docs | Primary project docs  | Provider versus configured store and readiness       |
| HashiCorp Vault concepts and API docs      | Primary vendor docs   | KV versus dynamic lifecycle and client auth lifetime |
| Python standard library docs               | Primary language docs | Subprocess timeout and child cleanup guarantees      |
| 1Password CLI docs                         | Primary vendor docs   | Native secret-reference lookup contract              |
