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
provider instances validate config and emit HelperDefinition values
        |
        +-- secret arm -> resolve + optional provider runup -> StaticTokenSource
        |
        +-- gh-cli / az-cli -> no resolve or runup -> RuntimeCommandSource
        |
        v
core builds one UserCredentialState
        |
        v
per-user reconciler atomically replaces Agentworks-owned state
        |
        v
Git request -> stable launcher -> generation-owned dispatcher -> static value or runtime command
```

Providers own forge knowledge: allowed configuration, selector construction, secret-token
verification, and the fixed runtime command recipe for their CLI arm. Core owns composition:
collision checks, deterministic selection, safe materialization, Git registration, migration, and
complete reconciliation.

## Components

### Provider acquisition models

`TokenAcquiringConfig.token` remains a discriminated union, but the union is no longer one shared
global set of arms. Each provider's concrete config closes the modes it supports:

```text
GitHubConfig.token = SecretToken | GitHubCliToken
AzDOConfig.token   = SecretToken | AzureCliToken
```

The existing omission and scalar-shorthand behavior belongs to `SecretToken` and remains stable.
Only that arm carries `SecretRef`. Structural reference extraction therefore continues to derive the
correct graph without provider code or special-case filtering.

The provider capability contract advances atomically from version 2 to version 3. There is no
compatibility adapter inside the capability registry: every in-tree implementation migrates in the
same change, and registration refuses an old implementation.

### Helper definitions

The provider operation surface becomes one helper-definition operation rather than the current
`helper_entry` plus `credential_lines` pair:

```text
HelperDefinition
  selector: CredentialSelector
    host
    exact_repositories[]
    owner_or_org?
  source:
    StaticTokenSource
      secret_name
      response_username
    RuntimeCommandSource
      response_username
      argv
      environment
```

These are frozen, validated, inert values. `StaticTokenSource` contains the secret name, never the
token. The material builder joins it with scoped secret delivery after runup. Runtime sources are
fixed provider-owned command recipes executed by one core helper; operator configuration cannot
supply commands or executable bodies.

The selector does not carry a store username. Selection maps a request directly to a helper source,
which removes the current accidental coupling between scope routing and static-store lookup.

### User credential state builder

Core builds one complete `UserCredentialState` for a target user:

- validated selectors and acquisition sources;
- the static token file, containing only secret-arm credentials;
- a generation-owned dispatcher and optional static store;
- an Agentworks-owned Git include;
- legacy cleanup targets.

The builder rejects duplicate exact-repository and owner/organization claims on one host. It orders
records deterministically and emits no generation files when the desired definition set is empty.
The reconciler owns the fixed stable launcher and lock.

### Per-user reconciler

Admin and agent initialization call the same reconciler unconditionally. The caller supplies the
target user's transport/home, the complete desired helper definitions, and resolved values for the
secret-backed subset.

The reconciler:

1. builds the entire state before target mutation;
2. takes a bounded exclusive lock;
3. cleans an abandoned stage, disables legacy credential material/registration, stages a private
   generation, and compares it to active state;
4. atomically replaces the stable launcher/current generation when changed and ensures the exact
   include once for nonempty state, or removes the launcher/include for empty state;
5. deletes inactive generations and releases the lock.

Each helper invocation enters through the stable launcher, takes a bounded shared lock, and executes
the dispatcher from the resolved immutable generation. That dispatcher loads its selected value,
closes the descriptor, and only then starts a child process. Replacement and garbage collection
cannot race its read, while CLI descendants cannot retain the lock. The include owns the helper
registration and `useHttpPath` behavior for only the hosts represented in the desired state. For
each managed host it resets inherited helper values before registering the Agentworks launcher;
unrelated hosts retain operator-managed helpers. Agentworks does not delete or rewrite any
indistinguishable operator `credential.helper` value.

The launcher and dispatcher share one immutable version-1 ABI: unchanged Git arguments and one fixed
inherited lock descriptor that the dispatcher closes after loading its generation. A launcher update
must remain compatible with both adjacent dispatcher generations or use a new versioned managed
root.

### Stable launcher and runtime dispatcher

Git invokes the stable Agentworks launcher through the standard credential-helper protocol. The
launcher acquires the shared lock and executes the dispatcher from the resolved generation. That
dispatcher parses the bounded request, selects exact repository before owner/organization before
default, and handles the selected static source or fixed runtime recipe.

The dispatcher is a read-only generator:

- **Static token** reads the selected credential from the mode-0600 managed store and returns the
  provider's username/password response.
- **GitHub CLI** runs the fixed GitHub CLI recipe without interaction for the active `github.com`
  identity and returns the token as the HTTPS credential.
- **Azure CLI** runs the fixed Azure DevOps token recipe under the active Azure CLI identity and
  returns organization-as-username plus token-as-password, matching Git Credential Manager's
  username/password treatment of Azure Repos Entra tokens.

The helper ignores unsupported `store`/`erase` operations except for the existing safe rejection
diagnosis. It does not cache, log, or write the runtime token.

### One write path

`vm add-git-credential` is removed. It is a second desired-state path that cannot safely preserve
the complete installed static-token set from the workstation without reading target secrets or
adding target-side merge metadata. Operators declare credentials on admin or agent templates and run
reinit; those two callers share the one complete reconciler.

## Lifecycle

### Registry and graph

Provider configuration remains validated by the capability framework. The graph derives a secret
edge only from `SecretToken.secret`. CLI-backed credential nodes have no secret refs and can pass
preflight without a secret source.

### Boundary resolution

Composition roots collect resolved values only for secret-backed credentials. Their token mapping is
partial by design: every `StaticTokenSource` must have a value, while runtime sources must not. The
current invariant that every provider needs a token mapping is retired.

### Runup

The git-credential node calls provider runup only for a secret acquisition arm and only when
`defaults.runup_git_credentials` is enabled. The existing provider-specific HTTP probe and caller
policies remain:

- multi-credential initialization skips a definitively rejected static token and records partial;
- network indeterminacy warns and continues;
- CLI-backed arms do no runup work.

### Materialization

Materialization accepts the full helper-definition set and the resolved static-token subset. It
line-safety-checks every static value at the final sink, renders the deterministic dispatcher, then
reconciles even if the surviving set is empty.

### Git operation

The runtime helper sees the target user's environment. Missing executable, CLI command failure,
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

The provider supplies the fixed `github.com` recipe and disables prompting. GitHub CLI owns active
account selection and its ordinary token behavior; Agentworks neither authenticates it nor parses
its configuration.

### Azure CLI

The provider supplies the fixed Azure DevOps resource recipe. The response uses organization as
username and the Entra token as password, following current Git Credential Manager prior art and
pending the required real Azure Repos proof. The exact command and evidence live in the LLD and
research artifact.

## Security Boundaries

- Provider configuration can choose only closed acquisition arms; it cannot inject commands.
- Runtime helpers execute no login and receive no Agentworks secret.
- Static token verification remains after secret resolution and before target writes.
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
| Missing secret or unsafe static token      | resolution   | existing secret/validation failure                          |
| Static token definitively rejected         | runup        | skipped/partial multi-credential initialization             |
| Static probe network indeterminate         | runup        | warning; helper is installed                                |
| `gh`/`az` absent or command fails          | Git runtime  | nonzero helper with fixed value-safe diagnostic             |
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
