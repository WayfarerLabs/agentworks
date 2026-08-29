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
provider materialization -> StoredCredential | ManagedHelper
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

`TokenAcquiringConfig.token` remains a discriminated union, but the union is no longer one shared
global set of arms. Each provider's concrete config closes the modes it supports:

```text
GitHubConfig.token = SecretToken | GitHubCliToken
AzDOConfig.token   = SecretToken | AzureCliToken
```

The existing omission and scalar-shorthand behavior belongs to `SecretToken` and remains stable.
Only that arm carries `SecretRef` in the current providers. Structural reference extraction derives
the graph from each provider configuration without requiring core to know what any secret means. A
future provider may declare several secret references and derive one credential from them without a
contract change.

The provider capability contract advances atomically from version 2 to version 3. There is no
compatibility adapter inside the capability registry: every in-tree implementation migrates in the
same change, and registration refuses an old implementation.

### Provider material

The provider operation surface becomes one context-taking materialization operation rather than the
current `helper_entry` plus `credential_lines` pair:

```text
CredentialMaterial
  scopes: HttpsCredentialScope[]
    protocol = https
    host
    path_prefix_segments[]
  payload:
    StoredCredential
      username
      password/token
    ManagedHelper
      bounded provider-owned helper program
      fixed value-safe failure hint
```

Core first resolves the operation-wide secret union, then constructs one `RunContext` per credential
node whose `ScopedSecrets` view contains exactly that node's provider-declared names. The provider's
materialization operation receives that context, owns any validation/exchange/derivation, and
returns the final shape. A stored password/token is sensitive final material, never an inert secret
reference for core to interpret. A managed helper is declarative provider output: provider code owns
its behavior while core owns installation, registration, replacement, and removal. Operator
configuration cannot supply commands or executable bodies. Core accepts a managed helper only from a
provider configuration whose structurally derived secret-reference set is empty; every
secret-bearing provider returns a stored credential.

Scopes contain only Git's HTTPS credential context. GitHub translates `repos` and `owner` to path
prefixes; Azure DevOps translates `org` to a path prefix. Core does not know those source concepts.
Selection maps a request directly to provider material, which removes the current accidental
coupling between routing and static-store usernames.

### User credential state builder

Core builds one complete `UserCredentialState` for a target user:

- validated generic scopes and provider-returned payloads;
- the private credential store, containing only `StoredCredential` values;
- a generation-owned dispatcher and optional static store;
- provider-owned managed-helper implementations embedded in the generation;
- an Agentworks-owned Git include;
- legacy cleanup targets.

The builder rejects duplicate nonempty path claims, retains released first-declared behavior for
multiple host defaults, orders longest path prefixes before shorter prefixes and host defaults, and
emits no generation files when the desired material set is empty. The reconciler owns the fixed
stable launcher and lock.

### Per-user reconciler

Admin and agent initialization call the same reconciler unconditionally. The caller supplies the
target user's transport/home and the complete material already returned by providers. It never
receives a provider secret map.

The reconciler:

1. builds the entire state before target mutation;
2. takes a bounded exclusive lock;
3. cleans an abandoned stage, disables legacy credential material/registration, stages a private
   generation, and compares it to active state;
4. atomically replaces the stable launcher/current generation when changed and ensures the exact
   include once for nonempty state, or removes the launcher/include for empty state;
5. deletes inactive generations and releases the lock.

Each helper invocation enters through the stable launcher, takes a bounded shared lock on the
current implementation's fixed inherited descriptor, and executes the dispatcher from the resolved
immutable generation. The dispatcher loads everything it needs for one selected stored credential or
managed helper before releasing that descriptor; managed-helper descendants cannot retain it.
Replacement and garbage collection therefore cannot produce a mixed-generation read. The include
owns the helper registration and `useHttpPath` behavior for only the hosts represented in the
desired state. For each managed host it resets inherited helper values before registering the
Agentworks launcher; unrelated hosts retain operator-managed helpers. Agentworks does not delete or
rewrite any indistinguishable operator `credential.helper` value.

The stable launcher and dispatcher replacement window exists in this implementation because their
two atomic replacements cannot be one filesystem transaction. Both adjacent pairings MUST therefore
work for this migration and be tested with a fault between replacements. The fixed descriptor is a
current lock-handoff mechanism, not a public or permanently versioned ABI; a future incompatible
change designs its migration when it exists.

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
assumes what the values mean. A provider cannot read an undeclared secret, and a zero-secret
provider receives an empty scoped view. The context deliberately contains no admin or agent
transport, so the materialization operation has no target-mutation power.

### Runup

When `defaults.runup_git_credentials` is enabled, the git-credential node calls provider runup with
the same scoped context later used for materialization. Each provider decides whether its configured
arm has optional validation work. The current provider-specific HTTP probes and caller policies
remain:

- multi-credential initialization skips a definitively rejected static token and records partial;
- network indeterminacy warns and continues;
- current CLI-backed arms do no runup work.

### Materialization

Core calls each surviving provider's materialization operation with its scoped context. The provider
returns final `CredentialMaterial`; core then validates generic scopes, line/control safety for
stored protocol fields, the secret-reference/payload relationship, bounded managed-helper shape, and
collisions. It renders the deterministic state and reconciles even if the surviving set is empty.
Core never performs authentication-specific mapping or exchange.

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
Credential Manager prior art and pending the required design-ready Azure Repos wire proof. Core does
not know that mapping. The implementation later proves the generated helper through clone, fetch,
and a reversible write. The exact command and prior-art evidence live in the LLD and research
artifact.

## Security Boundaries

- Provider configuration can choose only closed acquisition arms; it cannot inject commands.
- Current runtime helpers execute no login and receive no Agentworks secret.
- Provider-owned static validation remains after scoped secret resolution and before target writes.
- Provider materialization can read only declared secrets; core never interprets their values.
- Stored credentials and managed-helper bodies are sensitive provider output and are never logged or
  represented verbatim.
- Runtime token stdout is captured separately from diagnostics, validated as one line, and emitted
  only through the Git protocol.
- Upstream stderr is summarized, not copied blindly.
- The managed directory and staged replacement are private to the target user.
- A stable shared/exclusive `flock` prevents cross-generation reads and cleanup races.
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

The configuration change is additive: all released omitted, scalar, and explicit secret spellings
remain valid. The provider capability contract changes atomically because the internal operation
surface changes.

Existing VMs migrate on their next admin or agent initialization. The upgrade guide and
command-removal release note direct operators to declarative configuration and reinit.
Reconciliation removes the legacy direct `credential.helper` value, old include, old helper script,
and Agentworks-owned `~/.git-credentials`, then installs the new layout. An empty declared list also
runs this cleanup.

No background fleet mutation, database migration, or implicit CLI authentication occurs.

## Permanent Homes

Before closeout, load-bearing behavior moves to:

- `cli/agentworks/capabilities/git_credential/README.md` for provider authors;
- the closest core Git credential README/module documentation for reconciliation ownership;
- resource schema, sample manifests/config, command reference, guide concepts, and upgrade guide;
- integration-testing evidence for GitHub and Azure runtime identities.

No production artifact references this SDD.
