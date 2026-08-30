# High-Level Architecture: Runtime Git Identities

- Status: Draft for design review
- Date: 2026-08-28
- Requirements: [frd.md](./frd.md)
- Detailed design: [credential-helper-lld.md](./credential-helper-lld.md)

## Architectural Result

The git-credential domain becomes a compiler and reconciler rather than a PAT installer:

```text
declarative git-credential resources
        |
        v
provider instances validate config and declare secret references
        |
        v
core resolves the operation-wide secret union and gives each provider a scoped RunContext
        |
        v
provider static scopes + side-effect-free input validation
        |
        v
creation may proceed; later provider materialization -> StoredCredential | ManagedHelper
        |
        v
core builds one UserCredentialState
        |
        v
per-user reconciler atomically replaces Agentworks-owned state
        |
        v
Git request -> stable launcher -> generation-owned dispatcher -> stored value or managed helper
```

Providers own forge knowledge: allowed configuration, declared secret dependencies, translation to
generic HTTPS scopes, validation/exchange/acquisition, and any provider-specific helper behavior.
Core owns orchestration and state: scoped secret delivery, output-boundary checks, collision checks,
generic longest-path selection, Git registration, migration, and complete reconciliation.

## Components

### Provider acquisition models

Each provider independently owns its complete config schema. GitHub and Azure DevOps happen to use
the same required field name, `source`, but there is no shared source base or global arm set:

```text
GitHubConfig.source = GitHubSecretSource | GitHubCliSource
AzDOConfig.source   = AzDOSecretSource | AzureCliSource
```

The outer `source` and its `mode` are required. Scalar shorthand and whole-source omission are
removed; an explicitly selected secret source may still omit only its `secret` field to use the
provider's owner-derived default. Only the secret arms carry `SecretRef` in the current providers.
Structural reference extraction derives the graph from each provider's complete configuration
without requiring core to know that a source field exists or what any secret means. Another provider
may choose a different config vocabulary, declare several secret references anywhere in its model,
or have no acquisition branch at all.

The provider capability contract advances atomically from version 2 to version 3. There is no
compatibility adapter inside the capability registry: every in-tree implementation migrates in the
same change, and registration refuses an old implementation.

### Provider material

The provider operation surface separates static routing declarations from later acquisition:

```text
credential_scopes() -> HttpsCredentialScope[]
validate_inputs(scoped_context) -> no side effects
credential_material(scoped_context) -> StoredCredential | ManagedHelper
```

Core first resolves the operation-wide secret union, then constructs one `RunContext` per credential
node whose `ScopedSecrets` view contains exactly that node's provider-declared names. The provider's
static scopes and input validation are prepared together before VM or agent creation mutation. Its
later materialization operation receives the same context, owns any validation/exchange/derivation,
and returns the final payload. A stored password/token is sensitive final material, never an inert
secret reference for core to interpret. A managed helper is declarative provider output: provider
code owns its behavior while core owns installation, registration, replacement, and removal.
Operator configuration cannot supply commands or executable bodies. A provider's declared inputs do
not constrain which payload variant it may return. Core treats both variants as sensitive provider
output and does not infer, scan, or attest their provenance.

Scopes contain only Git's HTTPS credential context. GitHub translates `repos` and `owner` to path
prefixes; Azure DevOps translates `org` to a path prefix. Core does not know those source concepts.
Selection maps a request directly to provider material, which removes the current accidental
coupling between routing and static-store usernames.

### User credential state builder

Core builds one complete `UserCredentialState` for a target user:

- validated generic scopes and provider-returned payloads;
- private per-credential Git-protocol records, containing only `StoredCredential` values without URL
  serialization;
- a generation-owned dispatcher and optional private `stored/` directory;
- an immutable file set of provider-owned managed-helper implementations embedded in the generation;
- an Agentworks-owned Git include;
- legacy cleanup targets.

The builder rejects every duplicate exact claim, including host defaults, orders longest path
prefixes before shorter prefixes and host defaults, and emits no generation files when the desired
material set is empty. The reconciler owns the fixed stable launcher and lock.

### Per-user reconciler

Admin and agent initialization call the same reconciler unconditionally. The caller supplies the
target user's transport/home and the complete material already returned by providers. It never
receives a provider secret map.

The reconciler:

1. builds the entire state before target mutation;
2. takes a bounded exclusive lock;
3. cleans an abandoned stage, disables legacy credential material/registration, stages a private
   generation, and compares it to active state;
4. installs or repairs the stable launcher, atomically replaces the current generation when changed,
   and ensures the exact include once for nonempty state, or removes the launcher/include for empty
   state;
5. deletes inactive generations and releases the lock.

Each helper invocation enters through the stable launcher, takes a bounded shared parent-directory
handoff lock and then a bounded shared lock on the current implementation's fixed inherited
descriptor, and executes the dispatcher from the resolved immutable generation. For stored material
the dispatcher reads the selected record under that lock. For a managed helper it opens the
generation-owned helper, then releases the shared lock and invokes the already-open file through its
descriptor. Reconciliation may unlink the inactive generation, but the in-flight request retains the
selected helper inode; provider CLI descendants cannot retain the lock. Replacement and garbage
collection therefore cannot produce a mixed-generation read. The include owns the helper
registration and `useHttpPath` behavior for only the hosts represented in the desired state. For
each managed host it resets inherited helper values before registering the Agentworks launcher;
unrelated hosts retain operator-managed helpers. Agentworks does not delete or rewrite any
indistinguishable operator `credential.helper` value.

This breaking cut introduces the managed root, stable launcher, and dispatcher together; no prior
valid Agentworks launcher exists to pair with the new dispatcher. The launcher is repaired from the
same implementation bytes during reconciliation, while `current` alone selects an immutable
generation atomically. The fixed descriptors are current lock-and-material handoff mechanisms, not
public or permanently versioned ABIs; a future incompatible change designs its migration when it
exists.

### Stable launcher and runtime dispatcher

Git invokes the stable Agentworks launcher through the standard credential-helper protocol. The
launcher acquires the shared lock and executes the dispatcher from the resolved generation. That
dispatcher parses the bounded request, normalizes protocol/host/path, chooses the longest
segment-aware path prefix on the exact host, and handles the selected stored credential or managed
helper.

The dispatcher is a read-only generator:

- **Stored credential** reads the selected provider-returned username/password from mode-0600
  managed storage and returns it without knowing how the provider acquired it.
- **Managed helper** invokes the installed provider-owned helper program through the bounded common
  execution envelope and relays only a valid Git credential protocol response. The GitHub and Azure
  provider programs own their command lookup and invocation, response construction, and fixed safe
  failure guidance.

The helper ignores unsupported `store`/`erase` operations except for the existing safe rejection
diagnosis. It does not cache, log, or write the runtime token.

### One write path

`vm add-git-credential` is removed. It is a second desired-state path that cannot safely preserve
the complete installed stored-credential set from the workstation without reading target secrets or
adding target-side merge metadata. Operators declare credentials on admin or agent templates and run
reinit; those two callers share the one complete reconciler.

## Lifecycle

### Registry and graph

Provider configuration remains validated by the capability framework. The graph derives every
provider-declared secret edge from its configuration. The current CLI-backed credential nodes have
no secret refs and can pass preflight without a secret source.

### Boundary resolution

Composition roots resolve the plan's complete secret union once and deliver each provider a
`RunContext` backed by `ScopedSecrets(node.secret_refs())`. Core neither builds a token mapping nor
assumes what the values mean. A provider cannot read an undeclared secret, and a provider with no
declared secrets receives an empty scoped view. The context deliberately contains no admin or agent
transport, so the materialization operation has no target-mutation power.

### Runup

Before any creation mutation, core validates all static scopes together and asks each provider to
validate its resolved inputs without side effects. Current secret arms enforce line safety there;
current CLI arms have no static input to validate.

When `defaults.runup_git_credentials` is enabled, the git-credential node later calls provider runup
with the same scoped context later used for materialization. Each provider decides whether its
configured arm has optional validation work. The current provider-specific HTTP probes and caller
policies remain:

- multi-credential initialization skips a definitively rejected static token and records partial;
- network indeterminacy warns and continues;
- current CLI-backed arms do no runup work.

### Materialization

Core calls each surviving provider's materialization operation with its scoped context. The provider
returns a final payload; core then validates line/control safety for stored protocol fields and the
bounded managed-helper shape. Static scopes and their collisions were already validated before
creation. Core does not correlate declared inputs with payload shape. It renders the deterministic
state and reconciles even if the surviving set is empty. Core never performs authentication-specific
mapping or exchange.

### Git operation

The runtime helper sees the target user's environment. Missing command, CLI command failure,
timeout, empty output, or malformed output produces a safe runtime-helper failure; no provisioning
status is retroactively changed. A valid token for an unauthorized identity is rejected by the forge
through Git's normal authentication flow. Reinitialization repairs configuration, not third-party
identity state.

## Git Configuration Boundary

One exact global include reference points to the Agentworks-owned include. The include contains a
credential context per managed HTTPS host. Each context:

- enables `useHttpPath`;
- resets helpers inherited for that host;
- registers the Agentworks launcher.

This gives Agentworks deterministic routing where the operator explicitly configured Agentworks
credentials while leaving other hosts and the operator's source config intact. Removing the last
credential removes the include, so prior operator behavior resumes automatically.

The implementation must prove actual Git config precedence with Debian Bookworm's Git 2.39, not only
inspect generated text.

## Runtime Identity Details

### GitHub CLI

The provider returns a managed helper containing the fixed `github.com` behavior and disables
prompting. GitHub CLI owns active account selection and its ordinary token behavior; Agentworks
neither authenticates it nor parses its configuration.

### Azure CLI

The provider returns a managed helper containing the fixed Azure DevOps resource recipe. It
constructs organization-as-username and Entra-token-as-password itself, following current Git
Credential Manager prior art and pending the required operator live proof before merge. Core does
not know that mapping. Final acceptance proves the generated helper through clone, fetch, and a
reversible write. The exact command and prior-art evidence live in the LLD and research artifact.

## Security Boundaries

- Provider configuration can choose only closed acquisition arms; it cannot inject commands.
- Current CLI runtime helpers execute no login and require no Agentworks secret.
- Provider-owned side-effect-free input validation follows scoped secret resolution and precedes VM
  or agent creation mutation; optional authenticated runup remains later under its existing policy.
- Provider materialization can read only declared secrets; core never interprets their values.
- Stored credentials and managed-helper bodies are sensitive provider output and are never logged or
  represented verbatim.
- Current CLI token stdout is captured separately from diagnostics, validated as one line, and
  emitted only through the Git protocol.
- Upstream stderr is summarized, not copied blindly.
- The managed directory and staged replacement are private to the target user.
- A shared launcher/exclusive reconciler parent handoff preserves one lock identity; the stable
  shared/exclusive `flock` prevents cross-generation reads and cleanup races.
- Reconciliation deletes only exact Agentworks-owned paths and exact Git config values.
- Integration fixtures use disposable identities/repos and clean up reversible writes.

## Failure Semantics

| Failure                                    | Stage        | Result                                                      |
| ------------------------------------------ | ------------ | ----------------------------------------------------------- |
| Invalid provider config or scope collision | config/build | hard configuration error before target mutation             |
| Missing declared secret                    | resolution   | existing secret-resolution failure                          |
| Provider rejects or cannot materialize     | runup/op     | existing skip/partial or initialization failure semantics   |
| Static probe network indeterminate         | runup        | warning; helper is installed                                |
| required command absent/helper fails       | Git runtime  | nonzero helper with fixed value-safe diagnostic             |
| Shared/exclusive lock contention           | runtime/init | bounded failure with fixed value-safe retry guidance        |
| CLI token empty, malformed, or timed out   | Git runtime  | nonzero helper with fixed value-safe diagnostic             |
| Valid token lacks repository permission    | forge/Git    | normal Git rejection plus fixed selected-credential context |
| Reconciliation transport/write failure     | init         | existing nonfatal initialization warning/partial semantics  |

## Compatibility and Migration

The configuration change is intentionally breaking. Both built-in providers replace `token` with a
required structured `source`; whole-source omission and scalar shorthand are removed. The explicit
secret source keeps the owner-derived default when only its inner `secret` field is omitted. There
is no dual reader or compatibility alias. The provider capability contract changes atomically
because the internal operation surface changes.

Existing VMs migrate on their next admin or agent initialization after their manifests are updated.
The changelog/release notes and upgrade guide prominently label the config break, give exact
`token`-to-`source` rewrites, and direct operators to declarative configuration and reinit.
Reconciliation removes the legacy direct `credential.helper` value, old include, and old helper
script. It removes `~/.git-credentials` only when the exact legacy Agentworks helper registration is
present before cleanup; otherwise that path is operator-owned and untouched. An empty declared list
also runs this cleanup.

No background fleet mutation, database migration, or implicit CLI authentication occurs.

## Permanent Homes

Before closeout, load-bearing behavior moves to:

- `cli/agentworks/capabilities/git_credential/README.md` for provider authors;
- `cli/agentworks/capabilities/README.md` for the capability-system summary of the same ownership
  and materialization boundary;
- the closest core Git credential README/module documentation for reconciliation ownership;
- resource schema, sample manifests/config, command reference, guide concepts, and upgrade guide;
- integration-testing evidence for GitHub and Azure runtime identities.

No production artifact references this SDD.
