# System plugins

A **system plugin** bundles capability implementations (and, optionally, resource manifests) that
ship with agentworks but are separable and opt-in. A plugin is not a resource kind and never
publishes a new kind: it contributes implementations of the four existing capability kinds
(`vm-platform`, `harness-integration`, `git-credential-provider`, `secret-backend`) and, optionally,
declarable resources bundled as YAML manifests. A plugin is an **origin** (`system-plugin`), the
fourth alongside `operator-declared`, `built-in`, and `auto-declared`.

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
- **`capabilities`**: a mapping keyed by capability kind, each value a tuple of impl **classes**.
  Every capability registry stores that exact class under its `name`; registration, graph
  publication, and snapshot/restore never construct or substitute an instance. Every impl exposes
  `name` and `description` as class attributes; the impl's `name` is the registry key its published
  row carries.
- **`manifests`** (optional): an importlib-resources package anchor whose `manifests/` subdirectory
  holds the plugin's bundled YAML resource manifests (the same envelope operators write; see
  `docs/guides/resources.md`). `None` when the plugin ships no manifests.
- **`guide_topics`**: an inert tuple of `TopicContribution` records consumed only while building an
  `agw guide` catalog. It does not execute during plugin registration or ordinary commands.
- **`required_scopes`** and **`commands`**: reserved, inert placeholders (see below).

The descriptor depends on nothing in the capability or registry machinery, so it is constructible in
a test without a registry. It becomes valid or rejected only when the installed index registers it.

### Guide contribution boundaries

A plugin may contribute an implementation topic it owns, a declarable resource topic registered
through its owner adapter, or a `plugin/<plugin>/<topic>` concept. It cannot claim core `concept-*`
topics, bare kind topics, another plugin's namespace, or another owner's resource. The guide catalog
isolates an invalid plugin topic while retaining valid core and plugin topics. The full index
reports rejected content and exits 1, while an unrelated valid topic remains a clean response. A
retained topic whose live projection is unavailable keeps its authored teaching, reports the scoped
issue, and exits 1.

Every renderer-owned level-2 heading in raw CLI Markdown carries the exact literal `⟦AGW framework⟧`
marker. After bounded input validation, HTML entity decoding, and Unicode normalization, the
contribution contract rejects either delimiter, whether literal or HTML-entity encoded, in authored
topic titles, summaries, and Markdown. An authored contribution therefore cannot emit that exact
literal marker in raw CLI Markdown. Other authored Markdown and HTML, including ordinary headings,
remain authored content and are not relabeled.

The marker is a source-provenance convention, not an anti-spoof guarantee for arbitrary downstream
Markdown, HTML, or CSS renderers, images, or styling. It grants no authority or trust to the content
that follows.

Guide contributions are data, not callbacks. Expression markers in authored titles, summaries, and
Markdown are accepted only inside a closed, same-line literal delimited by one unescaped backtick on
each side. The backticks cannot touch another backtick. Multi-backtick spans, fenced blocks,
multiline spans, escaped backticks, unmatched spans, headings, HTML, and prose do not exempt a
marker. Guide rendering never evaluates the accepted literal. Terminal control bytes are removed
from rendered output. Action records accept three exact token forms: a literal that starts with an
ASCII letter or digit and then contains only ASCII letters, digits, `.`, `_`, `:`, `/`, or `-`; a
flag that starts with `-` or `--`, then a lowercase ASCII letter or digit, and continues with
lowercase ASCII letters, digits, or `-`; or an exact registered input placeholder such as
`$SECRET_NAME`. Valid examples include `agw`, `vm-template/demo`, `secret_name`, `v1.2`, `-v`, and
`--non-interactive`. Invalid examples include the absolute path `/tmp/file`, the tilde path
`~/file`, `--flag=value`, `*.yaml`, and `#comment`. Each title is limited to 256 UTF-8 bytes, each
summary to 2 KiB, each authored block to 64 KiB, and one topic to 64 blocks, 64 related links, and
256 KiB of authored markdown. Every related link must be a canonical topic slug no larger than 317
UTF-8 bytes. A field-reference section accepts at most 32 path items of 256 UTF-8 bytes each. Keep
authored files under the owning package's `guide-content/` directory so the wheel package-data
assertion exercises them.

An `ActionList` contains inert `GuideAction` records, never an executor. Each action provides at
most 32 inputs and exactly one of a literal-token command or bounded platform-neutral manual steps.
Command and verification sequences contain at most 64 tokens, each at most 1 KiB. Input names are at
most 64 bytes, input descriptions are at most 2 KiB, and preconditions, expected states, refusal
alternatives, and manual instructions are each at most 8 KiB. One action list contains at most 32
actions and 128 KiB of action data. Action IDs are unique across all action lists in a topic, and
action data also counts toward the topic's 256 KiB bound. Rendered actions state their inputs,
consent boundary, expected result, optional verification, and useful refusal alternative without
executing any operation. The same expression-marker scanner covers every rendered action prose field
and input description. Command and verification tokens remain under the closed literal grammar.

`FieldReference` and `Sample` blocks contain selectors only. They read
`agentworks.manifests.reference` and `agentworks.manifests.samples` directly, never another CLI's
text or a copied field list. Field references are valid only for kind and capability-implementation
anchors. Samples are valid only for declarable bare kinds. Keep a resource topic linked to its bare
kind instead of attaching schema blocks to the resource instance. Field descriptions and alternative
summaries use the shared prose normalization. Literal defaults, examples, choices, and constraints
retain their exact rendered values inside Markdown code spans sized and padded for their backticks
and edge spaces. After YAML rendering, backslashes, carriage returns, line feeds, and tabs become
distinct visible escape sequences so one scalar cannot break the reference row across lines.

## Shipping a plugin

The installed index (`plugins/__init__.py`) uses **inverted registration**: it imports each shipped
plugin module and calls `register_plugin(module.PLUGIN)` itself, rather than registration being an
import side effect. To ship a plugin, add its module to `_INSTALLED_MODULES`:

```python
_INSTALLED_MODULES: tuple[_PluginModule, ...] = (
    agentworks_azure,
)
```

Every module listed there is registered and seated at import; `agw doctor`'s **System plugins**
roster is what a given build actually ships. On import the index:

- registers each plugin, wrapping any failure with the real module name (a bad descriptor is a
  curation bug that reads as `system plugin '<module>' failed to register: ...`, not an opaque
  traceback that kills the CLI), and
- rejects a duplicate plugin name as a typed error.

`register_plugin` validates the **whole** descriptor first (name shape; every capability kind has an
adapter; every impl is a class with a non-empty, `/`-free `name`; every impl **conforms to its
kind's contract**, see below; no intra-descriptor name collisions), then seats every impl
**atomically**: no capability registry is touched until every impl across the descriptor is known
seatable, so a mid-descriptor failure can never leave orphaned impls behind. Registration is
idempotent per impl name; a cross-plugin or plugin-versus-built-in name clash on the same capability
is a typed error naming the occupant's real origin.

### Contract conformance

Every impl is checked against its kind's descriptor (`agentworks/capabilities/descriptor.py`) before
anything is seated, so a class that merely looks plausible is refused at registration rather than
failing far from the mistake. The checks:

- **Contract**: derives from the kind's nominal capability base. `secret-backend` implementations
  subclass `SecretBackend`; a structural lookalike is rejected even when it exposes similarly named
  members.
- **Metadata**: `name` (non-empty, `/`-free) and `description`, readable as class attributes.
- **Attributes**: the kind's other non-operation members are present. A `secret-backend` declares
  `interactive` as exactly `bool`, plus separate `config_model` and `mapping_model` surfaces.
- **Constructibility**: nothing would stop the class being constructed (no unimplemented
  `@abstractmethod`). Checked structurally; the impl is never constructed to find out.
- **Operations**: the domain operations the framework depends on are present and callable. For a
  `secret-backend`, registration also checks the exact classmethod binding, parameter shape,
  resolved annotations, and return annotation of `backend_readiness`, `would_attempt`,
  `describe_lookup`, `external_operation_timeout`, and `create_client`.
- **Config models**: the impl declares every model surface its descriptor names (see
  [Declaring config](#declaring-config)). Each extends its declared base and can be built; tagged
  config models identify their implementation. A secret backend's source config cannot reference a
  secret, and its per-secret mapping annotation tree must be JSON-native. A model that could never
  be reached from a manifest is refused where its author can see it rather than going quietly
  unaddressable.
- **Contract version**: the impl's `contract_version` equals the version this build supports. Every
  impl of every kind spells it; nothing defaults it, so a version claim is always made rather than
  inherited. Exact equality, so a contract change is a hard cutover.

Each failure is a `PluginError` naming the plugin, the kind, the impl, and what is missing.

## Declaring config

A capability implementation DECLARES the shape of its config as a model, and the core does
everything else with it: shape validation, reference extraction, defaulting, JSON Schema emission,
`agw resource sample`, and `agw resource describe-kind` are all derived views of that one
declaration. **No plugin code is invoked for any of them**, which is what keeps a misbehaving plugin
out of the registry's finalize pass and what makes it impossible for a plugin's validator and its
documentation to disagree.

```python
from typing import Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.schema import AgwModel, NonEmptyStr, SecretRef


class ExampleCloudConfig(AgwModel):
    """An Example Cloud region, as one vm-site points at it."""

    name: Literal["example-cloud"]
    """The platform this config is for."""

    region: NonEmptyStr = Field(examples=["us-west-2"])
    """The region new VMs are created in."""

    api_token: Annotated[
        NonEmptyStr,
        SecretRef(usage="the Example Cloud API token", default_template="example-cloud-token"),
    ]
    """The secret holding the API token's value. Never the value itself:
    the field NAMES a secret in the framework's secret system."""

    instance_prefix: NonEmptyStr = "agw"
    """The name prefix new VMs are created under."""


class ExampleCloudPlatform(VMPlatform):
    name: ClassVar[str] = "example-cloud"
    description: ClassVar[str] = "Example Cloud VMs (region-scoped)"
    contract_version: ClassVar[int] = 2
    config_model: ClassVar[type[AgwModel]] = ExampleCloudConfig
```

For vm-platform contract version 2, `create()` receives a required Tailscale auth key and a
value-free bootstrap-progress sink in `ProvisionRequest`. It must finish the Tailscale join before
returning or raise after rolling back partial backend state. A successful `ProvisionResult` may omit
the Tailscale IP only when join succeeded but IP discovery did not; the manager then performs
IP-only rediscovery and Tailscale SSH verification. There is no older-contract adapter. Platforms
with multi-step in-create bootstrap use the required progress sink for observable steps and output;
simple fixed-stdin join finishers may accept it without emitting a platform-specific transcript.

A site then writes `platform: {name: example-cloud, region: us-west-2}`, and `api_token` resolves to
the `example-cloud-token` secret because the field was omitted. An OMITTED reference field and an
explicit `null` both mean "the owner-templated default", so leaving it out is how an operator takes
the default rather than how they opt out of the dependency.

What the base and the markers buy you:

- **`AgwModel` is strict, frozen, and closed-world.** A key the model does not declare is a load
  error naming the fields it does, a wrong type is an error rather than a coercion, and the operator
  reads it framed as their resource with the file and line they wrote it on.
- **The attribute docstring IS the field's operator-facing description.** It is what
  `agw resource describe-kind`, the generated sample, and the editor's hover text render, so write
  it for an operator. Do not restate the field list anywhere else; a second copy is free to drift.
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
[Contract conformance](#contract-conformance). The published row is read-only and lists, describes,
and is referenced like any other resource of that kind.

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
