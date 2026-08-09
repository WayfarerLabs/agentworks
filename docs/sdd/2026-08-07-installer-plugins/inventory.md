# Declared installer resource inventory

- Status: Revised R1 inventory, independent artifact review clean, pending roadmap review
- Date: 2026-08-08
- Scope: existing named apt and install-command resources shipped from
  `agentworks/manifests/builtin`
- Inputs: the [FRD](./frd.md) and implementation on `main` at `615aa0da`

## Scope rule

This effort changes declaration ownership, not execution ownership. A row moves when Agentworks
ships it as an optional, named resource selected through an existing resource reference. Core keeps
the generic loaders, validators, dependency extraction, and remote executors because those paths
also serve operator-declared resources.

The operator ruling on 2026-08-08 supersedes the earlier, broader installer classification. Snap,
mise, dotfiles, tmuxinator, and Claude setup remain core. No initializer capability, execution seat,
raw-field gate, default change, or mechanism runner move belongs to this effort.

## Rows that move

The inventory contains exactly 16 rows. Their names, specs, and selecting config fields do not
change.

### `apt` system plugin

| Kind          | Name             | Current declaration         | Existing relationship                  | Rationale                                                                  |
| ------------- | ---------------- | --------------------------- | -------------------------------------- | -------------------------------------------------------------------------- |
| `apt-source`  | `github-cli`     | `builtin/apt-sources.yaml`  | Required by `apt-package/gh`           | Optional third-party repository shipped with the GitHub CLI catalog entry. |
| `apt-source`  | `hashicorp`      | `builtin/apt-sources.yaml`  | Required by `apt-package/terraform`    | Optional third-party repository shipped with the Terraform catalog entry.  |
| `apt-source`  | `nodesource-v22` | `builtin/apt-sources.yaml`  | Required by `apt-package/nodejs`       | Optional third-party repository shipped with the Node.js catalog entry.    |
| `apt-source`  | `ngrok-agent`    | `builtin/apt-sources.yaml`  | Required by `apt-package/ngrok`        | Optional third-party repository shipped with the ngrok catalog entry.      |
| `apt-source`  | `tofuutils-tenv` | `builtin/apt-sources.yaml`  | Required by `apt-package/tenv`         | Optional third-party repository shipped with the tenv catalog entry.       |
| `apt-package` | `gh`             | `builtin/apt-packages.yaml` | References `apt-source/github-cli`     | Optional named package set selected by a VM template.                      |
| `apt-package` | `terraform`      | `builtin/apt-packages.yaml` | References `apt-source/hashicorp`      | Optional named package set selected by a VM template.                      |
| `apt-package` | `nodejs`         | `builtin/apt-packages.yaml` | References `apt-source/nodesource-v22` | Optional named package set selected by a VM template.                      |
| `apt-package` | `ngrok`          | `builtin/apt-packages.yaml` | References `apt-source/ngrok-agent`    | Optional named package set selected by a VM template.                      |
| `apt-package` | `tenv`           | `builtin/apt-packages.yaml` | References `apt-source/tofuutils-tenv` | Optional named package set selected by a VM template.                      |

All ten rows move unchanged into the `apt` plugin's manifest package. The apt kinds and execution
remain core.

### `install-command` system plugin

| Kind                   | Name        | Current declaration             | Existing installed check | Rationale                                                        |
| ---------------------- | ----------- | ------------------------------- | ------------------------ | ---------------------------------------------------------------- |
| `user-install-command` | `oh-my-zsh` | `builtin/install-commands.yaml` | `test_dir`               | Optional named user tool selected by an admin or agent template. |
| `user-install-command` | `bun`       | `builtin/install-commands.yaml` | `test_exec`              | Optional named user tool selected by an admin or agent template. |
| `user-install-command` | `fnm`       | `builtin/install-commands.yaml` | `test_exec`              | Optional named user tool selected by an admin or agent template. |
| `user-install-command` | `nvm`       | `builtin/install-commands.yaml` | `test_file`              | Optional named user tool selected by an admin or agent template. |
| `user-install-command` | `starship`  | `builtin/install-commands.yaml` | `test_exec`              | Optional named user tool selected by an admin or agent template. |
| `user-install-command` | `uv`        | `builtin/install-commands.yaml` | `test_exec`              | Optional named user tool selected by an admin or agent template. |

All six rows move unchanged into the `install-command` plugin's manifest package. Both install
command kinds and every execution scope remain core.

## Surfaces that remain core

| Surface                        | Core-owned behavior retained                                                                                                                                               | Why it does not move                                                                                                                    |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Apt resource framework         | `apt-source` and `apt-package` models, manifest loading, validation, source dependency extraction, source reconciliation, and selected package installation                | Operator manifests use the same kinds and executor. A plugin owns only the shipped catalog rows.                                        |
| Apt bootstrap and VM selection | Phase A packages, the closed Phase B core package set, raw VM `apt`, resource-referencing `apt_packages`, and their current ordering                                       | Bootstrap and raw package names stay core; `apt_packages` is the existing reference surface that selects moved rows without a new gate. |
| Install-command framework      | `system-install-command` and `user-install-command` models, predicate validation, reference extraction, system/admin/agent runners, PATH aggregation, and current ordering | Operator manifests use the same kinds and executors. A plugin owns only six shipped rows.                                               |
| Snap                           | VM template config and the snap installer                                                                                                                                  | No existing named built-in resource row is moving.                                                                                      |
| Mise                           | Admin and agent config, defaults, package installation, config and lockfile writes, activation, pruning, and ordering                                                      | No existing named built-in resource row is moving.                                                                                      |
| Dotfiles                       | Admin and agent source config, synchronization, and checkout-local installation                                                                                            | No existing named built-in resource row is moving.                                                                                      |
| Tmuxinator                     | Platform package installation, workspace-template default, project generation, and links                                                                                   | No existing named built-in resource row is moving.                                                                                      |
| Claude setup                   | Marketplace and plugin fields plus admin and agent installation steps                                                                                                      | These stay core until the roadmap's harness-integration user facet exists.                                                              |
| Initializer orchestration      | Phase A and B lifecycle, identity, security, transport, status, failure handling, profile and rc ownership, and step order                                                 | This effort does not introduce an initializer extension point.                                                                          |

## Shape test

The real grouping is two manifest-only system plugins:

- `apt` carries the ten mutually dependent apt catalog rows.
- `install-command` carries the six independently selectable user install-command rows.

They are separate because they publish different resource kinds with different selection and
dependency contracts. They do not need capabilities or callbacks. Core already has the correct
generic consumers.

## R7 and C4 framework gaps exposed by the move

The move depends on generic resource semantics rather than installer-specific checks:

1. Disabled plugin manifests currently publish weakly and silently yield to an operator or built-in
   row under the same name.
2. `DisabledMark` is transient, and multiple disable sources collapse to the first reason.
3. Settings have no explicit per-resource disable list.
4. The graph retains binary enablement but not all disable causes or a sanctioned provider
   substitution.
5. Finalize does not universally reject an enabled resource that references a disabled resource.
6. Some inspect surfaces reconstruct a plugin disable reason from origin instead of reading stored
   provenance.

R7 and C4 close those gaps once for every resource kind. The relevant invariant is an enabled source
referencing a disabled target. A disabled source is inert, so disabled plugin cohorts may safely
refer to other disabled rows in the same cohort. Settings references retain their separate
presence-not-availability rule.

## R1 decisions

1. Move the five apt sources and five apt packages to `apt`.
2. Move the six user install commands to `install-command`.
3. Preserve all names, specs, dependencies, config fields, executors, ordering, defaults, and
   idempotency behavior.
4. Keep snap, mise, dotfiles, tmuxinator, Claude setup, and initializer orchestration in core.
5. Implement R7 and C4 as universal registry behavior, not as checks in apt or install-command
   execution.
6. Add no initializer capability, consumer gate, raw-field plugin gate, or default change.

No production move begins until this revised inventory and the corresponding architecture pass the
phased artifact review required by FRD R1.
