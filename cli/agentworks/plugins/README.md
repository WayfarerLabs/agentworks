# System plugins

A **system plugin** bundles capability implementations (and, optionally, resource manifests) that
ship with agentworks but are separable and opt-in. A plugin is not a resource kind and never
publishes a new kind: it contributes implementations of the four existing capability kinds
(`vm-platform`, `harness`, `git-credential-provider`, `secret-backend`) and, optionally, declarable
resources bundled as YAML manifests. A plugin is an **origin** (`system-plugin`), the fourth
alongside `operator-declared`, `built-in`, and `auto-declared`.

This document is for authoring a system plugin. For the operator-facing model (how origins and the
enablement axis read on the surfaces) see `docs/guides/resources.md`; for the decision record see
`docs/adrs/0021-system-plugins.md`.

## The `Plugin` descriptor

A plugin is a single frozen `Plugin` descriptor (`plugins/base.py`), authored as a module-level
`PLUGIN` constant:

```python
from agentworks.plugins import Plugin

PLUGIN = Plugin(
    name="example-cloud",  # a fictitious plugin, for illustration
    description="Example Cloud VM platform",
    capabilities={"vm-platform": (ExampleCloudPlatform,)},
    manifests="agentworks.plugins.example_cloud",  # optional; an importlib-resources package anchor
)
```

Fields:

- **`name`** (required): the plugin's identity. Non-empty and `/`-free. It is the name an operator
  writes in `[plugins].system` and the name the surfaces attribute rows to
  (`from plugin example-cloud`).
- **`description`**: shown in the doctor roster.
- **`capabilities`**: a mapping keyed by capability kind, each value a tuple of impl **classes**,
  uniformly, even for `secret-backend` (whose registry holds instances; the class-vs-instance
  reconciliation is the framework's job, not yours). Every impl exposes `name` and `description` as
  class attributes; the impl's `name` is the registry key its published row carries.
- **`manifests`** (optional): an importlib-resources package anchor whose `manifests/` subdirectory
  holds the plugin's bundled YAML resource manifests (the same envelope operators write; see
  `docs/guides/resources.md`). `None` when the plugin ships no manifests.
- **`required_scopes`** and **`commands`**: reserved, inert placeholders (see below).

The descriptor depends on nothing in the capability or registry machinery, so it is constructible in
a test without a registry. It becomes valid or rejected only when the installed index registers it.

## Shipping a plugin

The installed index (`plugins/__init__.py`) uses **inverted registration**: it imports each shipped
plugin module and calls `register_plugin(module.PLUGIN)` itself, rather than registration being an
import side effect. To ship a plugin, add its module to `_INSTALLED_MODULES`:

```python
_INSTALLED_MODULES: tuple[_PluginModule, ...] = (
    agentworks_azure,
)
```

The index ships the migrated system plugins (`onepassword`, `claude`, `proxmox`, `azure`); each
listed module is registered and seated at import. On import the index:

- registers each plugin, wrapping any failure with the real module name (a bad descriptor is a
  curation bug that reads as `system plugin '<module>' failed to register: ...`, not an opaque
  traceback that kills the CLI), and
- rejects a duplicate plugin name as a typed error.

`register_plugin` validates the **whole** descriptor first (name shape; every capability kind has an
adapter; every impl is a class with a non-empty, `/`-free `name`; no intra-descriptor name
collisions), then seats every impl **atomically**: no capability registry is touched until every
impl across the descriptor is known seatable, so a mid-descriptor failure can never leave orphaned
impls behind. Registration is idempotent per impl name; a cross-plugin or plugin-versus-built-in
name clash on the same capability is a typed error naming the occupant's real origin.

## The enablement model: opt-in, present-but-disabled

A plugin is **off by default**. An operator opts in by listing the plugin name in `config.toml`:

```toml
[plugins]
system = ["azure"]
```

Enablement is a first-class axis, distinct from readiness (whether a capability can run on this
host). What "opted in" versus "not opted in" changes:

- **Capability rows publish unconditionally**, for every shipped plugin, opted in or not. A
  not-opted-in plugin's rows are **present-but-disabled**: the row exists, but the enablement axis
  marks it `disabled`. A reference to a disabled row (an operator `vm-site` naming a not-enabled
  plugin's platform) is **not-ready with the remediation "enable plugin `<name>`"**, never an
  unknown-name hard error. Publishing rows unconditionally is what makes that friendly hint
  possible: an absent row could only produce an unknown-name error.
- **Bundled manifests publish unconditionally too, at parity with capabilities.** A not-opted-in
  plugin's bundled declarable resources (install-commands, apt entries, templates) are also
  **present-but-disabled**: the row exists but the enablement axis marks it `disabled`. A reference
  to one (an agent-template's `user_install_commands` naming a not-enabled plugin's install-command,
  a `vm-template` `inherits` naming a not-enabled plugin's template) is **refused at use with the
  remediation "enable plugin `<name>`"**, never an unknown-name error and never a silent use. To
  keep a disabled plugin's row from ever blocking an operator's (or a built-in's, or an enabled
  plugin's) resource of the same name, a not-opted-in plugin's manifest rows publish **weak**
  (add-if-absent, silently replaced by any stronger row, never a collision error) in any publish
  order; enabling the plugin republishes them strong. A plugin may only bundle declarable kinds
  whose consumption sites are gated, and may not bundle a kind's reserved auto-declared name (such
  as a `default` template), so the opt-in guarantee holds by construction (see ADR 0021).

A `[plugins].system` entry that is not an installed plugin (a typo, or an uninstalled plugin) is a
config error raised up front, before anything publishes. Unknown keys in the `[plugins]` table are
**also** a hard config error, not a soft warning: because `[plugins]` is an opt-in gate, a typo'd
key must fail loudly rather than silently leave plugins un-enabled. (This is a deliberate departure
from the soft warn-on-unknown-key convention other config sections use; see
`docs/adrs/0021-system-plugins.md`.)

## The four capability kinds and manifests

A plugin contributes implementations of existing capability kinds only:

- **`vm-platform`**: a backend that runs VMs (paired with a `vm-site` resource's config).
- **`harness`**: what a session runs (paired with a `session-template`'s config).
- **`git-credential-provider`**: how a git token is obtained and served.
- **`secret-backend`**: where a secret's value is read from.

Each impl subclasses the kind's capability base class and exposes `name` / `description` class
attributes. The published row is read-only and lists, describes, and is referenced like any other
resource of that kind.

**Bundled manifests** are ordinary YAML resource documents the plugin ships, under the `manifests/`
subdirectory of the package `manifests` points at. Only declarable kinds whose consumption sites are
gated may be bundled (install-commands, apt entries/packages, and the template kinds); kinds whose
reference paths are not gated (such as `secret` or `vm-site`) are rejected at publish, as is a
reserved auto-declared name (a `default` template). They load through the same typed, validated
loader the built-in manifests use, stamped with the plugin's `system-plugin` origin. A malformed
bundle is a typed error attributed to the plugin, never a bare import or assertion failure.

## Reserved fields (inert in v1)

Two descriptor fields are reserved for a later effort and carry no behavior today:

- **`required_scopes`** (`tuple[ScopeLevel, ...]`): a least-privilege declaration. When populated it
  renders as an informational `least privilege: <levels>` line in `agw doctor`, but nothing enforces
  it. Empty (the default) renders nothing.
- **`commands`** (`tuple[PluginCommand, ...]`): a typed placeholder for a plugin-owned CLI command.
  Nothing constructs or dispatches one in v1.

Both are real typed shapes a future effort populates, not untyped holes, but no code reads them for
behavior today.

## Surfaces

- `agw resource list` hides disabled rows by default and shows them with `--include-disabled`; a
  not-ready-but-enabled row still lists (disabled hides, not-ready shows). A plugin row is annotated
  `from plugin <name>`.
- `agw resource describe <kind>/<name>` renders a named row even when disabled, with a `Disabled:`
  line.
- `agw doctor` has a **System plugins** roster: every installed plugin, its description, and its
  opt-in state. The roster is existence, description, and enable-state only; it never enumerates a
  disabled plugin's contributions.
