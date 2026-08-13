# High-Level Architecture: Installer Resource Plugins

- Status: Draft for artifact review
- Date: 2026-08-13
- Inputs: the [FRD](./frd.md), [inventory](./inventory.md), and `main` at `c7093147`

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
with plugin identity `install-command`. Each descriptor has an empty capability map, a
package-resource manifest anchor, and one conceptual guide topic.

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

Plugin manifest publication, enablement, registry visibility, use gating, and same-name operator
override behavior already apply to these resource kinds. The move uses those contracts unchanged. No
installer-specific validation or remediation layer is added.

The existing generic consumers remain responsible for apt source/package ordering, system/admin/
agent install-command execution, predicate evaluation, PATH results, and idempotent reinit.

### D4. Contribute teaching without duplicating the registry

Each plugin owns one `plugin/<name>/overview` conceptual guide topic. A module-local helper reads
only Markdown packaged beside the plugin and constructs one `TopicContribution` eagerly at import.
The descriptor stores that inert object in its existing `guide_topics` tuple. There is no callback,
execution hook, or new guide extension seam.

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

Tests pin the exact 16-row payload inventory and changed provider origin, both descriptors, default
disablement, independent and combined opt-in, use gating, operator override parity, doctor roster,
guide topics, and completion projection.

The wheel gate loads both YAML and Markdown assets through package resources. Source-checkout tests
alone are insufficient because missing package data would make the shipped plugins incomplete.

## Compatibility

Stable:

- resource selectors and payloads
- VM, admin, and agent template schemas
- apt and install-command kinds
- references, predicates, and executor ordering
- current disabled-plugin errors and same-name overrides
- snap, mise, dotfiles, tmuxinator, and Claude behavior

Intentional break:

- operators selecting one of the 16 moved resources must enable its owning installed plugin in
  `[plugins].system`

There is no migrator or compatibility alias. The 0.14 upgrade guide inventories the moved selectors
and gives the exact enablement lines.

## Rejected alternatives

- Moving generic runners: operator-declared resources use the same runners, so this would exceed the
  declaration-only scope.
- Moving snap, mise, dotfiles, tmuxinator, or Claude setup: none is an existing declared resource in
  this move set.
- Adding 16 authored guide pages: generated resource topics already provide the exact inventory.
