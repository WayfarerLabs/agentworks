# Functional Requirements: Runtime Git Identities

- Status: Draft for design review
- Date: 2026-08-28
- Architecture: [hla.md](./hla.md)
- Detailed design: [credential-helper-lld.md](./credential-helper-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Plan: [plan.md](./plan.md)

## Summary

Agentworks currently models every Git credential as a secret-backed personal access token. During
user initialization it verifies the token, writes it into a managed credential store, generates a
scope-selecting Git credential helper, and registers that helper globally. This works for PATs but
does not represent dedicated identities already authenticated through GitHub CLI or Azure CLI.

Git credential providers will become narrowly responsible for producing scoped Git credential helper
definitions. A credential may continue to obtain a static token from an Agentworks secret, or it may
delegate runtime token acquisition to the authenticated identity of `gh` or `az`. Core Agentworks
code will reconcile every Agentworks-owned helper and Git configuration artifact on every admin or
agent user initialization, including the transition to no configured credentials.

Secret-backed tokens retain their current runup validation. CLI-backed credentials deliberately do
not check tool installation or authentication during provisioning; their helpers acquire a fresh
credential at Git-operation time and return a clear, value-safe failure if the CLI is unavailable or
unauthenticated.

## Goals

1. Support GitHub CLI and Azure CLI identities without storing their short-lived access tokens in
   Agentworks configuration or on-VM credential files.
2. Make the provider contract describe Git helper output rather than PAT provisioning.
3. Preserve useful early validation for secret-backed static tokens.
4. Make Agentworks-owned Git credential state converge exactly on every user initialization,
   including complete removal.
5. Preserve repository-, owner/organization-, and host-default selection.
6. Coexist with operator-managed Git configuration and helpers outside the hosts Agentworks owns.
7. Remove the imperative credential-install path instead of creating a second source of desired
   state beside declarative user initialization.
8. Leave future CLI authentication features independent from helper installation.

## Terminology

- **Provider**: the code capability selected by a `git-credential` resource, currently `github` or
  `azdo`.
- **Acquisition mode**: the `token.mode` arm that says where a helper gets its credential: `secret`,
  `gh-cli`, or `az-cli` as allowed by that provider.
- **Selector**: a provider-owned, validated host plus optional repository or owner/organization
  scope used to choose one helper for a Git request.
- **Helper definition**: inert provider output describing the selector and one core-static or
  provider-runtime helper implementation.
- **Reconciliation**: rebuilding the complete Agentworks-owned per-user Git credential state from
  desired helper definitions.
- **User initialization**: admin initialization/reinitialization or agent creation/reinitialization
  for the target Linux user. It does not mean authenticating a third-party CLI.

## Users and Stories

### Operator using dedicated GitHub identities

The operator authenticates `gh` as the intended identity in an agent or admin user's environment,
configures a GitHub credential with `token.mode: gh-cli`, and expects later HTTPS Git operations to
use the token exposed by that user's active `gh` identity.

### Operator using an Entra service principal for Azure DevOps

The operator authenticates `az` as a service principal, configures an Azure DevOps credential with
`token.mode: az-cli`, and expects Git operations against the declared Azure DevOps organization to
obtain a short-lived Entra token at runtime.

### Operator retaining a secret-backed PAT

The operator keeps the existing omitted, scalar, or explicit `secret` token spelling and receives
the same secret resolution and provider-specific token validation before the helper is installed.

### Operator removing credentials

The operator removes every credential from an admin or agent template and reruns initialization.
Agentworks removes its prior token store, helper scripts, include, and exact global include
reference. No stale Agentworks credential remains available.

## Requirements

### R1: Narrow provider purpose

A git credential provider MUST validate its provider-specific configuration and emit an inert,
scoped Git credential helper definition. It MUST NOT write user files, alter Git configuration,
install or authenticate a CLI, or resolve secrets itself.

The provider contract MAY validate a materialized secret-backed token during runup. That is the only
acquisition-mode-specific provisioning operation in this effort.

### R2: Closed acquisition arms

The existing `token` discriminated union remains the configuration surface:

- `github` accepts `secret` and `gh-cli`.
- `azdo` accepts `secret` and `az-cli`.
- Omission and scalar shorthand continue to select `secret` exactly as they do today.
- Every non-secret arm requires its explicit discriminator.
- A provider rejects acquisition modes it does not implement.

Only the `secret` arm declares a `secret` reference. CLI-backed arms MUST add no synthetic secret
edge and MUST NOT accept a secret field.

### R3: Secret-backed runup validation

For `token.mode: secret`, initialization MUST resolve and line-safety-check the declared token and
MUST retain the provider-specific authenticated runup check. A definitive rejection keeps the
current multi-credential user-initialization semantics: skip that credential, warn, and record
partial initialization. Network indeterminacy warns and continues unverified.

`defaults.runup_git_credentials = false` continues to disable only this static-token verification.
It has no effect on runtime CLI acquisition.

### R4: Runtime CLI acquisition

For `gh-cli` and `az-cli`, initialization MUST emit and install a helper without testing whether the
corresponding executable is installed, whether it is authenticated, or whether it can reach the
forge. This avoids coupling helper installation to tool-install and login ordering.

At each matching Git `get` request:

- `gh-cli` obtains the active token for `github.com` from GitHub CLI.
- `az-cli` obtains a fresh token for the Azure DevOps resource from the active Azure CLI identity.
- The helper emits only the Git credential protocol response required for that forge.

The helper MUST NOT initiate login, prompt, open a browser, select an account/tenant/subscription,
or invoke a login command. Ordinary token-cache reads and refreshes performed internally by the
selected CLI are allowed; Agentworks does not reimplement or control them.

### R5: Runtime failure contract

A CLI-backed helper MUST fail nonzero with one concise, actionable stderr diagnostic when its CLI is
missing, the CLI command fails, returns an empty or malformed token, or times out. It MUST NOT print
a token or include captured token output in an error. Repository authorization is the forge's
decision; a valid token for the wrong identity fails through Git's normal authentication path and
the existing fixed selected-credential diagnosis.

Unsupported `store`, `erase`, and future Git helper operations MUST be safely ignored unless the
existing Agentworks rejection diagnosis applies. Runtime acquisition MUST be non-interactive and
bounded. Waiting for the stable credential-state lock MUST also be bounded and fail with fixed retry
guidance rather than hanging Git.

### R6: Scope selection

The existing deterministic selection order remains:

1. exact repository;
2. owner or Azure DevOps organization;
3. host default.

Scope collisions remain configuration errors. Helper selection MUST NOT depend on a static-store
username; a selected helper supplies the forge-appropriate credential response directly.

### R7: Total per-user reconciliation

Admin and agent initialization MUST always invoke the Agentworks Git credential reconciler, even
when the desired credential list is empty or every secret-backed credential was rejected.

The reconciler MUST rebuild the complete Agentworks-owned state from desired helper definitions. On
an empty desired state it MUST remove every provably Agentworks-owned helper, static-token store,
generation, Git include, and exact include reference left by current or legacy Agentworks versions.
One empty stable lock file MAY remain solely to serialize concurrent reconciliations and helper
starts. A generic `credential.helper=store` value that is indistinguishable from operator
configuration MAY remain, but the Agentworks-owned store it formerly read MUST be absent.

Repeated initialization with the same desired inputs MUST be idempotent. Removing, adding, changing
scope, or changing an acquisition mode MUST converge in one run without preserving stale material.
Concurrent helper requests MUST use one complete generation while reconciliation and garbage
collection wait for them rather than mixing selection and credential bytes.

Migration ordering MUST disable legacy token material before removing its registrations or the new
host reset. A failure at any cleanup/activation boundary MAY leave authentication absent until the
next reinit but MUST NOT reactivate or serve a stale credential.

### R8: Git configuration ownership

Agentworks MUST register its helpers through one Agentworks-owned included Git configuration file.
It MUST NOT use an unqualified `--replace-all credential.helper` that deletes operator-managed
helpers.

For each host managed by Agentworks, the included configuration MAY reset earlier helpers for that
host before registering the Agentworks launcher. Unrelated hosts and operator configuration MUST
remain untouched. When Agentworks manages no credentials, its include reference is absent.

### R9: One declarative write path

Admin users and agent users MUST consume the same helper-definition builder and reconciler. Their
only differences are transport and home path.

The imperative `vm add-git-credential` command MUST be removed. Operators declare credentials on the
admin or agent template and run initialization/reinitialization. The implementation MUST NOT add an
installed-state manifest, target-side merge protocol, or another credential writer.

### R10: Security and containment

- Static tokens remain in mode-0600 Agentworks-owned storage and never enter helper scripts or Git
  configuration.
- Runtime tokens exist only in helper process memory and the Git credential-protocol response.
- Provider-derived paths, selectors, argv/environment recipes, usernames, generated paths, and
  emitted protocol fields are validated before use.
- Reconciliation stages files in a private per-user location and does not expose partially written
  credentials.
- One stable shared/exclusive lock protects generation selection, helper reads, activation, and
  cleanup.
- Diagnostics, logs, exceptions, tests, and generated metadata contain no credential values.
- Helpers execute as the target user and use that user's `HOME`, CLI configuration, and active
  identity.

### R11: Operator and authoring surfaces

The implementation MUST update the provider-authoring README, generated resource schema and
examples, sample configuration where applicable, resource/guide teaching, command reference, and
upgrade guidance in the same change that alters behavior.

The permanent provider README MUST lead with the narrow helper-definition purpose and describe
static and runtime helper implementations without making PATs the universal model.

### R12: Future authentication features remain separate

This effort installs consumers of existing `gh` and `az` identities. It MUST NOT introduce secrets
for GitHub Apps or Entra applications, run `gh auth login` or `az login`, mint or renew GitHub App
installation tokens, or add the planned user-feature mechanism.

Future `gh-auth` and `az-cli-auth` user features may authenticate the tools idempotently before
runtime Git use. They consume this contract but are neither required nor implemented here.

## Non-goals

- Explaining or managing GitHub App installation, Azure DevOps organization membership, Entra
  permissions, or forge-side authorization.
- Replacing GitHub CLI or Azure CLI with an Agentworks OAuth client.
- Installing `gh`, `az`, Git Credential Manager, or Git itself as an implicit side effect of a
  credential declaration.
- Supporting SSH Git remotes; this contract serves HTTPS remotes.
- General arbitrary-command credential helpers in operator configuration.
- Caching runtime CLI tokens beyond the CLI's own behavior.

## Acceptance Criteria

### AC1: Configuration and graph

Existing secret spellings remain valid and produce the same secret edge. Explicit `gh-cli` and
`az-cli` declarations validate only under their owning providers, produce no secret edge, and appear
accurately in schema/resource descriptions without exposing identity data.

### AC2: Secret validation

Focused tests prove that secret-backed credentials resolve, line-safety-check, and run their
provider probe under the existing enabled/disabled and skip/partial policies. CLI-backed modes
perform none of those operations during provisioning.

### AC3: Runtime GitHub identity

In a disposable target-user home with a fake and then real authenticated `gh`, a matching Git
credential query obtains the active token at runtime; missing/failed/malformed/timeout cases fail
clearly and leak no value. A live GitHub HTTPS clone/fetch and a reversible write operation pass
under integration-test authorization.

### AC4: Runtime Azure identity

In a disposable target-user home with a fake and then real authenticated `az`, a matching helper
obtains an Azure DevOps-audience token at runtime. Live Azure Repos clone/fetch and a reversible
write operation prove that the emitted Git credential form works with the repository service, not
merely its REST API. Missing/failed/malformed/timeout cases are value-safe; a valid token for an
identity without repository access produces Git's normal forge rejection.

Before this design merges, a ready-PR integration run MUST prove the proposed
organization-username/token-password form with a real read-only Azure Repos Git operation. A failed
or unavailable proof is not approval to begin implementation.

### AC5: Selection

Secret and CLI-backed helpers can coexist on one host. Exact-repository, owner/organization, and
default selection, collision refusal, embedded-username advisories, and no-match behavior remain
deterministic.

### AC6: Reconciliation

For admin and agent users, tests start from current legacy files, the new managed layout, mixed
operator Git configuration, and no prior state. Same-input reruns are byte-stable; each add, remove,
scope change, and mode transition converges; empty desired state removes all provably
Agentworks-owned credential/routing state except the inert stable lock while preserving unrelated
helpers and Git config. Any indistinguishable generic `store` helper left behind has no
Agentworks-owned credential file to serve. Concurrent helper/swap/cleanup tests prove one invocation
never mixes generations or loses files while using them. Mixed legacy/new fault injection proves
every mutation boundary is stale-safe, and contention tests prove lock waits and child-descriptor
lifetimes are bounded.

### AC7: Single write path

The old direct-add command and its static-store append/fallback code are absent. Admin and agent
initialization are the only credential writers and converge from their complete declared lists.

### AC8: Shipped collateral and gates

Permanent provider documentation, resource/guide surfaces, schemas, samples, and upgrade guidance
match the final implementation. Focused, full non-integration, static, lint, locked-SDD, Rulesync,
and authorized live GitHub/Azure tests pass with no token in logs or artifacts.
