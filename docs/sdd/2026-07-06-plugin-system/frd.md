# Plugin system: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

The original agentworks SDD (`docs/sdd/2026-03-05-agentworks`) reserved "VM initialization plugins"
as deferred future work. Two subsequent SDDs built the foundation that makes a plugin system
tractable rather than speculative:

- **resource-registry** (`docs/sdd/2026-06-17-resource-registry`, locked) made resource handling
  publisher-agnostic: a parsing layer composes Resources and publishes them into a `Registry` that
  finalizes and freezes them, independent of where they came from.
- **resource-manifests** (`docs/sdd/2026-07-01-resource-manifests`) moved operator-declared
  resources into YAML manifests, formalized the **capability / exposed-resource** split for secrets
  and git credentials, established the **origin taxonomy** (and explicitly reserved the
  `system-plugin` and `external-plugin` origin variants for this SDD), and shipped the
  **built-in-manifest** mechanism (app-bundled resources published through the same loader).

This SDD builds the plugin system on that foundation. The motivations:

1. **Move harness-specific logic out of the core.** The core has accumulated baked-in knowledge of
   specific agent harnesses (Claude Code, Codex): install commands, template fields
   (`claude_marketplaces`, `claude_plugins`), and conventions. We want an excellent experience for
   these and other harnesses without polluting the runtime-neutral core. Purely optional, opt-in
   plugins are the right home for harness-specific behavior, resources, and config schema.

2. **Let capabilities participate in the object lifecycle.** Some extensions are not just static
   resources; they need to _do_ something when VMs, workspaces, or agents are created. An `az-cli`
   plugin should be able to run `az login` with a service-principal credential at agent init so the
   agent can just use the CLI. A cryptographic-passport plugin should be able to publish validator
   keys on each VM and place a signed passport in each agent's home directory. A privileged-broker
   plugin should be able to run a daemon on the VM that agents call through a constrained API. These
   span levels and have dependencies between them.

3. **Bring your own resources and capabilities.** With resources as data and capabilities as a
   uniform code contract, plugins can ship both: templates, catalog entries, secret backends,
   harnesses, VM platforms, and lifecycle features.

### Two motivating plugins, deliberately different in shape

- **A harness plugin (Claude Code / Codex): pure resources plus a session capability.** Ships
  templates, catalog entries (install commands), config schema, and a _harness_ that knows how to
  start/restart its named session and where to place skills/rules/allow/deny. It does not
  participate in vm/workspace/agent lifecycle.
- **A cross-cutting plugin (passport, privileged broker): resources plus lifecycle features across
  levels.** Ships one capability that runs at the VM level and another at the agent level, with the
  agent-level capability declaring a dependency on the VM-level one. It maintains workstation-local
  state (a CA chain), participates in agent and VM initialization, and may ship a skill telling the
  agent about the facility.

These two shapes bound the design: the harness plugin must be trivial to build, and the
cross-cutting plugin must be _possible_ to build without the core knowing anything about it.

### Scope

In scope: the plugin as a trust and distribution unit (install, enable, namespace); the generalized
capability / exposed-resource pattern extended to harnesses, features, and VM platforms; plugin
publication of resources and bundled manifests under the reserved plugin origins; namespaced plugin
config and doctor contributions; the **harness** capability (session lifecycle); the **feature**
capability (vm/workspace/agent lifecycle participation) with declared inter-feature dependencies;
the **vm-provider / vm-platform** reshape of today's hardcoded provisioners into the pattern; and a
namespaced per-feature state store.

Out of scope: sandboxing or privilege confinement of plugin code (see R2 for why the boundary is
distribution trust, not runtime confinement); session-level features (the harness owns session
lifecycle); namespaced CLI commands from plugins; and a general inter-feature dependency
_resolution_ engine beyond declared-dependency _validation_.

## Terminology

- **Plugin**: a distributable, independently installable and enable-able unit that ships some mix of
  capabilities and resources under a single namespace. The plugin is the trust and distribution
  boundary; it is not itself an activation or coupling unit.
- **Capability**: a unit of code the app or a plugin provides, exposed to the framework through a
  uniform contract and mirrored into the registry as a read-only descriptor row. Capabilities are
  not manifest-declarable. Secret providers, git-credential providers, harnesses, features, and VM
  providers are all capabilities.
- **Exposed resource**: a named, configured instantiation of a capability. A resource, hence
  manifest-declarable, referencing its capability by name. `secret-backend`, `git-credential`,
  `harness`, `feature`, and `vm-platform` are exposed-resource kinds.
- **Harness**: a session-level capability that owns how a named session of a particular tool is
  started, restarted, and probed, and where that tool's assets (skills, rules, allow/deny lists) are
  placed. The default `shell` harness reproduces today's generic command/restart behavior.
- **Feature**: a single-level capability (vm, workspace, or agent) that participates in that level's
  create/delete lifecycle. Activated by explicit opt-in on that level's template. Coupled to other
  features only through declared dependencies.
- **Activation**: an exposed resource being referenced by a template (or config), which is what
  causes the capability to participate. Installing or enabling a plugin never activates anything on
  its own.
- **Dependency**: a declared, one-directional requirement from one feature to another
  (`agent-feature-X requires vm-feature-Y`), validated (lineage-scoped) at resource create time.
- **Origin tiers**: `built-in` (shipped with the app, inseparable), `system-plugin` (shipped with
  the app but a separable, independently enable-able plugin), `external-plugin` (installed from
  outside sources), plus the existing `operator-declared` and `auto-declared` variants. The two
  plugin variants were reserved by the resource-manifests SDD and become constructible here.

## Requirements

### R1: The plugin as a trust and distribution unit

- A plugin is **installed** (present in the environment) and separately **enabled** (the operator
  has explicitly declared, in `config.toml`, that this plugin is trusted and its capabilities and
  resources should participate). Installed-but-not-enabled plugins contribute nothing: no resources,
  no capabilities, no config schema, no doctor rows, nothing on the CLI surface. Enablement is the
  trust decision.
- A plugin declares a **namespace**. Everything it ships (exposed-resource names, capability names,
  config keys, state) lives under that namespace, so two plugins cannot silently collide and an
  operator can always attribute a resource or a config key to the plugin that owns it.
- Enabling and disabling a plugin are operator actions with clear, auditable config-level
  representation. Disabling a plugin cleanly removes its contributions on the next invocation
  (subject to the state-cleanup semantics of R10).

### R2: Trust model is distribution trust, not runtime confinement

- An enabled plugin's capability code runs **with the same privileges as the agentworks CLI
  itself**: full read access to the database, registry, and config; local filesystem access; and the
  ability to run SSH commands against VMs as the admin user or (for agent-level participation) the
  agent user. This is not an escalation: the CLI already holds admin SSH to every VM and full local
  access. A plugin the operator chose to enable is no more privileged than the tool hosting it.
- Consequently, **the plugin system does not attempt to sandbox or confine plugin code.** The
  security boundary that matters is _distribution trust plus explicit enablement_, expressed through
  the origin tiers: `system-plugin` code is trusted like the app; `external-plugin` code is where
  provenance, pinning, and verification matter, and where the supply-chain caution the project
  README already states applies with full force.
- Documentation must state plainly that enabling a plugin that ships capability code is equivalent
  to running arbitrary admin-capable code, not merely adding configuration. Enablement is a
  deliberate, per-plugin gate, never implied by installation.
- **Least-privilege convention (not enforcement):** VM-level capabilities default to acting as the
  admin user; agent-level capabilities default to acting as the agent user. Either may use the other
  identity when it genuinely needs to, but the default nudges the safer choice. Because capability
  code runs workstation-side and reaches VMs over SSH, "act as admin" never grants the on-VM agent
  user any additional privilege; the VM-side privilege separation is unaffected.

### R3: The unifying capability / exposed-resource pattern

The pattern the resource-manifests SDD introduced for secrets and git credentials becomes the single
shape for every code-backed extension point. A **capability** is code (a registry descriptor row,
not manifest-declarable); an **exposed resource** is its named, configured instantiation
(manifest-declarable, referencing the capability by name). Five instances exist after this SDD:

| Capability (code, descriptor row, not declarable) | Exposed resource (configured, manifest-declarable) | Referenced by                    |
| ------------------------------------------------- | -------------------------------------------------- | -------------------------------- |
| `secret-provider`                                 | `secret-backend`                                   | the chain in `[secret_config]`   |
| `git-credential-provider`                         | `git-credential`                                   | admin / agent templates          |
| `vm-provider`                                     | `vm-platform`                                      | vm-templates / `vm create`       |
| `harness-provider`                                | `harness`                                          | `session-template.spec.harness`  |
| `feature-provider`                                | `feature`                                          | `<level>-template.spec.features` |

- The top two exist today (resource-manifests). This SDD adds the bottom three.
- Templates and config always reference the **exposed resource**, never the raw capability, exactly
  as the secret chain references backend names rather than provider names. Per-instance
  configuration lives on the exposed resource.
- A capability may ship one or more **default exposed resources** as app-bundled or plugin-bundled
  manifests, for the common case where an instance only makes sense once (the built-in `env-var` and
  `prompt` backends are the existing precedent). Default-exposed-resource names follow the per-kind
  `builtin_override` policy: reserved (operator cannot redeclare) or overridable (operator manifest
  of the same name replaces it), decided per kind.
- Capabilities provided by a plugin are mirrored into the registry as descriptor rows with the
  plugin origin (`system-plugin` or `external-plugin`); their exposed resources carry the same
  origin unless operator-declared.

### R4: Plugins publish resources and bundled manifests

- A plugin may ship any manifest-declarable resource kind (templates, catalog entries, secret
  backends, git credentials, harnesses, features, VM platforms) as app-bundled/plugin-bundled
  manifests, published through the same loader the app's own built-in manifests use.
- Plugin-published resources land in the registry with origin `system-plugin` or `external-plugin`.
  These variants become constructible here; the taxonomy, display strings, and `--origin` filter
  vocabulary were already reserved by resource-manifests and are extended, not redefined.
- Operator override of a plugin-published resource follows the same per-kind `builtin_override`
  policy already defined (catalog-like kinds overridable by name; reserved kinds a load error).
- Plugin resources compete in the same `(kind, name)` space as operator and built-in resources;
  namespacing (R1) keeps plugin-shipped names attributable and collision-free between plugins.

### R5: Namespaced plugin config and doctor

- A plugin may declare a **config schema** for its own namespaced settings, validated by the
  plugin's own code (the core does not know the plugin's fields). Operator-supplied plugin config
  lives under the plugin's namespace in `config.toml`.
- A plugin may contribute **doctor logic**: checks over its own config and resources that surface in
  `agw doctor` alongside core checks, attributed to the plugin.
- Config and doctor contributions appear only when the plugin is enabled.

### R6: Harness capability (session lifecycle)

- `harness-provider` is a session-level capability. A `harness` exposed resource is its configured
  instance, referenced by `session-template.spec.harness` (default: the built-in `shell` harness).
- The harness owns the behavior currently expressed as opaque `command` / `restart_command` strings
  on the session template:
  - starting and restarting its named session (e.g. a fresh launch vs a resume), and probing
    liveness;
  - the directory conventions and asset placement for its tool (skills, rules, allow/deny lists),
    given an operator-declared set of such assets;
  - any harness-specific spec fields it chooses to accept, validated by the harness, not the core.
- The **default `shell` harness** ships built-in and reproduces today's behavior exactly: it owns
  the `command` / `restart_command` / `required_commands` fields as its own spec. Every existing
  session template keeps working unchanged, because "no harness declared" resolves to `shell`.
- Claude Code and Codex ship as plugins that register a harness plus templates and catalog entries.
  Harness-specific fields currently baked into core templates (`claude_marketplaces`,
  `claude_plugins`) move into the relevant harness/plugin surface; the core sheds this knowledge.
- Harness is a **core capability that plugins can implement**, not itself a plugin feature: it sits
  in the same layer as secret and VM providers. The first non-`shell` harness happens to ship as a
  plugin.

### R7: Feature capability (vm/workspace/agent lifecycle)

- `feature-provider` is a capability bound to exactly **one level**: vm, workspace, or agent. A
  `feature` exposed resource is its configured instance. Its level is intrinsic to the capability
  (the code that runs a VM daemon is a different capability from the code that wires an agent as
  that daemon's client).
- A feature is **activated by explicit opt-in** on that level's template:
  `vm-template.spec.features`, `workspace-template.spec.features`, `agent-template.spec.features`
  each list feature (exposed-resource) names. Installing or enabling the plugin does not activate a
  feature; only a template reference does. There is no implicit cascade from a container to its
  contents.
- On the create path for its level, after the core initialization of that resource, each activated
  feature runs its participation with a **context** providing: its own configuration; the relevant
  core resource data (the vm/workspace/agent row and its lineage); a way to run SSH commands (as
  admin or the agent user per R2); and its namespaced state store (R10). On the delete path,
  features participate **before** core teardown, while the resource and its SSH target still exist.
- Feature failure is **loud but isolated**: a failing feature does not abort unrelated features or
  the parent operation silently; the failure is surfaced (persisted on the resource, shown in `list`
  / `doctor`) and retried on the next reinitialization. (Exception: an unsatisfied declared
  dependency is a hard refuse at create time; see R8.)
- Multiple features may be activated on one resource; their run order within a level is
  deterministic and respects same-level dependencies (R8).
- **No session-level features.** Session lifecycle is owned by the harness (R6). A general
  plugin-writable session-init surface is disallowed because a session's only resource is its own
  process, making it too easy to persist state into the shared workspace or agent user and affect
  other sessions. Legitimate per-session-start needs are met by the harness capability, which is
  bounded to its own tool.

### R8: Feature dependencies

- A feature may declare **dependencies** on other features by name (typically capability name):
  `agent-feature-X requires vm-feature-Y`, expressing that X's participation assumes Y is active on
  the same resource lineage.
- Dependencies are **lineage-scoped**: "required on _this agent's_ VM," not "somewhere in the
  fleet."
- Dependencies are validated at **resource create/plan time**. An activation whose dependencies are
  not satisfiable on the target lineage is **refused loudly** before the resource is created, naming
  the missing feature and the resource that would host it. Half-activated cross-level facilities (an
  agent reaching for a daemon that was never started) are thereby unrepresentable.
- **Cross-level dependencies need no ordering**: the outer level always initializes before the
  levels it contains (a VM is provisioned before its agents), so a required VM feature has already
  run by the time an agent feature is validated. **Same-level dependencies** are permitted and
  determine activation order within the level by topological sort; a dependency cycle is a config
  error (reusing the registry's existing cycle detection).
- The dependency mechanism is **validation, not resolution**: the framework checks satisfiability
  and orders same-level activation; it does not auto-enable, auto-install, or infer missing
  features. If a specific configured instance must be paired (an agent client bound to a particular
  broker instance rather than any instance of its capability), that pairing is expressed as a
  reference in the dependent feature's configuration, not as a blanket dependency.

### R9: VM providers and platforms

Today's provisioners (`azure`, `lima`, `proxmox`, `wsl2`) are hardcoded: a `VALID_PLATFORMS` tuple,
`if/elif` instantiation in the VM manager, `[azure]` / `[proxmox]` config sections, no registry, and
no presence in the resources Registry. The resource-manifests SDD deliberately left provisioners as
config, noting the plugin SDD may revisit. This SDD revisits them by fitting them to the pattern:

- `vm-provider` becomes a capability: the provisioner code, registered in a provider registry and
  mirrored into the resources Registry as a read-only descriptor row (like `secret-provider`),
  reporting origin `built-in` for the four current provisioners.
- `vm-platform` becomes an exposed resource: a named, configured instantiation of a provider (e.g.
  an Azure platform carrying subscription/region/resource-group; a Proxmox platform carrying
  api-url/node/token). VM creation and vm-templates reference a `vm-platform` by name. The `[azure]`
  and `[proxmox]` config sections are reshaped into `vm-platform` exposed resources; the built-in
  single-instance platforms (e.g. `lima`) ship as bundled default exposed resources.
- Once providers are a registry capability rather than a hardcoded tuple, a plugin can ship a new
  `vm-provider` on the same footing as the built-in four. Whether plugin-shipped providers ship in
  the first release or follow the reshape of the existing four is a phasing decision for the plan;
  the pattern is established here regardless.
- The `--platform` selection surface and completions are updated to be registry-derived rather than
  a hardcoded choice list.

### R10: Namespaced per-feature state store

- A plugin (specifically its features) may **read** the database, registry, and config freely, but
  **writes** are directed to a **namespaced state store** rather than freehand mutation of core
  tables. The store is keyed by (plugin/feature namespace, resource kind, resource id) with an
  opaque value the plugin owns and evolves; the framework does not migrate plugin data.
- The state store is what lets a cross-cutting feature persist workstation-local state (the passport
  CA chain, per-resource issued material) across invocations.
- Disabling or uninstalling a plugin can drop its namespaced state wholesale
  (`delete where namespace = ?`), independent of core tables.

## Personas and user stories

- **Operator (platform owner).** Wants to enable a Claude Code plugin so agents get a great Claude
  experience without the core carrying Claude-specific code; wants to see, in `list`/`doctor`,
  exactly which resources and capabilities came from which plugin; wants enabling an external plugin
  to be a conscious, auditable trust decision.
- **Operator activating a facility.** Wants to turn on cryptographic passports for a set of agents
  by referencing the passport features on the relevant vm-template and agent-template, and be told
  immediately (at create) if they activated the agent half without the VM half.
- **Plugin author (harness).** Wants to ship a harness plus templates and catalog entries as a
  single namespaced unit, implementing a small, well-typed contract
  (start/restart/probe/asset-placement) rather than a sprawling set of lifecycle hooks.
- **Plugin author (cross-cutting).** Wants to ship a VM-level feature and an agent-level feature
  with a declared dependency between them, full access to run the SSH orchestration they need, and a
  namespaced place to persist workstation-local state.

## Non-goals

- **Sandboxing / privilege confinement of plugin code.** The boundary is distribution trust plus
  explicit enablement (R2); confinement would fight a trust boundary that does not exist for a
  self-hosted operator tool.
- **Session-level features / a general plugin-writable session-init surface.** Owned by the harness,
  bounded to its tool (R7).
- **Namespaced CLI commands from plugins.** Deliberately excluded: an operator should be able to
  reason about the `agw` surface without knowing which plugins are installed. Revisit only with a
  concrete need.
- **A general dependency-resolution engine.** Dependencies are declared and validated, not resolved,
  auto-enabled, or inferred (R8).
- **New VM provisioners as the headline deliverable.** R9 establishes the pattern and reshapes the
  existing four; shipping a plugin-authored provisioner is enabled by the pattern but its inclusion
  in the first release is a phasing question.
- **Changing the core object model.** VMs, workspaces, agents, sessions remain DB-managed with the
  existing containment hierarchy; plugins participate in lifecycle, they do not add new core object
  types.

## Open questions

- **Feature context and the user/security model.** The privileged-broker example implies a feature's
  context must include identities (the agent user, the admin user, per-agent policy), not just
  resolved secrets and an SSH target. How far into the user/security layer a feature is allowed to
  reach (read-only identity context vs the ability to manipulate the user model) is a real trust
  boundary to decide, even for enabled code.
- **First-release feature scope.** Whether the first release includes the full feature +
  dependency-validation machinery (making passport/broker buildable) or ships plugins + harness +
  vm-platform + single-level features _without_ cross-feature dependencies first, adding the
  dependency graph once a concrete driver exists.
- **Plugin config location and shape.** A `[plugins]` enable list plus `[plugins.<name>]` namespaced
  config in `config.toml` (consistent with config-is-config) vs a dedicated manifest or plugin-owned
  config file; and whether feature/harness/platform instance config is single-instance (namespaced
  TOML) or a configurable exposed resource permitting multiple instances.
- **Capability and kind naming.** `feature-provider` / `feature`, `harness-provider` / `harness`,
  `vm-provider` / `vm-platform` as the kind identifiers, vs alternatives; confirmed against the
  lower-kebab kind vocabulary the resource-manifests SDD established.
- **Plugin installation and discovery mechanism.** How an external plugin is delivered and located
  (Python entry points, source-refs, a plugins directory) and how enablement pins/verifies an
  external plugin. Distribution mechanics are a design item for the HLA/LLD; the trust _model_ (R2)
  is fixed here.
