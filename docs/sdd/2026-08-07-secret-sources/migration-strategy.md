# Migration strategy: direct backends to declared secret sources

- Snapshot: 2026-08-08
- Code baseline: `origin/main` at `dd236c25`
- Governing design: [FRD](./frd.md) and [HLA](./hla.md)

## Executive summary

The migration replaces one overloaded concept with two explicit ones: a `secret-backend` is code,
and a `secret-source` is one configured instance of that code. The operator-visible cut is narrow.
The app publishes `env-var` and `prompt` source rows under their current names, so the default chain
and existing mappings for those names do not change. Direct configured-backend references, notably
`onepassword`, break in 0.14 and receive an exact declared-source rewrite.

Internally, the change is additive-first within the feature branch but atomic before the feature PR
merges. No merged state resolves some names as sources and others as backends. A short-lived
operation API adapter may exist between commits to translate the final typed batch into the current
all-or-nothing dictionary for unmigrated callers, but it is deleted before the same PR becomes
ready.

## Current-state inventory

The dated baseline has:

| Surface                 | Current state                                                                        |
| ----------------------- | ------------------------------------------------------------------------------------ |
| Backend implementations | Three: core `env-var`, core `prompt`, system-plugin `onepassword`                    |
| Registry payload        | Constructed backend instances; the descriptor's only `CONSTRUCTED_SINGLETON` policy  |
| Configured instances    | None; shared fields such as OnePassword `account` repeat in each secret mapping      |
| Default chain           | `("env-var", "prompt")`, interpreted as backend names                                |
| Secret mappings         | `backend_mappings` keys are backend names; values validate through one root model    |
| Graph                   | `secret -> secret-backend`; finalize receives a backend-specific instance projection |
| Runtime result          | `{name: value}` plus an optional string-error out-parameter                          |
| Runtime lifetime        | Process-global stateless backend objects; no bounded authenticated client contract   |
| CLI                     | `agw secret list` and `describe`; no `verify` command                                |
| Deprecation producers   | None; both kept settings carrier and absent manifest carrier remain unchanged        |

The migration has a broad caller surface even though the operator break is narrow: 11 files import
`agentworks.secrets.backends`, 21 files mention `active_backends`, 18 files mention
`resolve_secrets`, 27 test files contain `backend_mappings`, and 26 test files are named for
secrets, OnePassword, or doctor behavior. Those counts are migration guards, not estimates of lines
changed.

## Target shape

### Unchanged simple case

The following remains valid and keeps the same precedence and lookup rules:

```toml
[secret_config]
backends = ["env-var", "prompt"]
```

```yaml
kind: secret
metadata:
  name: github-token
spec:
  description: GitHub token
  backend_mappings:
    env-var: GITHUB_TOKEN
    prompt: false
```

If `[secret_config]` is absent, the same two names remain the default. They resolve to app-published
source rows rather than directly to backend rows. An operator declaration under either name replaces
the synthesized row under ordinary registry collision and provenance rules.

### Breaking configured-source case

The old OnePassword form combines source config and the lookup address:

```toml
[secret_config]
backends = ["onepassword", "prompt"]
```

```yaml
kind: secret
metadata:
  name: deploy-token
spec:
  description: Deployment token
  backend_mappings:
    onepassword:
      account: team
      reference: op://engineering/deploy/token
```

The 0.14 form declares the configured store once:

```yaml
kind: secret-source
metadata:
  name: team-op
spec:
  backend:
    name: onepassword
    account: team
    timeout: 30
---
kind: secret
metadata:
  name: deploy-token
spec:
  description: Deployment token
  backend_mappings:
    team-op: op://engineering/deploy/token
```

and config selects `team-op` in the chain. Multiple accounts become multiple source rows without
duplicating account selection on every secret.

## Transition mechanics

### One source-only reference model

The cutover changes both reference producers together:

- `[secret_config].backends` targets `secret-source` rows.
- `SecretDecl.dependencies` emits `secret -> secret-source` edges.
- mapping validation resolves the source, selects its backend class, and applies that backend's
  mapping model.
- runtime builds the ordered active-source chain from the same source rows.

An unknown name hard-errors. If it exactly names a backend, the hint explains that 0.14 requires a
source and renders the minimal declaration plus the specific setting or mapping rewrite. No
compatibility row is published, no warning carrier is populated, and no legacy mapping parser runs.

### Additive-first code sequence

Within the feature PR, the code moves in this order while the full suite remains green after each
commit:

1. Add the final dual backend contracts, typed source-client records, and descriptor map-host
   metadata behind existing behavior.
2. Move the capability half under `capabilities/secret_backend`, switch the code registry to
   classes, and retain public imports only where they are genuine supported exports.
3. Add the declarable source kind and built-in publisher before operator manifests.
4. Add final bounded clients and the value-free typed resolution core.
5. Atomically repoint settings, graph edges, mapping validation, active-chain construction, and
   OnePassword config to sources, routing current command callers through the typed batch's narrow
   complete-or-raise adapter.
6. Migrate every consumer and delete that adapter before the PR becomes ready.

Intermediate commits are review checkpoints on a draft feature branch, not separately mergeable
products. The completed PR exposes neither a declarable source that runtime ignores nor a settings
chain that can mean either a backend or a source.

### Consumer completion

Later commits on the same branch migrate the operation-scoped resolver and inspection surfaces to
the typed batch, add `agw secret verify`, and delete the compatibility adapter and string-error
out-parameter. Human output, future JSON output, logs, exceptions, and object representations are
tested with sentinel values to prove that resolved values cannot leak.

## Module relocation

The capability-owned code moves to `agentworks.capabilities.secret_backend`: base contract,
registry, built-in implementations, kinds, and author documentation. Secret declarations, source
declarations, resolution policy, the operation-scoped resolver, inspection, and orchestration remain
in `agentworks.secrets`.

Relocation uses `git mv` and updates imports in the same commit. Compatibility import modules are
not retained merely to reduce test churn. Package `__init__` exports remain only for names the
codebase treats as public; internal tests repoint to the truthful owner.

## Delivery and rollback

Delivery uses one normal `feat/secret-sources` branch and PR #453. The PR stays draft while phase
commits receive focused review and becomes ready only after source model, backend contracts, schema,
OnePassword migration, typed runtime, consumer adoption, operator surfaces, permanent docs, and
closeout are complete. No database or persisted secret data migrates, so rollback is a normal code
revert before the 0.14 release. After operators adopt the new manifest shape, downgrading to 0.13 is
not supported because 0.13 does not know `secret-source`.

## Risks and safeguards

| Risk                                                 | Safeguard                                                                                                                                      |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Implied sources subtly change the simple case        | Golden tests compare default chain, explicit chain, env-name derivation, prompt opt-out, precedence, and first-hit behavior before and after   |
| Source and mapping schemas select different backends | One shared source-to-backend selection helper drives validation, reference extraction, attemptability, runtime construction, and tests         |
| A plugin client receives ambient framework authority | Frozen `SecretLookupRequest` contains only name and that source's mapping; prompt metadata stays behind the caller-owned broker                |
| Timeout reports while work continues                 | Backend-owned monotonic budgets cover every non-human blocking factory, prepare, resolve, and close boundary; no thread-only timeout wrapper   |
| Values leak through new outcome surfaces             | Outcomes are value-free; `ResolutionBatch` has redacted representation and no serializer; sentinel tests cover renderers, logs, and exceptions |
| Direct OnePassword errors are vague                  | Config and manifest tests pin the exact declared-source rewrite and the 0.14 upgrade guide shows the same before/after shape                   |
| Module move leaves two authorities                   | Import sweep and graph-guard tests reject the old registry path and constructed-singleton policy before the feature PR becomes ready           |
