# High-Level Architecture: Installer Resource Plugins

- Status: Revised for artifact review
- Date: 2026-08-14
- Inputs: the [FRD](./frd.md), [inventory](./inventory.md), and `main` at `6771c02a`

## Boundary

The architecture relocates 16 existing built-in manifest rows into two installed, opt-in system
plugins. Nothing executes through a new path. Both plugins publish into the same finalized resource
registry consumed by core today.

```text
installed plugin index
  -> apt manifest package
  -> install-command manifest package
  -> existing manifest decoder and plugin publisher
  -> existing finalized resource registry
  -> existing VM, admin, and agent consumers
```

## Decisions

### D1. Use manifest-only system plugins

Add `agentworks.plugins.apt` with plugin identity `apt` and `agentworks.plugins.install_command`
with plugin identity `install-command`. Each descriptor has an empty capability map and a
package-resource manifest anchor. Each owning package also supplies one conceptual guide topic
through the guide-scoped first-party collection described in D4.

The underscore in the Python package and hyphen in the plugin identity follow their respective
naming conventions. Manifest provenance derives from the descriptor's actual manifest anchor so the
recorded source path remains truthful for the hyphenated plugin identity.

### D2. Move rows without translation

Move the ten apt rows and six user install-command rows enumerated in the inventory at the decoded
entry boundary. Preserve every metadata name, spec field, sequence, command, URL, installed check,
PATH addition, and apt source reference.

The original built-in files stop publishing the moved rows in the same change. Each selector has
exactly one app-shipped provider at every mergeable revision.

### D3. Retain the current resource contracts

Plugin manifest publication, enablement, registry visibility, use gating, and same-name row
precedence already apply to these resource kinds. The move uses those contracts unchanged. No
installer-specific validation or remediation layer is added.

Row precedence does not propagate enablement through dependencies. FRD AC4 and the migration
strategy's operator-override section define the resulting composite contract; this architecture adds
no exception or new runtime behavior.

The existing generic consumers remain responsible for apt source/package ordering, system/admin/
agent install-command execution, predicate evaluation, PATH results, and idempotent reinit.

### D4. Load first-party teaching only inside the guide boundary

Each plugin owns one `plugin/<name>/overview` conceptual guide topic. The plugin package keeps a
private `_load_guide_contributions()` adapter matching the request-scoped loading pattern already
used by core owning packages. A module-local two-entry package-anchor mapping in the guide service
imports those private adapters only during guide-scoped catalog construction, then feeds their inert
`TopicContribution` tuples through the existing system-plugin candidate path. It is a fixed
first-party mapping, not a registry or extension seam.

The existing `Plugin.guide_topics` seat holds already-materialized inert contributions. It cannot
directly carry these file-backed topics without reading Markdown while constructing the descriptor;
turning it into a loader would add the callback contract this bucket-only effort excludes. This
effort therefore leaves that field and the public plugin contract unchanged rather than duplicating
its candidate-processing path or making plugin imports perform I/O.

Ordinary plugin imports perform no guide file I/O. Each private adapter reads only Markdown packaged
beside its plugin, so an asset failure fails that guide request but cannot break plugin registration
or an unrelated command. First-party package corruption is rejected by source and installed-wheel
coverage rather than converted into a plugin-scoped catalog issue. The installed-wheel catalog probe
requires both first-party topic slugs to be retained without catalog issues.

The topic teaches ownership, the disabled-by-default posture, discovery, and verification. Any
suggested config mutation is a validated, inert `GuideAction` with the exact config target,
`mutate-agentworks` authorization class, expected state, and a refusal alternative that leaves the
plugin disabled. Any suggested verification command is a separate inert action with its own
`read-configured-state` authorization class, expected result, and refusal alternative. Rendering a
topic never performs either action or grants consent.

The guide does not duplicate 16 resource reference pages. Registry-derived resource topics remain
the source of truth for exact payloads, relationships, state, and instances.

### D5. Keep completion derivation unchanged

The CLI command tree does not change. Resource completion continues to consume the normal filtered
resource list, so disabled plugin rows remain absent there. Guide completion consumes the authored
and generated guide catalog, which deliberately retains conceptual and dynamic topics for disabled
rows so operators can discover their state and enablement path. Tests pin this existing split; no
shell-specific completion code is added.

### D6. Verify both source and packaged behavior

Child-specific behavior tests pin the exact 16-row payload inventory and changed provider origin,
the two shipped descriptors and their default disablement, and truthful provenance from each
manifest anchor. One import-boundary test proves that importing either plugin performs no guide file
I/O. Existing generic framework tests remain the oracle for publication, gating, row precedence,
multiple enabled plugins, guide candidate validation, and completion projection.

The wheel gate adds coverage for both YAML bundles because no generic manifest-asset glob exists.
The existing installed-wheel guide test already covers every `agentworks/**/guide-content/**/*.md`
file, including these plugins' content; its existing catalog probe is extended to require both new
topics without plugin-scoped issues.

## Compatibility

Stable:

- resource selectors and payloads
- VM, admin, and agent template schemas
- apt and install-command kinds
- references, predicates, and executor ordering
- current disabled-plugin errors and same-name row precedence
- snap, mise, dotfiles, tmuxinator, and Claude behavior

Intentional break:

- operators selecting one of the 16 moved resources must enable its owning installed plugin in
  `[plugins].system`

The 0.14 upgrade guide inventories the moved selectors and gives the exact enablement lines. FRD R10
is the authoritative compatibility ruling.

## Rejected alternatives

- Moving generic runners: operator-declared resources use the same runners, so this would exceed the
  declaration-only scope.
- Moving snap, mise, dotfiles, tmuxinator, or Claude setup: none is an existing declared resource in
  this move set.
- Adding 16 authored guide pages: generated resource topics already provide the exact inventory.
