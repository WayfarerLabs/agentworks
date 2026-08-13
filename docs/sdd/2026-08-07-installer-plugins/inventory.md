# Declared Installer Resource Inventory

- Status: Revised for the bucket-only scope
- Date: 2026-08-13
- Snapshot: `main` at `c7093147`

## Inventory rule

A row moves when Agentworks ships it as an optional, named installer resource selected through an
existing resource reference. This effort changes declaration ownership only. Core keeps the generic
models, loaders, validation, dependency extraction, and executors that also serve operator-declared
resources.

## `apt` plugin

| Kind          | Name             | Current declaration         | Existing relationship                  |
| ------------- | ---------------- | --------------------------- | -------------------------------------- |
| `apt-source`  | `github-cli`     | `builtin/apt-sources.yaml`  | Required by `apt-package/gh`           |
| `apt-source`  | `hashicorp`      | `builtin/apt-sources.yaml`  | Required by `apt-package/terraform`    |
| `apt-source`  | `nodesource-v22` | `builtin/apt-sources.yaml`  | Required by `apt-package/nodejs`       |
| `apt-source`  | `ngrok-agent`    | `builtin/apt-sources.yaml`  | Required by `apt-package/ngrok`        |
| `apt-source`  | `tofuutils-tenv` | `builtin/apt-sources.yaml`  | Required by `apt-package/tenv`         |
| `apt-package` | `gh`             | `builtin/apt-packages.yaml` | References `apt-source/github-cli`     |
| `apt-package` | `terraform`      | `builtin/apt-packages.yaml` | References `apt-source/hashicorp`      |
| `apt-package` | `nodejs`         | `builtin/apt-packages.yaml` | References `apt-source/nodesource-v22` |
| `apt-package` | `ngrok`          | `builtin/apt-packages.yaml` | References `apt-source/ngrok-agent`    |
| `apt-package` | `tenv`           | `builtin/apt-packages.yaml` | References `apt-source/tofuutils-tenv` |

All ten rows move unchanged into the `apt` plugin's manifest package. VM templates continue to
select the package rows through `apt_packages`; sources continue to resolve through the package
dependency edges.

## `install-command` plugin

| Kind                   | Name        | Current declaration             | Existing installed check |
| ---------------------- | ----------- | ------------------------------- | ------------------------ |
| `user-install-command` | `oh-my-zsh` | `builtin/install-commands.yaml` | `test_dir`               |
| `user-install-command` | `bun`       | `builtin/install-commands.yaml` | `test_exec`              |
| `user-install-command` | `fnm`       | `builtin/install-commands.yaml` | `test_exec`              |
| `user-install-command` | `nvm`       | `builtin/install-commands.yaml` | `test_file`              |
| `user-install-command` | `starship`  | `builtin/install-commands.yaml` | `test_exec`              |
| `user-install-command` | `uv`        | `builtin/install-commands.yaml` | `test_exec`              |

All six rows move unchanged into the `install-command` plugin's manifest package. Admin and agent
templates continue to select them through `install_commands`.

## Core ownership retained

| Surface                   | Retained core ownership                                                                     |
| ------------------------- | ------------------------------------------------------------------------------------------- |
| Apt framework             | Kinds, decoding, source dependencies, VM selection, source reconciliation, and installation |
| Install-command framework | Kinds, predicates, references, system/admin/agent runners, PATH results, and ordering       |
| Snap                      | VM-template configuration and installer                                                     |
| Mise                      | Admin/agent configuration, installation, activation, lockfiles, pruning, and ordering       |
| Dotfiles                  | Admin/agent configuration, synchronization, and checkout-local installation                 |
| Tmuxinator                | Platform installation, workspace default, project generation, and links                     |
| Claude setup              | Marketplace/plugin fields and admin/agent installation steps                                |
| Initializer orchestration | Lifecycle, identity, security, transport, status, failure handling, and step order          |

## Shape decision

The two plugins publish manifests and guide topics only. They have no capabilities or callbacks.
They are separate because they publish different resource kinds with different selection and
dependency contracts. Core already provides the shared consumers, so moving execution would add an
extension point without a need.
