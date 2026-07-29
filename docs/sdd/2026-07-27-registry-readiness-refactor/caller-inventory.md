# Caller inventory: registry readiness refactor

**Dated snapshot: 2026-07-29** (verified against `HEAD` on `feat/registry-readiness-refactor`, which
tracks `main`). This is the pre-plan artifact the FRD names: it is both the **R11 anti-bypass
guard's baseline** and the migration checklist. It stands in for a full `migration-strategy.md`, the
change is atomic and in-repo with no data movement or rollout. When the plan lands each row becomes
a migration checkbox; when the guard lands (plan phase 6) the "banned after" rows are what it pins.

Line numbers drift as the work proceeds. They are anchors at the dated snapshot, not a contract; the
pattern per row is the contract.

## Legend

- **SPLIT**: a `validate_config` site that becomes `dependencies(config)` (edge extraction, total)
  or `validate(config)` (throwing), or both.
- **MOVE**: a validation call that moves out of decode/load into the finalize `validate` pass (R3).
- **RENAME**: `disabled_reason` to `not_ready`, or `referenced_resources` to `dependencies`, a
  mechanical rename plus the reshape the LLDs pin.
- **GRAPH**: a recompute/registry-probe that must read the retained graph instead (R11).
- **EXEMPT**: a site that looks like a banned pattern but is the single sanctioned derivation or a
  builder input; the guard must whitelist it (HLA component 7).
- **REMOVE**: deleted outright.

## A. The `validate_config` split (R2), across all four capability kinds

**Definitions (become `dependencies` + `validate`):**

| Site                                          | Kind                    | Note                                                                                                                                                                                                       |
| --------------------------------------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `capabilities/base.py:315`                    | base default            | "accepts no configuration" default splits into a no-op `dependencies` + a throwing `validate`                                                                                                              |
| `capabilities/harness/shell.py:63`            | harness                 |                                                                                                                                                                                                            |
| `capabilities/harness/claude_code.py:58`      | harness                 |                                                                                                                                                                                                            |
| `capabilities/vm_platform/lima.py:114`        | vm-platform             |                                                                                                                                                                                                            |
| `capabilities/vm_platform/proxmox.py:57`      | vm-platform             |                                                                                                                                                                                                            |
| `capabilities/vm_platform/azure_vm.py:238`    | vm-platform             |                                                                                                                                                                                                            |
| `capabilities/git_credential/github.py:106`   | git-credential-provider | scope re-parse (`github.py:118`) folds into `validate`                                                                                                                                                     |
| `capabilities/git_credential/azdo.py:36`      | git-credential-provider | org check (`azdo.py:58`) folds into `validate`                                                                                                                                                             |
| `secrets/backends.py:99` (`validate_mapping`) | secret-backend          | already the backend's `validate(mapping)`; gains a `dependencies(mapping)` counterpart (the `SecretBackend` docstring anticipates it). Impls: `env_var.py`, `secrets/prompt` backend, `onepassword.py:264` |

**Invocation sites (SPLIT / MOVE):**

| Site                                                                            | Current role                                                                | Target                                                                                                                                                           |
| ------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `capabilities/base.py:306-307`                                                  | construct-time `_secret_refs` extraction via `validate_config`              | construct-time `validate(config)` (keeps the invariant, R3) **plus** `dependencies(config)` for `_secret_refs`; the single sanctioned derivation (EXEMPT, see F) |
| `vms/sites.py:83`                                                               | edge extraction inside `referenced_resources`                               | `capability.dependencies(platform_config)` (SPLIT)                                                                                                               |
| `git_credentials/credential.py:99`                                              | edge extraction inside `referenced_resources`                               | `capability.dependencies(provider_config)` (SPLIT)                                                                                                               |
| `sessions/template.py:107`                                                      | edge extraction inside `referenced_resources`                               | `capability.dependencies(harness_config)` (SPLIT)                                                                                                                |
| `manifests/decode.py:176` (harness)                                             | decode-time validation                                                      | MOVE to finalize `validate`                                                                                                                                      |
| `manifests/decode.py:242` (git-credential)                                      | decode-time validation                                                      | MOVE to finalize `validate`                                                                                                                                      |
| `manifests/decode.py:310` (platform)                                            | decode-time validation                                                      | MOVE to finalize `validate`                                                                                                                                      |
| `config/loaders_sessions.py:163`                                                | load-time harness validation                                                | MOVE to finalize `validate`                                                                                                                                      |
| `config/loaders_core.py:384`                                                    | load-time git-credential validation                                         | MOVE to finalize `validate`                                                                                                                                      |
| `config/loaders_resources.py:430`                                               | load-time platform validation                                               | MOVE to finalize `validate`                                                                                                                                      |
| `sessions/templates.py:175`                                                     | template-resolution harness validation (`harness_for(...).validate_config`) | SPLIT to `validate`; confirm in LLD (e) whether this resolve-time call is redundant with the finalize pass or a distinct resolved-template check                 |
| `migrate/planning.py:458` (platform), `:502` (git-credential), `:548` (harness) | migrate dry-run validation                                                  | SPLIT to `validate` (migrate is not a finalized-registry path; keep its explicit validation)                                                                     |

## B. The `disabled_reason` to `not_ready` rename + reshape (R6, R4)

| Site                                                                         | Current role                                                                            | Target                                                                                                                                                                         |
| ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `capabilities/base.py:340`                                                   | the `disabled_reason(self) -> str \| None` hook (bound-instance)                        | RENAME to `not_ready`, reshaped to a **non-constructing classmethod** `not_ready(config) -> Readiness` (LLD c)                                                                 |
| `capabilities/vm_platform/lima.py:99`                                        | lima's local-`limactl` check                                                            | RENAME + non-constructing reshape                                                                                                                                              |
| `capabilities/vm_platform/wsl2.py:475`                                       | wsl2's readiness                                                                        | RENAME + non-constructing reshape                                                                                                                                              |
| `vms/kinds.py:197`                                                           | `_VMSiteKind.disabled_reason(registry, resource)` (reaches into `VM_PLATFORM_REGISTRY`) | RENAME to `not_ready`; the fold hands deps' verdicts instead of a live registry (GRAPH + RENAME)                                                                               |
| `vms/sites.py:169` `site_disabled_reason` (+ callers `:146`, `:150`, `:258`) | the three-step readiness chain                                                          | RENAME; chain splits per LLD (c): platform-missing to resolve-time hard error, unsupported to platform node's `not_ready`, tool check to site's `not_ready` off the graph impl |
| `resources/kind.py:169-181`, `resources/kind.py:172-177`                     | docstrings documenting the hook                                                         | RENAME (docs)                                                                                                                                                                  |
| `resources/inspect.py:76,109,198,217-235,329`                                | `disabled_reason` field + `disabled_reason_for` projection                              | RENAME to `not_ready` / `not_ready_for`, reads stored verdict via `readiness_of` (GRAPH)                                                                                       |
| `resources/inspect.py:470,504-505`                                           | `(disabled)` list cell + `Disabled:` describe line                                      | RENAME to readiness vocabulary (R9.1)                                                                                                                                          |
| `doctor.py:241,259,272`                                                      | `site_disabled_reason` usage                                                            | GRAPH (read `readiness_of`) + RENAME                                                                                                                                           |
| `cli/commands/resource.py:111`                                               | docstring                                                                               | RENAME (docs)                                                                                                                                                                  |

## C. The `referenced_resources` to `dependencies` rename + suppression removal (R2, R13)

**Resource-level definitions (RENAME to `dependencies(context)`):**

`vms/sites.py:52` (VMSite, **plus** suppression removal at `sites.py:60-71`), `vms/templates.py:45`,
`git_credentials/credential.py:71`, `sessions/template.py:66`, `apt.py:78`,
`declared_resource.py:55`, `agents/template.py:47`, `vms/template.py:84`, `vms/admin.py:62`,
`workspaces/template.py:32`.

**Deliberately NOT renamed:** `env/entry.py:38` `EnvEntry.referenced_resources(source)`, an internal
aggregation each template's `dependencies()` composes (HLA component 2), not a graph-node method the
builder calls.

**Call sites (GRAPH / builder):**

| Site                                                                                | Current role                                         | Target                                                                                                                |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `registry.py:321` (`_collect_new_references`), `:397-402` (`_referenced_resources`) | the builder walk                                     | becomes the graph build; passes each resource the uniform build `context` (EXEMPT for the secret's backend-list read) |
| `registry.py:542` (`_edges_from`, cycle detection)                                  | re-derives edges (re-runs `validate_config`)         | GRAPH: read `edges_of`                                                                                                |
| `resources/walk.py:71,79-83` (`collect_secrets_for`)                                | ad-hoc transitive DFS                                | GRAPH: thin filter over `reachable_from`                                                                              |
| `vms/nodes.py:412`                                                                  | `secret_refs` recompute via `referenced_resources()` | GRAPH: read `edges_of`                                                                                                |
| `git_credentials/nodes.py:93`                                                       | `secret_refs` recompute via `referenced_resources()` | GRAPH: read `edges_of`                                                                                                |

## D. The `unsupported_reason` publication gate (R13)

| Site                                                                                                     | Current role                                                        | Target                                                 |
| -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------ |
| `capabilities/vm_platform/__init__.py:99`                                                                | `if unsupported_reason() is not None: continue` (skips publication) | REMOVE the skip: publish unconditionally               |
| `capabilities/vm_platform/__init__.py:52,89`                                                             | docstrings asserting the gate                                       | RENAME (docs): host-support is readiness, not absence  |
| `vms/sites.py:70`                                                                                        | suppression's `unsupported_reason` check                            | REMOVE (folds into C's suppression removal)            |
| `vms/sites.py:186`                                                                                       | `site_disabled_reason`'s unsupported step                           | moves into the platform node's `not_ready` (LLD c)     |
| `doctor.py:229`                                                                                          | reads `unsupported_reason` directly                                 | GRAPH: read `readiness_of`                             |
| Definitions kept (feed `not_ready`): `capabilities/vm_platform/base.py:107`, `wsl2.py:466`, `lima.py:95` |                                                                     | unchanged as host-support source, consumed by the fold |

## E. The inbound `references` field to `dependents_of` (R11, HLA component 1)

**Field definitions to REMOVE (moved onto the graph):** `declared_resource.py:53`,
`capabilities/harness/kinds.py:44`, `capabilities/git_credential/kinds.py:55`,
`capabilities/vm_platform/__init__.py:79`, `secrets/kinds.py:68`.

| Reader                                      | Current role                                                | Target                                   |
| ------------------------------------------- | ----------------------------------------------------------- | ---------------------------------------- |
| `resources/inspect.py:187,327`              | `getattr(resource, "references", ())`                       | GRAPH: `dependents_of`                   |
| `resources/inspect.py:509,515`              | describe "Referenced by" rendering                          | GRAPH: `dependents_of`                   |
| `secrets/inspect.py:327,391,396`            | `getattr(decl, "references", ())` in `secret describe`      | GRAPH: `dependents_of`                   |
| `registry.py:257`                           | attaches `references` via `dataclasses.replace` in finalize | moves to the graph `attach` pass (LLD b) |
| `registry.py:462-478` (`_references_tuple`) | builds the inbound tuple                                    | moves to the graph builder (LLD a)       |

## F. Secret-ref recompute / registry-probe paths (R11, HLA components 6, 6a)

| Site                                                                                                                      | Current role                                         | Target                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `capabilities/base.py:306`                                                                                                | `_secret_refs` construct-time cache                  | keep, source becomes `dependencies(config)`; the single sanctioned op-time derivation (EXEMPT)                                                                                                 |
| `capabilities/harness/base.py:180` (`Harness.secret_refs`), `capabilities/git_credential/base.py:131-137` (`secret_name`) | read `_secret_refs`                                  | unchanged readers of the exempt cache; LLD (b/d) confirms single-derivation vs graph-threading                                                                                                 |
| `secrets/resolve.py` `active_backends` (`SECRET_BACKEND_REGISTRY` probe)                                                  | resolver reaches into the live backend registry      | REMOVE: read backend impls off the graph node (LLD d)                                                                                                                                          |
| `secrets/resolve.py:108` (`validate_chain`)                                                                               | per-mapping validation + reachability, post-finalize | SPLIT: per-mapping `validate` moves into the finalize pass (via the secret's `validate`, HLA component 2); reachability stays an eager post-finalize boundary check, now graph-reading (LLD d) |
| `secrets/kinds.py:188` (`collect_secrets_for` caller)                                                                     | consumes the walk                                    | unaffected; the walk internals move to `reachable_from`                                                                                                                                        |
| `bootstrap.py:105-106` (`validate_chain`, `validate_sites`)                                                               | post-finalize boundary checks                        | `validate_chain` splits as above; `validate_sites` (`sites.py:266`) is a pure `defaults.site` lookup, essentially unchanged (name resolved against the graph)                                  |

## G. Operator surfaces (R6, R9, HLA component 8)

| Site                                                                                                                                                                                    | Current role                                                                       | Target                                                                                                                                 |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `secrets/inspect.py:150-201` (grid render), `:436`                                                                                                                                      | grid cell `disabled`/`enabled` literals                                            | readiness-aware cells: would-attempt identifier / `not ready: <reason>` / won't-attempt (LLD e)                                        |
| `cli/commands/secret.py:32`                                                                                                                                                             | docstring using `disabled`/`enabled` for backends                                  | RENAME to the readiness/opt-in vocabulary                                                                                              |
| `doctor.py` `_check_vm_platforms` (`:220-231`), `_check_vm_sites` (`:242-322`), `_check_secrets`                                                                                        | ad-hoc `unsupported_reason` / `site_disabled_reason` recompute; one row per secret | GRAPH reads; **new** secret-backends group parallel to `_check_vm_platforms` (LLD e)                                                   |
| `env show --reveal-secrets` (the flag + `orchestration/secrets.py` predictor at `secrets.py:85`)                                                                                        | reveal flag + optimistic resolvability predictor                                   | RENAME flag to `--resolve` (R9.8); predictor becomes readiness-aware in lockstep (LLD d/e); decide `--reveal-secrets` deprecated-alias |
| Docs: `docs/guides/resources.md` "Secrets: backends and the chain", `sample-config.toml`, `cli/README.md` (~line 787 `--reveal-secrets`), command/section help strings, completion tree | permanent surfaces                                                                 | update in lockstep with the surface change (LLD e; the always-consider-completions rule)                                               |

## Guard baseline (R11): the banned patterns after migration

The guard test (plan phase 6) pins that these do not return:

1. Re-walking a resource's `dependencies()` to reconstruct the edge set outside the graph build
   (was: `registry.py:542`, `walk.py:71`, `vms/nodes.py:412`, `git_credentials/nodes.py:93`).
2. A `*_REGISTRY.get(...)` availability probe in edge production or readiness (was:
   `vms/kinds.py:197` via `VM_PLATFORM_REGISTRY`, `secrets/resolve.py` via
   `SECRET_BACKEND_REGISTRY`).
3. A lazy readiness recompute instead of reading `readiness_of` (was: `inspect.disabled_reason_for`,
   `site_disabled_reason` callers, `doctor.py`).
4. Reading inbound edges/usage off a resource dataclass `references` field (was: section E readers).

**Whitelisted (EXEMPT), or the honest path trips the guard:**

- A capability computing **its own** config-implied refs from **its own** config via
  `dependencies(config)` at construct (`capabilities/base.py:306`, for `_secret_refs`).
- The graph **builder** handing a resource's `dependencies(context)` a controlled context (the
  available-backend list the `secret` reads, HLA component 2).
