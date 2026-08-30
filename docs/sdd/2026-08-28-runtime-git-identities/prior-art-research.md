# Prior-Art Research: Runtime Git Identities

- Status: Draft for design review
- Date: 2026-08-28

## Executive Summary

Git's credential-helper protocol is expressly designed for generated credentials and supports
multiple helpers, path-aware selection, host-specific configuration, and safe non-support of
`store`/`erase`. This validates keeping a generic core scope router while providers return either a
final stored credential or their own managed helper behavior.

GitHub CLI provides a direct command for printing the active host/account token and a documented
non-interactive environment posture. Azure CLI can issue short-lived Microsoft Entra tokens for the
Azure DevOps resource, and Microsoft recommends service principals or managed identities over PATs
for automation. Both fit runtime acquisition better than provisioning-time copying.

The important limits are equally clear. Azure DevOps documentation demonstrates bearer-token Git
usage. Current Git Credential Manager goes further: its Azure Repos provider returns the same Entra
access token as a standard username/password `GitCredential`, including for service principals.
Agentworks adopts that conventional response for Debian Bookworm Git and keeps real Azure Repos Git
acceptance as an implementation merge gate.

## Git Credential Helper Protocol

Git executes configured helpers with `get`, `store`, or `erase`, sends request attributes on stdin,
and accepts credential attributes on stdout. A helper may return no values, may diagnose failures on
stderr, and may safely ignore unsupported operations. Shell snippets and absolute helper paths are
supported. Multiple helpers are tried until Git has a complete credential.

Git normally ignores HTTP path when matching credentials; `credential.useHttpPath=true` makes the
path available to helpers. URL-specific credential sections constrain configuration to a host/path
context, but the target Debian Bookworm Git 2.39 requires exact config-path matches and therefore
cannot itself express today's owner/organization prefix fallback. A small generic dispatcher can do
segment-aware longest-prefix matching without learning forge vocabulary. An empty helper value
resets helpers accumulated at lower-priority configuration levels, which gives the generated include
a way to own configured hosts without deleting unrelated global configuration.

**Design decisions:**

- retain one deterministic Agentworks dispatcher;
- register it through host-specific contexts in one Agentworks-owned include;
- represent matching as exact HTTPS host plus longest segment-aware path prefix, letting providers
  translate repository/owner/organization configuration without core knowing those concepts;
- let the dispatcher ignore `store` and avoid destructive `erase`;
- prove config precedence on Debian Bookworm's Git 2.39.

Sources:

- [Git credential documentation](https://git-scm.com/docs/gitcredentials)
- [Git 2.39 credential documentation](https://git-scm.com/docs/gitcredentials/2.39.0)
- [Git credential plumbing and helper capabilities](https://git-scm.com/docs/git-credential)
- [Git configuration includes](https://git-scm.com/docs/git-config)

## GitHub CLI Identity

`gh auth token` prints the token for the active account on a selected host. `--hostname` makes the
host explicit. GitHub CLI also documents `GH_TOKEN`/`GITHUB_TOKEN` precedence and
`GH_PROMPT_DISABLED`, so an automation-safe helper can delegate identity selection to the CLI
without parsing its config files.

`gh auth login --with-token` accepts a supplied token, but that is an authentication operation and
belongs to the future user-feature layer, not a Git helper. A fine-grained token in environment is
also explicitly recommended over storing it through `gh auth login` for headless use.

**Design decisions:**

- `gh-cli` invokes `gh auth token --hostname github.com` at Git runtime;
- the helper disables prompting and treats the active CLI account as operator-selected state;
- enabled runup checks `gh auth status --hostname github.com` read-only in the target-user
  environment, warns without blocking helper installation, and never authenticates `gh`;
- enterprise hosts are deferred rather than inferred.

Sources:

- [`gh auth token`](https://cli.github.com/manual/gh_auth_token)
- [`gh` environment variables](https://cli.github.com/manual/gh_help_environment)
- [`gh auth login`](https://cli.github.com/manual/gh_auth_login)

## Azure CLI and Azure DevOps

Microsoft recommends Entra service principals and managed identities over PATs for Azure DevOps
automation. Azure CLI can request an access token for the Azure DevOps resource; current guidance
uses resource ID `499b84ac-1321-427f-aa17-267ca6975798`. Tokens are short-lived (up to approximately
one hour), which makes per-Git-operation acquisition appropriate and static provisioning wrong.

The service principal must also be explicitly added to the Azure DevOps organization and granted
Azure DevOps permissions; Entra application permissions alone do not grant repository access.

Official Git guidance demonstrates bearer-header use. The current Git Credential Manager Azure Repos
provider returns the Entra access token as the password of a normal `GitCredential`; for a service
principal its username is the client ID. This is direct prior art for conventional helpers on older
Git, though GCM owns more authentication and storage behavior than this effort needs.

**Design decisions:**

- `az-cli` runs the exact resource-ID/query/TSV command pinned in the LLD;
- the helper returns configured organization as username and token as password, with live Git proof
  required before merge;
- it does not choose or mutate tenant/subscription state;
- enabled runup uses read-only `az account show`, never token acquisition, to warn about current
  target-user readiness without blocking helper installation;
- Agentworks documents that forge-side organization membership/permissions are prerequisites;
- real clone/fetch/push proves the exact credential-helper response before merge.

Sources:

- [Azure CLI `account get-access-token`](https://learn.microsoft.com/en-us/cli/azure/account?view=azure-cli-latest)
- [Issue Entra tokens with Azure CLI](https://learn.microsoft.com/en-us/azure/devops/cli/entra-tokens?view=azure-devops)
- [Use service principals and managed identities in Azure DevOps](https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/service-principal-managed-identity?view=azure-devops)
- [Authenticate to Azure DevOps with Microsoft Entra ID](https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/entra?view=azure-devops)
- [Azure Repos credential managers](https://learn.microsoft.com/en-us/azure/devops/repos/git/set-up-credential-managers?view=azure-devops)
- [Git Credential Manager Azure Repos token model](https://github.com/git-ecosystem/git-credential-manager/blob/00bac415e196099a881371e709574c104156ab8e/docs/azrepos-users-and-tokens.md)
- [Git Credential Manager Azure Repos provider source](https://github.com/git-ecosystem/git-credential-manager/blob/00bac415e196099a881371e709574c104156ab8e/src/Microsoft.AzureRepos/AzureReposHostProvider.cs#L87-L150)

## Current Agentworks Prior Art

The current implementation already has several parts worth preserving:

- tagged provider configuration as the natural place for acquisition modes;
- structurally derived provider secret graph edges and scoped `RunContext` delivery;
- provider-specific secret-token probes;
- deterministic path-based routing outcomes;
- a core-owned helper that refuses destructive erase;
- atomic material building before remote writes;
- per-user admin and agent state.

The parts to retire are accidental PAT assumptions and lifecycle duplication:

- the `token` field, its scalar shorthand, and its implicit whole-source default;
- universal `secret_name`, naked-token calls, and core token mapping;
- `credential_lines` as the provider's main output;
- duplicated admin/agent writes;
- skipping setup when desired state is empty;
- global unqualified `credential.helper` replacement;
- direct-add's static unscoped append path.

**Design decision:** evolve the existing dispatcher/provider boundary rather than add a parallel CLI
credential system. Providers own acquisition and final output; core owns only generic routing,
boundary validation, and state reconciliation.

## Alternatives Considered

### Use `gh auth setup-git`

Rejected. It asks GitHub CLI to own Git configuration globally, bypasses Agentworks' repository and
owner selection, does not cover Azure DevOps, and creates a second configuration owner.

### Use Git Credential Manager for everything

Rejected for this effort. GCM is capable and well established, but adopting it adds installation,
configuration, authentication, storage, and UI behavior that duplicates the planned user-feature
layer. Agentworks already owns a smaller scoped dispatcher and needs only CLI token bridges.

### Copy CLI tokens during initialization

Rejected. Azure tokens are short-lived, authentication may happen after tool installation, and
copied tokens become stale durable secrets. Runtime acquisition is the point of delegating to the
CLI identity.

### Read-only CLI readiness at runup

Accepted as advisory provider behavior. Tool installation and manual/future-feature authentication
can still occur later, so readiness failure cannot block helper installation and success cannot be
treated as a durable guarantee. The provider checks command presence and its CLI's read-only account
status in the current target-user environment, suppresses arbitrary output, and never authenticates
or acquires the final Git token. The runtime helper independently checks again at actual use; core
adds no dependency model or forge-specific probe.

### Remove static token validation for symmetry

Rejected by operator ruling. Secret-backed tokens are already available at runup, and their
provider-specific probe gives useful early feedback. The asymmetry follows real acquisition
semantics: static material can be validated now; CLI material does not exist until runtime.

### Let providers mutate user Git state directly

Rejected. Provider-specific writes would make removed providers impossible to clean up reliably and
would recreate separate admin/agent state protocols. Providers return declarative stored credentials
or managed-helper definitions; one core reconciler owns installation and removal.

### Let operator configuration supply arbitrary helper commands

Rejected. A trusted provider implementation may return its own fixed or safely rendered helper
program, but operator configuration cannot inject a program, executable path, or arguments. This
preserves provider extensibility without turning a credential resource into a command-execution
surface.

### Infer provider output from declared secret presence

Rejected by operator ruling. A provider may need one or several secrets to authenticate to an API,
exchange credentials, or construct a runtime helper; a declared secret is not necessarily the final
Git token. Core can enforce scoped access to declared inputs and validate the generic returned
shape, but it cannot prove the lineage of derived output. Restricting secret-bearing providers to
stored output would encode a false assumption and block valid providers. The provider implementation
owns the relationship between its authorized inputs and either returned output variant.

### Share one source model across providers

Rejected. GitHub and Azure DevOps happen to call a similar provider-local field `source`, but core
does not interpret it and future providers may use a different schema. Separate closed unions keep
mode validity with the implementation that owns it and avoid a central acquisition vocabulary.

### Replace all global helpers as today

Rejected. It deletes operator-managed Git configuration and is unnecessary. An Agentworks-owned
include can reset helpers only in managed host contexts and disappear entirely when no credential is
configured.

### Use Git's newer bearer credential capability for Azure immediately

Not selected. Newer Git versions support `authtype`/`credential`, but the target estate may include
older distribution Git. The first implementation proves a conventional helper response against real
Azure Repos. If that proof fails, the effort returns for a deliberate minimum-Git/design decision
rather than silently growing version-dependent branches.

## Refuted or Do-not-rely-on Claims

- **"A valid Azure DevOps REST token necessarily works as a Git password."** Not established by the
  token API; real Git acceptance is required.
- **"A provisioning check prevents future runtime failure."** False for expiring/revoked tokens and
  mutable CLI authentication.
- **"Removing files is enough to unregister a helper."** False; the exact global include/helper
  references must also be reconciled.
- **"`--replace-all credential.helper` is harmless because Agentworks owns Git."** False; Agentworks
  owns only its generated values, not the operator's entire global Git configuration.

## Open Questions for Implementation Evidence

These do not change the accepted requirements, but must be answered at the named gates:

1. Does the host-scoped reset/include sequence behave as designed on Debian Bookworm's Git 2.39?
2. Before merge, does the selected GCM-style username/password response work through the generated
   helper with a real `az`-issued Entra token for Azure Repos clone, fetch, and push?

Each answer is recorded in permanent implementation documentation or tests, not left load-bearing
only in this research file.

## Source Quality Table

| Source family               | Quality | Used for                                                    |
| --------------------------- | ------- | ----------------------------------------------------------- |
| Git official manuals        | Primary | helper protocol, selection, config, capability behavior     |
| GitHub CLI official manual  | Primary | token command, host/account selection, non-interactive env  |
| Microsoft Learn             | Primary | Azure CLI token command, audience, lifetime, SP permissions |
| Git Credential Manager repo | Primary | mature Azure Repos OAuth/service-principal prior art        |
| Agentworks HEAD             | Primary | current contract, files, lifecycle, and migration baseline  |
