# System plugins

A **system plugin** bundles capability implementations (and, optionally, resource manifests) that
ship with agentworks but are separable and opt-in. A plugin is not a resource kind and never
publishes a new kind: it contributes implementations of the four existing capability kinds
(`vm-platform`, `harness-integration`, `git-credential-provider`, `secret-backend`) and, optionally,
declarable resources bundled as YAML manifests. A plugin is an **origin** (`system-plugin`), the
fourth alongside `operator-declared`, `built-in`, and `auto-declared`.

The shipped index currently installs `onepassword`, `apt`, `install-command`, `claude`, `proxmox`,
`azure`, `codex`, `grok`, `aws`, and `gcp`; all are disabled until named in `[plugins].system`. The
index remains authoritative, and `agw doctor` renders it directly.

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

- **`name`** (required): the plugin's identity. It uses lowercase ASCII letters, digits, and
  hyphens, starts with a letter, and ends with a letter or digit. It is the name an operator writes
  in `[plugins].system` and the name the surfaces attribute rows to (`from plugin example-cloud`).
- **`description`**: shown in the doctor roster.
- **`capabilities`**: a mapping keyed by capability kind, each value a tuple of impl **classes**.
  Every capability registry stores that exact class under its `name`; registration, graph
  publication, and snapshot/restore never construct or substitute an instance. Every impl exposes
  `name` and `description` as class attributes; the impl's `name` is the registry key its published
  row carries.
- **`manifests`** (optional): an importlib-resources package anchor whose `manifests/` subdirectory
  holds the plugin's bundled YAML resource manifests (the same envelope operators write; see
  `docs/guides/resources.md`). `None` when the plugin ships no manifests.
- **`required_scopes`** and **`commands`**: reserved, inert placeholders (see below).

The descriptor depends on nothing in the capability or registry machinery, so it is constructible in
a test without a registry. It becomes valid or rejected only when the installed index registers it.

### Vendor bundles grow by composition

The plugin name is the vendor-level composition boundary, not the name of one service capability.
For example, `gcp` bundles the independently named `gcp-gce` VM platform and optional `gcloud-cli`
guest apt package, while `aws` currently contributes only `aws-ec2`. A future vendor capability
keeps its own existing capability contract, model, and service-specific name, then joins the
existing vendor plugin. Do not introduce a provider-wide base class or reserve an unconsumed
abstraction merely because a vendor bundle gains a second contribution.

### Guide content ownership

The guide has no plugin registration API. First-party packages inside `agentworks` may own a concept
by placing one Markdown shell directly in a package-local `guide-content/` directory. The guide
discovers those files only when requested; plugin registration and ordinary commands do not read
them. A shell named `apt.md` becomes the global topic `concept-apt`, so filenames must be unique
across the installed first-party package tree. Separately installed plugins are outside this
discovery boundary.

Each shell has restricted frontmatter containing a single-line `description` and optional bounded
`index-order`, followed by one unfenced level-1 heading and ordinary reviewed Markdown. The optional
order selects the concept for the concise no-topic index. The required reserved `_index.md` belongs
to the core guide package and is not a plugin concept. Every other underscore-prefixed Markdown
filename in a `guide-content/` directory is reserved and invalid. Agent-only fences and bounded
exact-section includes are the only directives. Their comment lines must stand alone at column zero
between top-level Markdown blocks; comments inside lists, block quotes, or code remain content.
Shells do not call Python or inspect configuration, resources, secrets, provider state, or the
workstation. Signpost command-owned facts instead of copying them into teaching. The guide contract
and structural tests own the exact grammar and package-data boundary. Discovery validates every
installed first-party shell atomically: a malformed shell or duplicate global filename prevents the
index, list, show, and topic-completion paths from returning a partial catalog.

## Shipping a plugin

The installed index (`plugins/__init__.py`) uses **inverted registration**: it imports each shipped
plugin module and calls `register_plugin(module.PLUGIN)` itself, rather than registration being an
import side effect. To ship a plugin, import its module under an underscore alias and add it to
`_INSTALLED_MODULES`:

```python
from agentworks.plugins import azure as _azure
from agentworks.plugins import claude as _claude

_INSTALLED_MODULES: tuple[_PluginModule, ...] = (
    _claude,
    _azure,
)
```

The shipped tuple lists every system plugin; the lines above are the two a new plugin adds.

Every module listed there is registered and seated at import; `agw doctor`'s **System plugins**
roster is what a given build actually ships. On import the index:

- registers each plugin, wrapping any failure with the real module name (a bad descriptor is a
  curation bug that reads as `system plugin '<module>' failed to register: ...`, not an opaque
  traceback that kills the CLI), and
- rejects a duplicate plugin name as a typed error.

`register_plugin` validates the **whole** descriptor first (the plugin name follows the identifier
shape above; every capability kind has an adapter; every impl is a class with a non-empty, `/`-free
`name`; every impl **conforms to its kind's contract**, see below; no intra-descriptor name
collisions), then seats every impl **atomically**: no capability registry is touched until every
impl across the descriptor is known seatable, so a mid-descriptor failure can never leave orphaned
impls behind. Registration is idempotent per impl name; a cross-plugin or plugin-versus-built-in
name clash on the same capability is a typed error naming the occupant's real origin.

### Contract conformance

Every impl is checked against its kind's descriptor (`agentworks/capabilities/descriptor.py`) before
anything is seated, so a class that merely looks plausible is refused at registration rather than
failing far from the mistake. The checks:

- **Contract**: derives from the kind's nominal capability base. `secret-backend` implementations
  subclass `SecretBackend`; a structural lookalike is rejected even when it exposes similarly named
  members.
- **Metadata**: `name` (non-empty, `/`-free) and `description`, readable as class attributes.
- **Attributes**: the kind's other non-operation members are present. A `secret-backend` declares
  `supports_tty_interaction` as exactly `bool`, plus separate `config_model` and `mapping_model`
  surfaces.
- **Constructibility**: nothing would stop the class being constructed (no unimplemented
  `@abstractmethod`). Checked structurally; the impl is never constructed to find out.
- **Operations**: the domain operations the framework depends on are present and callable. For a
  `secret-backend`, registration also checks the call shape of `backend_readiness`,
  `describe_lookup`, and `create_client`: each declared as a `@classmethod`, with the parameter
  names and kinds the resolution loop calls with. The factory receives tagged operation intent,
  exact TTY access, an optional broker, and validated source config before any lifecycle hook runs.
  Core source identity and a generic operation deadline do not cross this boundary. Annotations are
  not compared, because a type claim is what the type checker is for and a plugin's types are not
  under it either way.
- **Config models**: the impl declares every model surface its descriptor names (see
  [Declaring config](#declaring-config)). Each extends its declared base and can be built; tagged
  config models identify their implementation. Both config and mapping model surfaces must satisfy
  their `ConfigContract`'s schema-directed merge contract when that contract opts in. A secret
  backend's source config cannot reference a secret, and its per-secret mapping annotation tree must
  be JSON-native. A model that could never be reached from a manifest is refused where its author
  can see it rather than going quietly unaddressable.
- **Contract version**: the impl's `contract_version` equals the version this build supports. Every
  impl of every kind spells it; nothing defaults it, so a version claim is always made rather than
  inherited. Exact equality, so a contract change is a hard cutover.

Each failure is a `PluginError` naming the plugin, the kind, the impl, and what is missing.

The complete version-1 secret-backend authoring contract, including exact result variants, reason
ownership, core flow, lifecycle, TTY broker rules, value containment, and a conforming example,
lives in the [secret-backend capability README](../capabilities/secret_backend/README.md).
Secret-backend version `2` shipped in Agentworks 0.14.0 and 0.14.1, when plugins could already
contribute this capability; there are no known third-party implementations. Agentworks 0.15
intentionally renumbers the incompatible complete contract to exact version `1`, with no adapter.
Backend authors must migrate the complete client and tagged-result API before registration.

## Declaring config

A capability implementation DECLARES the shape of its config as a model, and the core does
everything else with it: shape validation, reference extraction, defaulting, JSON Schema emission,
`agw resource sample`, and `agw resource explain` are all derived views of that one declaration.
**No plugin code is invoked for any of them**, which is what keeps a misbehaving plugin out of the
registry's finalize pass and what makes it impossible for a plugin's validator and its documentation
to disagree.

```python
from typing import Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.schema import AgwModel, NonBlankStr, NonEmptyStr, SecretRef


class ExampleCloudConfig(AgwModel):
    """An Example Cloud region, as one vm-site points at it."""

    name: Literal["example-cloud"]
    """The platform this config is for."""

    region: NonBlankStr = Field(examples=["us-west-2"])
    """The region new VMs are created in."""

    api_token: Annotated[
        NonEmptyStr,
        SecretRef(usage="the Example Cloud API token", default_template="example-cloud-token"),
    ]
    """The secret holding the API token's value. Never the value itself:
    the field NAMES a secret in the framework's secret system."""

    instance_prefix: NonBlankStr = "agw"
    """The name prefix new VMs are created under."""


class ExampleCloudPlatform(VMPlatform):
    name: ClassVar[str] = "example-cloud"
    description: ClassVar[str] = "Example Cloud VMs (region-scoped)"
    contract_version: ClassVar[int] = 3
    config_model: ClassVar[type[AgwModel]] = ExampleCloudConfig
```

For vm-platform contract version 3, `create()` receives a concrete core-selected Debian release, a
required Tailscale auth key, and a value-free bootstrap-progress sink in `ProvisionRequest`. The
platform resolves the release through a local artifact map before mutation, with no default or
fallback. It must finish the Tailscale join, probe `/etc/os-release` while backend rollback remains
available, and raise after cleaning up a mismatch. Core independently repeats that probe over the
returned transport and persists its own observation rather than trusting a plugin result field. A
missing code-owned map entry says that Agentworks or the plugin is out of date; an operator-owned
catalog miss names the exact vm-site field. A successful result may omit the Tailscale IP only when
join succeeded but IP discovery did not; the manager then performs IP-only rediscovery and Tailscale
SSH verification. There is no older-contract adapter.

An operator-owned release catalog also overrides the pure `validate_create_release(release)` hook.
Core calls it with the concrete selection before resolving secrets or running authenticated platform
readiness, while `create()` repeats the lookup before mutation. Do not turn that operation-specific
requirement into load-time rejection when the same site can still operate existing VMs. When the
backend can inspect an operator-owned artifact after boot but before Agentworks bootstrap, attest
`/etc/os-release` at that boundary and roll back a mismatch. Core retains the final live probe over
the returned transport; neither verification gets an operator bypass.

A site then writes `platform: {name: example-cloud, region: us-west-2}`, and `api_token` resolves to
the `example-cloud-token` secret because the field was omitted. An OMITTED reference field and an
explicit `null` both mean "the owner-templated default", so leaving it out is how an operator takes
the default rather than how they opt out of the dependency.

What the base and the markers buy you:

- **`AgwModel` is strict, frozen, and closed-world.** A key the model does not declare is a load
  error naming the fields it does, a wrong type is an error rather than a coercion, and the operator
  reads it framed as their resource with the file and line they wrote it on.
- **The attribute docstring IS the field's operator-facing description.** It is what
  `agw resource explain`, the generated sample, and the editor's hover text render, so write it for
  an operator. Do not restate the field list anywhere else; a second copy is free to drift.
- **`SecretRef` / `ResourceRef` mark a field that NAMES another resource**, optionally with an
  owner-templated default (`git-token-{owner_name}`). That marker is the single authored place the
  reference semantics live: extraction reads it to build the dependency graph, validation fills the
  default from it, emitted JSON Schema carries it as `x-agw-ref`, and it is what later authorizes an
  op to read that secret through `ctx.secret(name)`.
- **A capability that accepts no configuration still declares a model**, one with no fields beyond
  its tag. "Accepts nothing" has to be something an author SAYS, not something they get by
  forgetting.

Whether the model carries a `name` tag depends on the surface. `vm-platform`, `harness-integration`,
and `git-credential-provider` config models are selected by a `name` key inside their tagged table.
A secret backend's source config is tagged the same way, while its per-secret mapping is selected by
its outer map key, carries no tag, and extends `AgwRootModel` rather than `AgwModel` because a
mapping may be a bare string. Conformance checks both backend models against their separate
contracts.

The capability model as a whole, including how a config is offered per facet and what the framework
does with the declaration at each lifecycle stage, is
[`../capabilities/README.md`](../capabilities/README.md).

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
- **`harness-integration`**: how a session runs its harness or shell workload (paired with a
  `session-template`'s config).
- **`git-credential-provider`**: how a git token is obtained and served.
- **`secret-backend`**: where a secret's value is read from.

Each impl subclasses the kind's nominal capability base class and exposes `name` / `description`
class attributes. Registration checks that conformance before seating anything; see
[Contract conformance](#contract-conformance). The published row is read-only, appears in resource
inventory and kind explanation, and participates in the graph like any other resource of that kind.

**Bundled manifests** are ordinary YAML resource documents the plugin ships, under the `manifests/`
subdirectory of the package `manifests` points at. Only declarable kinds whose consumption sites are
gated may be bundled (install-commands, apt entries/packages, and the template kinds); kinds whose
reference paths are not gated (such as `secret` or `vm-site`) are rejected at publish, as is a
reserved auto-declared name (a `default` template). They load through the same typed, validated
loader the built-in manifests use, stamped with the plugin's `system-plugin` origin. A malformed
bundle is a typed error attributed to the plugin, never a bare import or assertion failure.

Install-command manifests contain one logical shell invocation as a single-line YAML scalar, either
plain or quoted. Prefer the template's `apt`, `apt_packages`, `snap`, or `mise_packages` surfaces,
followed by a maintained package-manager or vendor entry point. Do not embed a script, block scalar,
here-document, multi-step installer, state machine, signature pipeline, or cleanup routine. The
invocation must be repeat-safe itself or use the existing `test_exec`, `test_file`, or `test_dir`
completion fields. A system install command still runs as the VM admin, not root, and explicitly
invokes `sudo` for any privileged step.

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
- `agw doctor` has a **System plugins** roster: every installed plugin, its description, and its
  opt-in state. The roster is existence, description, and enable-state only; it never enumerates a
  disabled plugin's contributions.
