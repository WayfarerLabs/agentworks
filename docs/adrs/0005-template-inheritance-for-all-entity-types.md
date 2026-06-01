# 5. Template inheritance for all entity types

Date: 2026-03-08

## Status

Accepted

## Context

Agentworks manages four entity types (VMs, workspaces, agents, tasks) that each need configurable
defaults with the ability to define named variations. Without templates, every VM gets the same
config and every agent gets the same config. With templates, operators can define profiles like
"heavy" VMs with more resources or "restricted" agents with fewer permissions.

The question is whether each entity type should have its own templating mechanism or whether a
single, consistent pattern should apply everywhere.

## Decision

All entity types use the same template pattern:

- Templates are named TOML sections (`[vm_templates.heavy]`, `[agent_templates.restricted]`, etc.).
- A `default` template is used when `--template` is not specified.
- The `default` template can be customized simply by defining a `[<entity>_templates.default]`
  section in the config file. This is entirely optional. If no `default` is explicitly defined,
  built-in defaults apply.
- Templates support inheritance via `inherits` (depth-first, left-to-right).
- Consistent merge semantics are applied solely based on field types:
  - Scalars: child overrides parent (last-one-wins).
  - Lists: append with dedupe (parent items first, child items added if not already present).
  - Maps: merge with child winning on key collision.
  - `None` means "not set, inherit from parent or use built-in default."

## Consequences

- Operators learn one templating model and apply it everywhere. No per-entity-type quirks.
- The implicit default means config files start minimal. Only define what you need to override.
- Inheritance enables composition: a "heavy" VM template can inherit from "default" and only
  override resource fields. A child that adds `apt = ["python3-dev"]` gets the parent's apt packages
  plus python3-dev, not a replacement.
- Map fields (like task template `env`) merge naturally: a parent can define base environment
  variables and children can add or override specific keys.
- The resolution code is nearly identical across entity types, making it easy to add new template
  types in the future.
- Tradeoff: list append semantics mean a child cannot remove an item from a parent's list. If
  removal is needed, the child must not inherit from that parent.
