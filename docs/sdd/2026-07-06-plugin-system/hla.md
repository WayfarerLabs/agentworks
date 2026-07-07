# Plugin system: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Overview

The plugin system is deliberately not a new subsystem bolted onto the side. It is the generalization
of a pattern the resource-registry and resource-manifests SDDs already built: **capabilities are
code mirrored into the registry as descriptor rows; exposed resources are their named, configured,
manifest-declarable instantiations.** A plugin is a namespaced, separately-enabled bag of
capabilities and bundled resource manifests that publish into the same registry, through the same
publisher seam, as the app's own built-ins.

Three of the five capability kinds are new here (`vm-provider`, `harness-provider`,
`feature-provider`); the registry framework, kinds machinery, finalize pass, reference and
cycle-detection logic, origin taxonomy, and inspection surfaces all carry over unchanged. The
genuinely new runtime machinery is narrow: a plugin loader/enablement gate, a harness contract that
the session lifecycle calls, a feature contract that the vm/workspace/agent lifecycle calls, a
lineage-scoped dependency validator, and a namespaced state store.

```text
+-----------------------------+        +-------------------------------+
| config.toml                 |        | agentworks.resources          |
| [plugins] enable list       |        | (framework, UNCHANGED)        |
| [plugins.<ns>] plugin config |        |  - Registry publish/finalize  |
| [secret_config] chain, etc.  |        |  - kinds, miss policies        |
+--------------+--------------+         |  - references, cycles          |
               |                        +---------------^---------------+
               | enablement gate                        | publish (descriptor rows +
               v                                         | exposed resources)
+-----------------------------+   register  +-----------+-------------+
| agentworks.plugins          |------------>| capability registries   |
| (NEW: discover, enable,     |             | secret / git-cred /     |
|  namespace, load)           |             | vm / harness / feature  |
+--------------+--------------+              +-----------+-------------+
               | bundled manifests                      ^
               v                                         | exposed resources
+-----------------------------+   parse     +-----------+-------------+
| plugin-bundled + app-bundled|------------>| agentworks.manifests    |
| manifests (importlib.res.)  |             | (loader + publisher)    |
+-----------------------------+             +-------------------------+

lifecycle call-outs (NEW contracts, invoked by existing managers):
  sessions.manager  --> harness.start / restart / probe / place_assets
  vms/ws/agents init --> feature.apply(context)      [after core init]
  vms/ws/agents delete --> feature.remove(context)   [before core teardown]
  build/create path  --> dependency validator (lineage-scoped, refuse on miss)
  feature writes     --> namespaced state store (keyed by ns, kind, id)
```

## The unifying pattern in code

Every capability kind follows the same three-part shape already established for secrets:

1. **A code-side capability registry** (`dict[str, Capability]`): the raw capabilities, keyed by
   name. Built-ins registered by the app; plugin capabilities registered when the plugin is enabled.
2. **A descriptor kind** in the resources framework (`secret-provider`, `git-credential-provider`,
   `vm-provider`, `harness-provider`, `feature-provider`): read-only rows, error miss policy, not
   manifest-declarable, one row per registered capability, so references validate uniformly and the
   capabilities are visible in `agw resource list`.
3. **An exposed-resource kind** (`secret-backend`, `git-credential`, `vm-platform`, `harness`,
   `feature`): manifest-declarable, referencing its capability by name via a `provider`-style field,
   carrying provider-validated configuration. This is what templates and config reference.

The two existing instances (secret, git-credential) are the template; the three new instances reuse
the identical `manifest_declarable` / `builtin_override` kind flags and the same publish/finalize
lifecycle. Adding a capability kind is adding a registry, a descriptor kind, and an exposed-resource
kind -- no new dispatch machinery.

## Package layout

```text
cli/agentworks/plugins/               # NEW
  __init__.py         # public surface: discover, enabled_plugins, load
  registry.py         # plugin discovery + enablement gate + namespace ownership
  loader.py           # instantiate enabled plugins; collect their contributions
  contract.py         # the Plugin base/protocol: namespace, capabilities,
                      #   bundled-manifest roots, config schema, doctor checks
  state.py            # namespaced per-feature state store (read core, write namespaced)

cli/agentworks/harness/               # NEW capability family
  __init__.py         # HARNESS_REGISTRY + descriptor publisher
  contract.py         # Harness protocol: start / restart / probe / place_assets / spec schema
  shell.py            # built-in default 'shell' harness (owns command/restart_command today)

cli/agentworks/features/              # NEW capability family
  __init__.py         # FEATURE_REGISTRY + descriptor publisher
  contract.py         # Feature protocol: level, apply(ctx) / remove(ctx), declared deps, config schema
  context.py          # FeatureContext: config, resource+lineage, ExecTarget, state store, identities
  deps.py             # lineage-scoped dependency validation + same-level topo order

cli/agentworks/vms/
  providers/          # existing provisioners, reshaped behind a vm-provider capability registry
  provider_registry.py # NEW: VM_PROVIDER_REGISTRY + descriptor publisher; replaces VALID_PLATFORMS
                       #   hardcoding and the if/elif instantiation

cli/agentworks/resources/kinds/       # +vm_provider, +harness_provider, +feature_provider (descriptors)
                                      # +vm_platform, +harness, +feature (exposed resources)
cli/agentworks/secrets/providers.py   # unchanged (the pattern's origin)
cli/agentworks/bootstrap.py           # build_registry gains plugin publishers (see below)
```

Loaders and contracts are pure Python with no Typer dependency, consistent with the typer-isolation
rule.

## Plugin discovery, enablement, and namespacing

- **Discovery** finds installed plugins (mechanism deferred to LLD: Python entry points, a plugins
  directory, and/or source-refs; the choice must support pinning/verification for external plugins).
- **Enablement** is an explicit `config.toml` declaration. Only enabled plugins are instantiated and
  only their contributions participate. Installed-but-not-enabled plugins are inert.
- **Namespace ownership.** Each plugin declares a namespace. The plugin registry enforces that a
  plugin's exposed-resource names, capability names, and config keys are attributable to it, and
  that two enabled plugins cannot claim the same namespace. Namespacing is what makes the shared
  `(kind, name)` resource space safe across independently-authored plugins.
- **Origin stamping.** A plugin is classified `system-plugin` (app-shipped, separable) or
  `external-plugin` (outside source). Its capability descriptor rows and bundled exposed resources
  carry that origin.

## Trust boundary

There is no runtime confinement (FRD R2). Enabled capability code runs in-process with the CLI's
full authority. The architecture places the entire security weight on two gates:

- **Enablement**: a per-plugin, explicit, auditable config decision. The loader refuses to run a
  plugin that is installed but not enabled.
- **Distribution tier**: `system-plugin` vs `external-plugin` origin, where the external tier is the
  locus of provenance/pinning/verification and the README's supply-chain stance.

The least-privilege _convention_ (R2) is expressed in which SSH identity a feature's `ExecTarget`
defaults to (admin for VM-level, agent user for agent-level), not in any sandbox.

## Harness architecture

The session manager today builds an opaque command string from `session-template.command` /
`restart_command` and hands it to tmux (`sessions/manager.py`, `sessions/tmux.py`). The harness
capability inserts a typed contract at exactly that seam:

- `session-template.spec.harness` references a `harness` exposed resource (default: built-in
  `shell`). The resolved template carries the harness selection.
- At create/restart, the session manager calls the resolved harness rather than string-substituting
  a raw command: `harness.start(session_ctx)` / `harness.restart(session_ctx)` return the concrete
  launch command (and any pre-launch steps), and `harness.probe` backs liveness checks. The `shell`
  harness's `start`/`restart` simply reproduce today's `command` / `restart_command` substitution,
  so behavior is identical when no harness is declared.
- Tool asset placement (skills, rules, allow/deny) is a harness method invoked with an
  operator-declared asset set, so the core stops needing to know where `~/.claude/` or a Codex
  equivalent lives. Harness-shipped helper scripts, if needed, are staged from package data under a
  namespaced path and referenced by the harness's own command strings (kept out of first release
  unless a driver appears; inline shell suffices meanwhile).
- The env-composition and secret-injection path (`env/compose.py`, `secrets/orchestration.py`,
  backends-are-the-door) is unchanged: the harness shapes the _command_, not how env/secrets are
  resolved and delivered.

The default `shell` harness ships built-in (origin `built-in`); Claude Code and Codex ship as
plugins registering non-default harnesses. Core template fields specific to a tool
(`claude_marketplaces`, `claude_plugins`) migrate to the harness/plugin surface, shedding core
knowledge.

## Feature architecture

A feature is a single-level capability that the vm/workspace/agent managers call at lifecycle
boundaries. The contract is intentionally small:

- **Level** (`vm | workspace | agent`), intrinsic to the capability.
- **`apply(ctx)`** run on the create/reinit path, **after** core initialization of the resource.
- **`remove(ctx)`** run on the delete path, **before** core teardown, while the SSH target and state
  still exist.
- **Declared dependencies** on other features (R8).
- **Config schema** validating the feature exposed resource's spec.

`FeatureContext` carries: the feature's resolved configuration; the core resource row and its
lineage (the containing vm/workspace and, for agents, the agent identity); an `ExecTarget` for SSH
(defaulting to the level-appropriate identity per R2); the namespaced state store; and read-only
identity/user context (exact extent is an open question, FRD). It deliberately does **not** hand
over the whole `Config` object -- features depend on a stable context, not on core internals.

Activation is by template reference only: `<level>-template.spec.features` lists feature names. The
manager resolves the activated features for a resource, validates dependencies (below), orders
same-level features by topological sort, and runs each `apply`. Failure is loud-but-isolated: a
feature that raises is recorded on the resource and surfaced in `list`/`doctor`, retried next
reinit, without aborting sibling features or the parent operation -- except an unsatisfied
dependency, which is a pre-create hard refuse.

There is no session-level feature surface; the harness covers legitimate per-session needs (R7).

### Dependency validation

`features/deps.py` implements validation, not resolution:

- For a resource being created, gather its activated features and their declared dependencies.
- **Cross-level** dependencies (`agent-feature requires vm-feature`) are checked against the target
  lineage: is the required feature active on this agent's VM? The outer level has already
  initialized, so this is a presence check, needing no ordering. Unsatisfied → refuse the create,
  naming the missing feature and the hosting resource.
- **Same-level** dependencies determine `apply` order via topological sort; a cycle is a
  `ConfigError` reusing the registry's existing cycle detection.
- Instance-specific pairing (bind an agent client to a _particular_ broker instance) is expressed as
  a configuration reference on the dependent feature, resolved through the normal registry reference
  machinery, not as a blanket capability dependency.

## VM provider / platform architecture

Today: `VALID_PLATFORMS = ("lima", "azure", "wsl2", "proxmox")`, `if/elif` provisioner instantiation
in `vms/manager.py`, `[azure]` / `[proxmox]` config dataclasses in `config.py`, no registry, no
resource-Registry presence. The reshape:

- **`VM_PROVIDER_REGISTRY`** (`vms/provider_registry.py`) keys provisioner classes by name and
  publishes a `vm-provider` descriptor row per provider (origin `built-in` for the four existing).
  This replaces `VALID_PLATFORMS` and the hardcoded instantiation switch; selection and completions
  become registry-derived.
- **`vm-platform`** exposed resources are named, configured provider instantiations. The `[azure]`
  and `[proxmox]` config sections become `vm-platform` resources (Azure platform: subscription,
  resource group, region, idle timeout; Proxmox platform: api-url, node, token id, template,
  storage, bridge, pool); single-instance providers like `lima`/`wsl2` ship default `vm-platform`
  exposed resources. VM creation and vm-templates reference a `vm-platform` by name; the DB
  continues to record which platform provisioned a VM.
- With providers as a registry capability, a **plugin-shipped `vm-provider`** is possible on the
  same footing as the built-ins. Whether that ships in the first release or follows the reshape is a
  phasing call (FRD R9); the pattern lands here regardless.

This is the one capability kind whose reshape touches existing operator surface (the provisioner
config sections and `--platform`); its migration is a distinct concern for the plan, mirroring how
resource-manifests migrated secret backends.

## Origins

The origin variants `system-plugin` and `external-plugin` reserved by resource-manifests become
constructible: added to the `Origin.variant` literal with factory classmethods, rendered in
`agw doctor`, `agw resource list/describe`, `agw secret list/describe`, and accepted by the
`--origin` filter (extended from `operator | builtin | auto`). Field shapes are unchanged; plugin
rows carry the plugin's namespace as the source identifier (e.g. `system-plugin (claude-code)`).

## Registry integration (bootstrap)

`build_registry` gains the plugin publishers, slotted between the app built-ins and the operator
publishers so operator override policy applies uniformly (built-ins and plugins publish first,
operator documents last):

```python
def build_registry(config, manifests=None) -> Registry:
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)     # app-bundled resources
    catalog.publish_to(registry, config)       # built-in + operator catalog
    git_credentials.publish_to(registry)       # git-credential-provider descriptors
    secrets.publish_to(registry)               # secret-provider descriptors
    vm_providers.publish_to(registry)          # NEW: vm-provider descriptors
    harnesses.publish_to(registry)             # NEW: harness-provider descriptors + built-in shell
    features.publish_to(registry)              # NEW: feature-provider descriptors
    plugins.publish_to(registry, config)       # NEW: enabled plugins' capabilities + bundled resources
    config.publish_to(registry)                # operator TOML resource sections (dual-path)
    manifests.publish_to(registry)             # operator YAML documents
    registry.finalize()
    secrets.validate_chain(config, registry)
    # feature dependency validation runs per-resource at create/plan time, not here
    return registry
```

Cross-source collision handling (built-in vs plugin vs operator) reuses the existing per-kind
`builtin_override` policy at `Registry.add`. Feature dependency validation is a per-resource,
create-time concern (it needs the concrete lineage), not part of registry finalize.

## CLI surfaces

- `agw resource list/describe`, `agw secret list/describe`, `agw doctor`: display the new capability
  descriptor rows and exposed resources, plugin origins, and plugin-contributed doctor checks.
- `agw vm create --platform` and completions: registry-derived platform names instead of a hardcoded
  choice list.
- Plugin management commands (enable/disable/list plugins): shape deferred to LLD; no plugin-shipped
  top-level commands (FRD non-goal).
- Completions: kind vocabulary and platform lists extend the existing dynamic-completion plumbing;
  the completion tree extraction rule applies to any new commands.

## Design decisions

### Model B (explicit per-level opt-in + declared dependencies), not cascade

An earlier design considered cascading a multi-level feature's activation from its outermost
container to the contents (activate on the VM, agents inherit). It was rejected in favor of explicit
opt-in at every level plus declared dependencies. Rationale: the security story wants nothing
implicit -- an operator reading an agent-template must see exactly which features are active, which
is essential when a feature is a privilege broker. The cost (a feature can be half-activated) is
bought back by create-time dependency validation, which turns the incoherent state into a loud
refuse. This also makes richer compositions (an agent feature depending on two VM features)
expressible without a cascade special case.

### Features are single-level; multi-level facilities are multiple features

A capability is bound to one level because its code differs per level (a VM daemon vs an agent
client). The passport/broker plugins therefore ship two capabilities coupled only by a declared
dependency. This keeps the feature contract small (one level, one context type, one participation)
and makes the plugin a pure bag-of-capabilities with no internal coupling machinery.

### Dependencies are validated, not resolved

The framework checks satisfiability and orders same-level activation; it never auto-enables or
infers features. This keeps the dependency mechanism cheap (a presence check plus a topo sort,
reusing existing cycle detection) and avoids a resolver engine the use cases do not need. Instance
pairing, when needed, is an ordinary configuration reference.

### Harness is a capability, above the plugin layer

Harness sits in the same layer as secret/VM providers, not as a feature. It is a narrow, well-typed
contract (start/restart/probe/asset-placement) rather than an open lifecycle-hook surface, which is
the right shape for something as central as session launch. The first non-`shell` harness merely
happens to ship as a plugin.

### Distribution trust over sandboxing

Confining plugin code would fight a boundary that does not exist: the CLI already runs as admin over
every VM. The honest control is explicit enablement plus the external-plugin distribution tier,
which is exactly where supply-chain rigor belongs. This keeps the architecture simple and truthful
rather than offering the illusion of a sandbox.

### Namespaced writes over freehand DB mutation

Features read core state freely but write only to a namespaced store. Even trusted code scribbling
in the core `agents`/`vms` tables is a corruption vector, and a namespaced store is almost certainly
all a feature needs (the passport CA, per-resource issued material), with a trivial uninstall story
(`delete where namespace = ?`).

## Phasing (high level; detailed plan is a separate artifact)

- **Foundation:** plugin discovery + enablement + namespacing; origins `system-plugin` /
  `external-plugin` constructible; the three new capability kinds and their descriptor + exposed
  resources; plugin-published bundled manifests; namespaced plugin config + doctor.
- **Harness:** the harness contract at the session seam; the built-in `shell` harness reproducing
  today's behavior; Claude Code / Codex extracted from core into harness plugins.
- **VM platform reshape:** `vm-provider` registry + descriptors; `[azure]`/`[proxmox]` reshaped into
  `vm-platform` exposed resources; registry-derived `--platform`; migration of the provisioner
  config surface.
- **Features:** the feature contract at the vm/workspace/agent seams; the namespaced state store;
  dependency validation. Whether the first release includes dependency validation or ships
  single-level features first is the open scope question (FRD).
- **Cross-cutting drivers (passport, privileged broker):** built on the feature + dependency + state
  machinery once it is real, serving as the concrete validators of the contract.

## Open questions / for LLD

- **Feature context extent into the user/security model** (FRD open question): read-only identity
  context vs deeper participation. Bounds the `FeatureContext` shape.
- **Plugin installation/discovery mechanism and external-plugin pinning/verification**: entry points
  vs plugins directory vs source-refs; how enablement records a verified version.
- **Plugin config and instance-config shape**: `[plugins.<ns>]` namespaced TOML vs manifest;
  single-instance capability config vs configurable exposed resources allowing multiple instances
  (matters for harness/feature/platform that could reasonably have more than one configured form).
- **VM platform migration**: how `[azure]`/`[proxmox]` sections convert to `vm-platform` resources
  and how VMs already recorded against a bare platform string reconcile with named platforms.
- **First-release feature/dependency scope**: full dependency validation in v1 vs a stateless,
  single-level-feature first cut.
- **Harness asset staging**: whether harness-shipped helper scripts need a staged bin surface in the
  first release or inline shell suffices until a driver appears.
