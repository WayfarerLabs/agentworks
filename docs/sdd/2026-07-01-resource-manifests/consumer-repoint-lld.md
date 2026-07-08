# Phase 1 LLD: Config-to-Registry consumer repoint

Every resource read moves from `Config` attributes to Registry queries so Phase 2 can swap the
operator source without touching consumers. Pure refactor; behavior unchanged except the one
sanctioned relocation noted under "Eager resolution".

## Registry access surface

The Registry already exposes everything needed: `lookup(kind, name)`, `iter_kind(kind)`,
`iter_kind_items(kind)`. No typed per-kind views are added. A small accessor module
`agentworks/resources/access.py` provides the handful of shapes consumers actually want, so call
sites stay readable and the kind-string literals live in one place:

- `kind_dict(registry, kind) -> dict[str, Any]` (insertion-ordered copy; feeds the template
  resolvers)
- `admin_template(registry) -> AdminConfig` (the single `admin-template` row)
- `named_console_template(registry) -> NamedConsoleConfig`
- `git_credential(registry, name) -> GitCredentialConfig | None`
- `secret_decls(registry) -> dict[str, SecretDecl]`

## Repoint recipes (by consumer group)

| Consumer group                                                                                                                     | Today                                                                   | After                                                                                                                                                                                  |
| ---------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Template resolvers (`vms/templates.py:128`, `agents/templates.py:61`, `sessions/templates.py:67`, `workspaces/templates.py:38-71`) | `resolve_template(config, name)` reads `config.<kind>_templates`        | `resolve_template(registry, name)` reads `kind_dict(registry, "<kind>-template")`; `resolve_from_dict` internals unchanged (field merge + `_visiting` cycle guard stay)                |
| Managers / sessions / env-show / multi-console callers of the resolvers                                                            | pass `config`                                                           | pass a `registry` built once at the entry point (most entries already call `build_registry`; the rest gain one call)                                                                   |
| `config.admin` field readers (initializer `_init_admin`, vms/agents managers, orchestration, doctor, sessions)                     | `config.admin.<field>`                                                  | `admin_template(registry).<field>` (same object shape; Registry stores the published copy)                                                                                             |
| `config.named_console.tmux_layout` (multi_console x4)                                                                              | direct read                                                             | `named_console_template(registry).tmux_layout`                                                                                                                                         |
| `config.git_credentials` (doctor:108, initializer:1130, vms/manager:800,1358)                                                      | dict lookup                                                             | `git_credential(registry, name)` / `iter_kind_items("git-credential")`                                                                                                                 |
| `config.secrets` (doctor:348,356)                                                                                                  | dict read                                                               | `secret_decls(registry)`                                                                                                                                                               |
| Catalog consumers (`catalog.load_catalog` merge at catalog.py:288-291)                                                             | merges built-in dicts with `config.apt_*` / `config.*_install_commands` | read merged rows from the registry (`iter_kind_items("apt-package")` etc.); operator-over-built-in override already happens at publish, so the merge step collapses to a registry read |
| `Config.publish_to` (config.py:667-698) and `catalog.publish_to` (catalog.py:387-398)                                              | read Config attrs                                                       | UNCHANGED: this is the layer handoff and the only sanctioned reader                                                                                                                    |

## Eager resolution relocation

`load_config()` (config.py:1733-1740) eagerly resolves the default VM and agent templates into
`Config.vm` / `Config.agent`. Readers: `agents/manager.py` (982, 1037, 1269-1270, 1334, 1336, 1486),
`vms/initializer.py` (836 and siblings), `vms/provisioners/azure.py` (137-151).

Change:

- `Config.vm` / `Config.agent` fields are removed; `load_config` stops calling `resolve_from_dict`.
- Each reader's manager entry resolves once via the (repointed) lazy resolver
  (`resolve_template(registry, None)` for the default) and threads the resolved template down as a
  parameter, mirroring how per-VM template resolution already flows.
- Cycle-guard behavior is preserved twice over: the resolvers keep their `_visiting` guards, and
  `Registry.finalize` runs the canonical cycle pass at `build_registry` time. The observable
  difference is that config-only commands (`agw config edit`, ...) no longer fail on a broken
  template inheritance chain at load; every resource-touching command still does, at
  `build_registry`. This is the sanctioned relocation from the plan.

## Registry threading

There is no shared CLI bootstrap context; commands call `load_config()` and managers call
`build_registry(config)` locally (vms/manager.py 223-1708, sessions/manager.py 1131-1148,
workspaces/manager.py 41-436, agents/manager.py 230-458, cli/commands/{secret,resource}.py). The
repoint keeps that shape: each manager entry that needs resources builds the registry once at entry
and passes it down; helpers take `registry` (or a resolved template) instead of `config`. `config`
keeps flowing in parallel for config-only reads (`operator`, `paths`, `defaults`, platform sections,
`session.config`, `secret_config`). No new context object; that would be scope creep with no Phase 2
payoff.

## Guard test

`tests/test_config_resource_read_guard.py`: source-level scan asserting no module outside
`agentworks/config.py` and `agentworks/catalog.py` (the publishers) reads the retired Config
resource attributes (`.secrets`, `.vm_templates`, `.agent_templates`, `.workspace_templates`,
`.session_templates`, `.git_credentials`, `.admin`, `.named_console`, `.vm`, `.agent`,
catalog-extension dicts) off a `Config`-typed object. Implementation: grep-level scan for
`config.<attr>` / `cfg.<attr>` patterns with an allowlist for the publisher files, same spirit as
the Phase 0 vocabulary guard.

## Execution order (each slice ends green)

1. `resources/access.py` + resolver signature change + their direct callers.
2. Admin/named-console readers (initializer, vms/agents managers, sessions, orchestration).
3. `config.vm` / `config.agent` removal + default-template threading.
4. Git-credential, secret, doctor, env-show readers.
5. Catalog consumer reads from registry; `load_catalog` merge collapse.
6. Guard test + full suite + `Config` docstring updates.
