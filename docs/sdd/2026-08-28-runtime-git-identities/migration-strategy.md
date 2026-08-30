# Migration Strategy: Runtime Git Identities

- Status: Draft for design review
- Date: 2026-08-28
- Baseline: `4c47f9c70a58f62f3a2e366f2870013d5fa032b8`
- Requirements: [frd.md](./frd.md)
- Detailed design: [credential-helper-lld.md](./credential-helper-lld.md)

## Current State

At the baseline:

- provider contract version 2 requires `helper_entry` and `credential_lines`;
- every provider extends one-arm `TokenAcquiringConfig`, so every credential has one secret edge;
- provider runup validates every resolved token;
- `build_credential_materials` writes one static `~/.git-credentials`, one generated dispatcher, and
  an include containing only `credential.useHttpPath`;
- admin and agent paths duplicate file writes and global Git registration;
- both paths skip credential setup entirely when their declared list is empty;
- the admin path also skips after every provider is rejected at runup;
- global registration uses `git config --global --replace-all credential.helper`, replacing
  operator-managed helper values;
- `vm add-git-credential` appends only unscoped static credentials and does not regenerate the
  dispatcher;
- comments explicitly acknowledge that removing all credentials leaves Agentworks files stale.

There is no database state to migrate. The persistent estate is provider resource manifests, user
Git config, and per-user files on existing VMs.

## Compatibility Decisions

### Configuration cuts cleanly to provider-owned sources

The manifest schema change is intentionally breaking:

- both built-in providers retire `token` and require a structured `source` table;
- `source.mode` is required;
- scalar source shorthand and omission of the complete source are invalid;
- `source: {mode: secret}` retains the existing `git-token-<credential-name>` default;
- provider scope fields retain their meanings;
- GitHub adds `source.mode: gh-cli`, while Azure DevOps adds `source.mode: az-cli`.

There is no migration tool, dual reader, alias, or warning period. Invalid old manifests fail
through normal schema validation before mutation. The changelog/release notes and upgrade guide
label the change as breaking and provide these exact rewrites:

```yaml
# Old default secret
provider:
  name: github
```

```yaml
# New default secret
provider:
  name: github
  source:
    mode: secret
```

```yaml
# Old named secret
provider:
  name: azdo
  token: my-azdo-token
  org: example-org
```

```yaml
# New named secret
provider:
  name: azdo
  source:
    mode: secret
    secret: my-azdo-token
  org: example-org
```

CLI identity sources use the same required table with `mode: gh-cli` or `mode: az-cli`. GitHub and
Azure DevOps own these similar schemas independently; core adds no shared `source` model.

### Operational manifest cutover and rollback

The old and new binaries intentionally have no common Git-credential manifest spelling. The upgrade
guide therefore treats them as one maintenance-window cutover rather than suggesting manifests can
be migrated gradually:

1. Record the exact installed Agentworks version and make a recoverable copy of the complete config
   directory, including every resource manifest. Inventory every `git-credential` declaration; a
   partial file selection is insufficient because normal registry construction loads the complete
   resource directory.
2. Prepare a complete rewritten resource directory beside the active one. Rewrite every built-in
   provider declaration, including implicit-default declarations, before changing the active tree.
3. Stop concurrent Agentworks commands for that workstation. Upgrade the CLI and atomically replace
   the active resource directory with the prepared directory as one operator-controlled cutover. A
   short validation outage between those two operations is expected: the new binary rejects old
   manifests and the old binary rejects new ones. Existing VM Git state remains unchanged until a
   user reinitialization runs.
4. Run config/registry validation with the new CLI before initializing any user. Then reinitialize
   one selected admin or agent as the canary before continuing the rollout.
5. Before any successful new-format user reinitialization, rollback means restoring the recorded old
   CLI version and complete old config directory together. Never restore only one side. After a
   successful new-format reinitialization, old Agentworks no longer understands the installed
   credential layout; downgrade is not an active-management recovery path, so failures fix forward
   with the new CLI.

The implementation's upgrade guide supplies concrete commands for the supported installation path
and safe directory replacement. It does not add a dual reader, manifest rewriter, or background
fleet mutation.

### Provider implementation contract cuts atomically

Contract version 2 becomes version 3 because the operation shape changes. Core GitHub and plugin
Azure DevOps implementations migrate together. Registration rejects an old provider; no v2 adapter,
deprecated method alias, or long-lived bridge is added.

### Remote user state migrates on reconciliation

Existing VMs are not mutated at package upgrade. The next admin reinit or agent create/reinit runs
the new unconditional reconciler, removes exact legacy Agentworks state, and installs the new layout
or a clean empty state.

This is the earliest safe point because the operation already has the target user, desired
credential list, scoped secret delivery, transport, logger, and initialization status semantics.

## Migration Sequence

### 1. Introduce final types and builders in one implementation branch

Add provider-owned acquisition unions, generic HTTPS scopes, the two version-3 material shapes, the
state builder, the reconciler, and migration cleanup. Tests use the new types directly while
production remains on the old path only within the working branch.

### 2. Convert both providers

Convert GitHub and Azure DevOps to `credential_material(ctx)`. Each provider translates its own
scope fields, reads only its scoped declared secrets, retains provider-owned validation for the
secret arm, and returns either a final stored credential or its fixed CLI managed helper. Input
dependencies and output variants remain independent; synthetic coverage proves a secret-bearing
provider may return either shape without core interpreting the relationship. Update the descriptor
contract version and required operation in the same commit or tightly adjacent commits that never
form a mergeable partial.

### 3. Convert graph and boundary assumptions

Allow credential nodes with zero, one, or several secret edges. Resolve the operation-wide union and
construct one `ScopedSecrets` context per provider. Delete core's resolved-token map and keep the
existing provider-runup caller policies.

### 4. Cut admin and agent initialization together

Replace duplicated writes with the shared unconditional reconciler. Both paths reconcile an empty
state. This cut also replaces global helper mutation with the exact include reference.

### 5. Remove direct add

Delete `vm add-git-credential`, its manager path, tests, and command/guide teaching. Point operators
to declared admin/agent credentials and reinit. Do not replace it with installed metadata or another
imperative writer.

### 6. Delete old machinery

Delete version-2 methods, material wrappers, conditional setup gates, duplicated writers, stale
constants/comments/tests, and the scoped-static direct-add refusal. Do not leave aliases for
internal unreleased names.

### 7. Update permanent collateral and validate live

Rewrite the provider README around stored credentials, managed helpers, provider-owned acquisition,
and generic core scope. Update resource/schema/sample/guide/command and upgrade surfaces, then run
GitHub and Azure live acceptance before merge intent.

## Legacy File Ownership

The released implementation overwrites all of these files wholesale and documents them as
Agentworks-owned:

- `~/.agentworks-git-cred-helper.sh`;
- `~/.agentworks-git-scopes.gitconfig`;
- `~/.git-credentials` while configured through Agentworks;
- the earlier warn-only `~/.agentworks-git-cred-warn.sh` helper.

Migration removes all four exact paths. The released admin and agent initializers overwrite the
entire `~/.git-credentials` file, and the generated helper labels it Agentworks-owned; parsing its
secret lines adds risk while retaining them can keep stale credentials active. The upgrade guide
calls out that manual values placed in this Agentworks-owned file are removed, matching released
reinit ownership. Tests prove cleanup never reads or logs file content.

Under the exclusive stable lock, migration first deletes the Agentworks-owned credential file and
disables the exact old custom helper before removing its registration. A generic
`credential.helper=store` possibly installed by old direct add is indistinguishable from operator
configuration and remains, but has no old Agentworks store to serve. Nonempty reconciliation then
activates the new generation/include; empty reconciliation removes the new include/generations.
Cleanup never reactivates a mechanism that it has already disabled. A failure before disablement may
leave the complete preexisting mechanism unchanged; a later failure may leave a safe no-credential
gap. Retrying initialization converges from either state.

## Git Config Transition

Migration removes only exact values owned by Agentworks:

- `credential.helper = !~/.agentworks-git-cred-helper.sh`;
- `include.path = ~/.agentworks-git-scopes.gitconfig`;
- duplicates of the new include path before adding its single final instance.

It does not unset the entire `credential.helper` key. The new included file resets helpers only in
managed host contexts. Empty desired state removes both old and new Agentworks references.

Tests begin with operator helper values before and after the legacy value, unrelated includes,
duplicates, missing files, and malformed Agentworks files. Every case preserves unrelated config.

## Rollout and Recovery

- No fleet-wide push or database migration runs on CLI upgrade.
- Reinit is the rollout and repair mechanism.
- The reconciler holds the bounded exclusive lock across legacy cleanup, target staging, comparison,
  launcher/current activation, include mutation, and inactive-generation cleanup. Activation failure
  never exposes a partial new generation. A failure before a preexisting mechanism is disabled may
  leave that complete mechanism unchanged; a later failure leaves the Agentworks include absent
  until retry. Existing logger semantics mark initialization partial.
- If new helpers fail at runtime, the operator authenticates/fixes the CLI identity and retries Git;
  reinit is needed only for config/helper changes.
- Downgrading Agentworks after new-format reconciliation is unsupported as an active-management or
  recovery workflow. Paired binary/config rollback is available only before the first successful
  new-format reinit; afterward recovery fixes forward with the new CLI.

## Removal of Credentials

The following transitions are explicit acceptance cases:

| Before                   | Desired after init  | Required result                                       |
| ------------------------ | ------------------- | ----------------------------------------------------- |
| one stored credential    | none                | store/helper/include removed                          |
| one managed helper       | none                | provider helper/include removed                       |
| mixed stored and managed | none                | credential/routing state removed; inert lock may stay |
| rejected last provider   | none survives       | prior installed credential removed, init partial      |
| stored                   | matching CLI arm    | stored value removed, managed helper installed        |
| CLI arm                  | matching stored arm | managed helper removed, provider credential stored    |
| scoped material          | narrower scope      | old generic path scope absent after one run           |

## Migration Definition of Done

- Every released `token` spelling is rejected with an actionable schema path; every documented
  structured `source` rewrite loads.
- Provider contract v2 has no live implementation or adapter.
- Admin and agent zero-credential init removes all provably Agentworks-owned credential/routing
  state; only the inert stable lock may remain. An indistinguishable generic
  `credential.helper=store` may also remain, but the Agentworks-owned credential file it formerly
  read is absent.
- Operator-managed Git configuration outside exact Agentworks-owned paths survives.
- The imperative direct-add command and all of its state-writing code are absent.
- Same-input initialization is byte-stable after the first migration.
- Changelog/release notes prominently label the schema break, and upgrade collateral explains the
  exact manifest rewrites, runtime CLI authentication, cleanup, and downgrade posture.
- Upgrade collateral gives the paired binary/resource-directory cutover, expected validation outage,
  pre-reinit rollback boundary, and post-reinit fix-forward rule.
- GitHub and Azure live tests prove useful Git operations after migration.
