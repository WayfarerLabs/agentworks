# HLA: Secret Sources

- Status: Implemented; final operator-gated validation pending
- FRD: `docs/sdd/2026-08-07-secret-sources/frd.md`
- Prior art: `docs/sdd/2026-08-07-secret-sources/prior-art-research.md`
- Saga contract: `docs/sdd/2026-08-04-next-steps/capability-descriptor-contract.md`

## Architectural summary

Secrets adopt the same two-level shape as VM platforms and sites:

1. `secret-backend` is a code capability registered by class.
2. `secret-source` is a declarable configured instance of one backend.
3. A secret's `backend_mappings` keys name sources, retaining the existing field and the existing
   `env-var` and `prompt` keys for the simple case.
4. The active setting keeps its `[secret_config].backends` spelling for compatibility, but its
   values now name sources.
5. Resolution opens one bounded client per attempted source, returns a typed outcome for every
   requested secret, and discards clients and values at the operation boundary.

`env-var` and `prompt` are true, app-published `secret-source` rows synthesized from their backend
classes before operator manifests publish. An operator declaration under either name replaces the
synthesized row by the kind's explicit override policy. Describe and doctor infer and show that
substitution from the reserved source name plus operator provenance.

The capability half moves from `agentworks.secrets` to `agentworks.capabilities.secret_backend`. The
declarable rows, resolution policy, operation-scoped resolver, and inspection remain in
`agentworks.secrets`, parallel to the existing split between `capabilities.git_credential` and
`git_credentials`. This fulfills the capability relocation without making the capabilities package
own a consuming domain.

## Component topology

```text
backend implementation classes
  config_model + mapping_model
              |
              v
   secret-backend descriptor
      |                 |
      |                 +--> map-host schema projection
      v
secret-backend rows
      |
      v
secret-source declarations ----> source readiness
      ^                                  |
      |                                  v
[secret_config].backends ------> active source chain
                                         |
secret.backend_mappings -----------------+
                                         |
                                         v
                              bounded source clients
                                         |
                                         v
                              per-secret typed outcomes
                                  |              |
                                  v              v
                         operation errors   secret verify
```

Source resolution completes before the operation-scoped `Resolver.resolve()` returns. Every other
capability's runup remains downstream of that boundary, so secret-source clients never depend on a
later capability stage.

## Resource and capability model

### `secret-backend`

`SecretBackend` becomes an abstract `Capability` subclass under
`capabilities/secret_backend/base.py`. Its registry stores classes under the descriptor's existing
`CLASS_BY_NAME` policy. The constructed-singleton enum member and every special graph or adapter
branch that served it are removed.

Each implementation declares:

- `name`, `description`, `contract_version`, and optional topic prose through the shared capability
  contract;
- `config_model`, the tagged per-source config model;
- `mapping_model`, the per-secret lookup-address model;
- a cheap config-independent backend-readiness verdict for the capability row;
- pure mapping preview and attemptability operations;
- a bounded client factory for runtime resolution.

The built-ins declare tag-only source config models. The 1Password config model adds an optional
account selector and an external-operation timeout. Its permanent mapping model is the native
`op://` reference.

`SecretBackend` inherits the shared capability identity and config-binding machinery, but ordinary
capability `preflight` and `runup` are deliberately final no-ops for this kind. They run too late:
source resolution is the earlier stage that supplies values to every other capability. The source
orchestrator instead drives the distinct `SecretSourceClient.prepare` and `resolve` protocol under
the bounded client lifetime described below. Registration conformance rejects an implementation that
replaces either ordinary lifecycle method; the invariant is runtime-enforced rather than only an
`@final` type-checking hint.

### `secret-source`

`SecretSourceDecl` is a `DeclaredResource` with:

- `name` and ordinary declaration metadata;
- `backend: CapabilityBlock`, a tagged block selecting exactly one backend and carrying that
  backend's per-source config.

The source emits one `secret-source -> secret-backend` edge and every resource reference declared by
its backend config. Its validation uses the shared capability-config bridge, including file-and-line
framing. Its folded readiness:

1. propagates a disabled or config-independent not-ready backend verdict;
2. calls the backend's total, non-constructing classmethod over source config;
3. leaves authentication, network reachability, and interactive unlocks for the bounded client's
   operation-time `prepare` call.

This chooses the consuming-resource readiness hook from the FRD fork, matching `vm-site`. The
descriptor's tagged-host record names `secret-source`; the consuming resource's readiness hook is
derived from that record, so no second readiness-shape field or secret-only readiness switch is
added.

### Synthesized sources and provenance

`secret-source` uses `miss_policy="error"`, so an arbitrary miss is a typo, not an implicit source,
and sets `builtin_override="allow"`. A small domain publisher derives `env-var` and `prompt` source
rows from their registered zero-config backend classes before operator manifests publish:

```yaml
kind: secret-source
metadata:
  name: env-var
spec:
  backend: { name: env-var }
```

The rows use normal built-in origins, so existing registry collision handling lets an operator row
replace either one. A source inspector classifies an operator-origin row under a default source name
as `operator override of synthesized default`; doctor and describe render that fact. No shadow row,
second source registry, or change to the one-name `ResourceKind.synthesize` contract is needed.

## Two declared schema surfaces per backend

The current descriptor assumes one implementation model because every existing capability had one
config surface. This wave has two real, non-interchangeable consumers:

- source config, selected by the source's tagged `backend` block;
- lookup-address config, selected after a mapping key resolves to a source and then its backend.

Overloading one `config_model` would make one schema mean two shapes. Copying a mapping model onto
each source row would create a second authority and make static discovery depend on live operator
instances. The contract is therefore extended explicitly:

- `config_schema` remains the implementation's primary config contract and becomes tagged,
  mapping-shaped source config hosted by `secret-source.backend`;
- `mapping_schema` is an additive descriptor contract for `mapping_model`;
- `mapping_host` records the declarable host kind (`secret`), map field (`backend_mappings`), key
  target kind (`secret-source`), and framework-owned `false` opt-out.

Registration conformance checks both model declarations without calling implementation code. It also
rejects a `SecretRef` marker anywhere in the source `config_model`. This is expressed as a general
forbidden-reference-kinds property on `ConfigContract`, set to `{"secret"}` for `secret-backend`,
rather than as a kind-name conditional.

The mapping contract also declares a JSON-native input domain. The common conformance walker rejects
Python-only wire annotations (for example dates, bytes, sets, tuples, enums, custom classes, or
non-string mapping keys), including nested occurrences, while allowing JSON primitives, string-keyed
objects, arrays, literals, `Any`/`object`, and model composition. Validators may still reject any or
every value (the prompt mapping intentionally does), so this guarantees the wire type vocabulary,
not that a model is satisfiable.

Runtime validation is exact: resolve the mapping key to a `secret-source`, read that source's
backend class, and validate the value against that class's `mapping_model`. Extraction follows the
same selected model and remains total.

Static JSON Schema cannot infer an arbitrary source declaration in another YAML document. The
map-host projection therefore emits `propertyNames` carrying a `secret-source` reference marker and
uses the union of all registered backend mapping models plus `false` for every value, including the
overridable `env-var` and `prompt` names.

That union gives editors the onepassword `op://` validation and closed nested-key checking required
by R8 without claiming a cross-document relationship JSON Schema cannot enforce. Runtime validation
narrows to the selected source's one backend, so the emitted union is intentionally
under-constrained but never rejects a valid document. Describe-kind explains this static/runtime
split.

## Secret mappings and graph edges

`SecretDecl.backend_mappings` keeps its public spelling. Its raw carrier broadens to every
JSON-compatible value so JSON-native addresses reach the selected model unchanged for
backend-specific runtime narrowing; `false` remains the framework-owned opt-out. Its keys now name
`secret-source` rows. The permanent graph edges are `secret -> secret-source`; the special
`FinalizeContext.available_backends` list disappears.

A secret's dependency pass emits:

- every explicit non-`false` mapping key as a source edge;
- every present source whose backend says it has a default mapping for that secret, excluding an
  explicit `false` opt-out.

Source-name validation is separate from candidate-edge emission: every explicit key is checked,
including a `false` opt-out. Thus `onepassword: false` still receives the 0.14 direct-backend
migration diagnostic when no source named `onepassword` exists, while a known source's `false` entry
suppresses its candidate edge and runtime attempt exactly as today.

The descriptor-derived key checker collects references for each map-host row as that row enters the
Registry build or fixed-point walk, then resolves them in the corresponding existing resolve stage.
This keeps build total and preserves cycle/error precedence while ensuring late-materialized host
rows cannot bypass validation. One target schedule interleaves validation-only and ordinary targets
in first encounter order while their reference maps remain separate, so existing ordinary misses
retain their relative precedence. These are validation-only references rather than graph edges:
descriptor conformance therefore permits map hosts only for `USES` markers targeting an error-policy
kind. They neither auto-declare a target nor duplicate the candidate edges above.

Activity remains settings-owned and is filtered only by chain validation and runtime resolution; the
Registry stays config-agnostic. `FinalizeContext.available_backends` is replaced by a generic,
read-only capability-class projection derived from descriptor rows. The secret walk iterates
`context.rows_of("secret-source")`, reads each source's selected backend class through that
projection, and calls only its pure attemptability operation. This is the builder-owned registry
read already permitted at HEAD, generalized from one backend-specific tuple rather than moved into
the resource. No client is constructed and no secret is resolved during graph construction,
validation, schema generation, describe, or guide rendering.

## Breaking migration

### Reference resolution

Every configured name resolves only as a source:

1. A declared or synthesized source resolves normally.
2. Otherwise the name is an unknown source and hard-errors with the available source names.
3. If the unknown name exactly matches a backend, the error identifies the direct-backend migration
   and gives the source declaration plus settings or mapping rewrite.

This makes `env-var` and `prompt` native source references from day one. It also prevents an
operator-declared source named like a backend from being bypassed by a second lookup branch.

No compatibility source, warning carrier, or legacy normalization branch is added. Existing generic
config deprecation machinery remains available to other efforts but gains no Secret Sources
producer.

### OnePassword rewrite

The old direct form:

```toml
[secret_config]
backends = ["onepassword", "prompt"]
```

```yaml
backend_mappings:
  onepassword:
    account: team
    reference: op://vault/item/field
```

becomes a declared source, with the shared account selector on that source:

```yaml
kind: secret-source
metadata:
  name: team-op
spec:
  backend:
    name: onepassword
    account: team
---
kind: secret
metadata:
  name: example
spec:
  backend_mappings:
    team-op: op://vault/item/field
```

and `[secret_config].backends` names `team-op`. The old `{account, reference}` value receives the
same hard error and exact rewrite rather than a release-scoped parser. New reference and sample
surfaces teach only the source form.

## Bounded client lifecycle

The active chain is an ordered tuple of `ActiveSource` records derived from
`[secret_config].backends`. Each record holds the source declaration, folded readiness, backend
class, and validated config. It holds no client.

Before a client opens, the source orchestrator builds immutable `SecretLookupRequest` records. A
request contains only the secret name and that source's validated mapping. It contains no
`SecretDecl`, description, hint, other-source mappings, origin, references, Registry, graph, config
object, resolver, or resolved value. The client API accepts only this projection, which enforces the
FRD's anchored-projection boundary by construction.

Prompt text is a caller concern rather than broader client authority. When interaction is allowed,
the orchestrator may furnish the built-in prompt client with a narrow `InteractionBroker`. The
client asks the broker for a value by secret name; the broker alone holds the corresponding
description and hint and renders the prompt. Other backends and clients never receive that metadata,
and no broker exists when interaction is refused.

For each source in chain order, the resolution service:

1. skips folded-not-ready sources with a typed per-secret diagnostic;
2. computes the still-missing secrets the backend would attempt;
3. starts one monotonic external-operation budget when the backend's source config declares one;
4. constructs and enters one source-bound client only when the attempt set is non-empty, passing the
   remaining budget to any non-human blocking factory or context-entry boundary;
5. runs authenticated, read-only `client.prepare` and resolves the batch, passing the remaining
   budget to every non-human blocking boundary;
6. exits the client context under the same remaining budget, guaranteeing local release;
7. advances only after that client context has closed.

Setup is amortized across the batch, but lifetime never exceeds that source's turn in one operation.
Cleanup runs on success, failure, timeout, and interruption. Cleanup failure warns with source
identity and never replaces the primary outcome. No unused later source is constructed.

Timeout config belongs to the backend whose external boundary can enforce it, not to every source.
The 1Password source config declares a positive external-operation timeout, and its subprocess uses
`subprocess.run(timeout=...)`, which kills and waits before raising. Env-var needs no timeout.
Prompt is invoked only when interaction is explicitly allowed; operator input is outside unattended
external-operation timeout semantics and is not wrapped in a worker thread that could outlive a
reported timeout. Every future backend with non-human blocking I/O must declare and enforce a
bounded timeout in its own source config. The budget covers non-human blocking work in `prepare`,
`resolve`, and `close`. Once exhausted, close may cancel and release local state but may not begin
unbounded remote cleanup.

## Interaction policy

Resolution receives an explicit immutable policy rather than reading TTY state inside a backend:

- `interaction=allow` permits sources declared as possibly interactive;
- `interaction=refuse` excludes them before client construction and records `refused-interaction`
  when they were the remaining candidate;
- global `--non-interactive` always selects `refuse`;
- `agw secret verify` defaults to `refuse` and requires an explicit `--allow-interaction` to permit
  prompt, biometric, or reauthentication paths.

Ordinary operations preserve today's interaction default: the operation boundary derives `allow`
only when stdin is a TTY and global `--non-interactive` is absent; otherwise it selects `refuse`.
Before every allowed interactive source turn, resolution applies the fail-before-interaction doom
check: if any still-missing secret has no remaining ready source that would attempt it, the
operation fails without starting Prompt, 1Password, or an interactive plugin for a different secret
that cannot make the whole operation succeed.

The caller-owned `InteractionBroker` is the only interface permitted to render a prompt. A backend
cannot infer interactivity from TTY state or gain ambient access to prompt metadata.

Inspection remains side-effect-free. Describe uses pure attemptability and identifier previews and
does not open clients. Doctor reports folded readiness, source provenance, and non-probing
resolvability. Verification is the explicit surface for an actual read.

## Typed resolution outcomes

The resolution core returns one frozen, value-free `ResolutionOutcome` per requested secret.
`category` is a stable enum:

- `resolved`: carries source and safe identifier only;
- `unavailable`: every eligible source soft-missed or was not ready; a disabled backend system
  plugin is represented by a structured plugin identity and fixed enable-plugin remediation, never
  by copying a free-form readiness reason;
- `refused-interaction`: resolution required a source excluded by policy;
- `timeout`: an attempted source exceeded its backend-enforced external deadline;
- `resolution-failure`: hard mapping, authentication, transport, malformed-value, or unexpected
  source failure, with a typed detail code and safe remediation.

Resolved values live in a separate private mapping on `ResolutionBatch`, never on an outcome. The
batch has a redacted representation, no generic serialization method, and exposes the value mapping
only through the complete-or-raise operation-boundary method. Renderers accept outcomes rather than
the batch. Tests assert that `repr`, human rendering, future JSON conversion, logs, and raised
errors cannot contain sentinel resolved values. This removes the current `errors` out-parameter and
boolean-shaped preview answer.

Hard mapping failures stop that secret from falling through. Soft misses continue. A batch-level
client failure is attributed to every secret attempted in that batch. First resolved source wins.
Control-character validation happens before an outcome becomes `resolved`.

The operation-scoped `Resolver` consumes the complete-or-raise adapter and continues to cache only
for that operation. `agw secret verify NAME...` renders outcome categories and source identities,
never values, and exits nonzero when any requested secret is not resolved. Human and future JSON
renderers consume the same result records.

## Discovery, samples, completions, and docs

Adding `secret-source` to `KIND_REGISTRY` automatically supplies kind discovery, manifest decode,
schema-set membership, samples, describe-kind, and kind-name completion. The backend descriptor's
tagged host projects backend-specific source config into the source model. The new map-host
projection enriches secret mappings.

The implementation updates in the same phases that make each claim true:

- `cli/agentworks/sample-config.toml` and its tests;
- the secrets and capability READMEs, with the backend author contract promoted under
  `capabilities/secret_backend/README.md`;
- `docs/guides/resources.md` and the 0.14 upgrade guide, including the exact onepassword rewrite;
- ADR 0016's graduated-instance wording and ADR 0023's descriptor field inventory;
- the secrets CLI README, command reference, and Bash, Zsh, and PowerShell completion snapshots for
  the new `agw secret verify` command;
- guide topic prose through the universal topic contract once that contract is present at HEAD.

The guide-topic contribution is the only conditional item. If the universal contract has not landed
by this effort's closeout, Secret Sources does not invent a temporary adapter: it records the
deferral against
`docs/sdd/2026-08-05-onboarding-and-discovery/plan.md#phase-4-wave-2-adoption-and-registry-inventory`
and still completes every README, command reference, sample, schema, and shell-completion update it
owns.

No permanent artifact points readers back to this SDD.

## Security and invariant enforcement

- Source config may not contain `SecretRef` markers, enforced during backend registration.
- Architectural owners of values are limited to the private `ResolutionBatch` mapping,
  operation-scoped consumers, and the centralized process-input boundary. Values never enter
  outcomes, resource rows, graph nodes, config models, logs, doctor records, describe records, argv,
  or persisted/provider-retained state.
- Provider-shaped tests enforce that boundary at five final-inspection surfaces: Lima instance YAML,
  WSL2 and Proxmox bootstrap staging, Azure `OSProfile.custom_data`, and AWS
  `RunInstances.UserData`. Lima, Azure, and AWS retain credential-free bootstrap payloads and
  deliver the key after boot through a fixed command on provisioning-transport stdin. WSL2 and
  Proxmox use private temporary staging with one verified removal attempt.
- The workstation process is inside the trust boundary. Process memory and ordinary Python traceback
  locals are outside the security guarantee; a workstation-user compromise already grants process
  access and passwordless administrative access to managed VMs.
- Backend registries and graph nodes carry classes, never authenticated clients.
- Source clients receive only immutable `SecretLookupRequest` projections, never descriptions,
  hints, declarations, registries, graphs, config roots, resolvers, or mappings for other sources;
  the caller-owned interaction broker keeps prompt metadata out of clients.
- Interaction is caller policy, not a backend guess.
- Backends with non-human blocking I/O declare and enforce deadlines at their interruptible external
  boundaries; every client has deterministic cleanup.
- Inspection and schema surfaces invoke no backend or client code.
- Mapping validation and reference extraction select the same backend model through the source.
- Reference resolution has one source-only branch; migration errors never create runtime rows.

## Rejected alternatives

- **Keep direct backend references forever:** creates two permanent lookup branches and violates the
  settled one-concept model.
- **Represent synthesized sources as a projection:** forces bespoke branches into discovery, doctor,
  schema, and resolution and cannot use normal provenance.
- **Keep constructed backend singletons:** gives future authenticated clients process lifetime and
  preserves the descriptor's only registry exception.
- **Put mapping models on source rows:** duplicates one backend-authored contract per instance and
  makes static references depend on operator state.
- **Use one model for source config and lookup mappings:** the shapes have different owners and
  consumers; overloading would make validation and documentation lie.
- **Wrap blocking clients in timeout threads:** a timed-out task can continue running and retain
  secrets or child processes after the operation reports completion.
- **Move the whole secrets domain under capabilities:** reverses the established provider versus
  consuming-domain layering used by VM and git-credential capabilities.

## Requirement coverage

| Requirement | Architectural commitment                                                                     |
| ----------- | -------------------------------------------------------------------------------------------- |
| R1          | Declarable `secret-source` with one tagged backend block and validated per-source config     |
| R2          | True app-published env-var/prompt rows, override policy, and rendered provenance             |
| R3          | Immediate source-only resolution with exact settings and manifest migration errors           |
| R4          | Typed outcomes, explicit interaction policy, external deadlines, and bounded clients         |
| R5          | Class registry, lazy source-bound construction, and removal of singleton branches            |
| R6          | Registration-time forbidden secret-reference markers and ordered active-source orchestration |
| R7          | Backend capability package relocated under `capabilities/secret_backend`                     |
| R8          | Descriptor `mapping_schema` and `mapping_host`, consumed by shared spec projection           |
| R9          | Derived discovery surfaces, sample/docs/completions updates, and exact remediation           |

## Decisions for pre-implementation review

1. Backend construction is lazy per attempted source per operation; the bounded client closes after
   that source's batch.
2. Source readiness is the consuming-resource hook over dependency state and backend source config.
3. The backend continues to author the per-secret `mapping_model`; the source selects which model
   applies but does not copy it.
4. Synthesized sources are true registry rows.
5. The descriptor gains explicit primary-config and mapping-config contracts.
6. Direct backend references break in 0.14 with precise errors and guide content; no compatibility
   normalizer is built.
