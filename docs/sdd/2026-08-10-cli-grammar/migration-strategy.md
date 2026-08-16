# CLI Grammar Migration Strategy

- Status: Final artifact checkpoint
- Date: 2026-08-15
- Release boundary: 0.14.0

## Compatibility posture

This is an atomic breaking cutover with no aliases, warnings, shims, or dual-support window.
`resource describe` shipped in 0.13.0; on 2026-08-15 the operator explicitly waived a deprecation
release rather than retain its presentation service and `resource.describe` JSON shape. The other
renamed spellings have not shipped as stable contracts.

The 0.14 upgrade guide maps shipped `resource describe` to the commands that answer its questions
after the cutover. The unreleased `describe-kind` and schema `--write` spellings are replaced
silently throughout active 0.14 guidance rather than presented as user migrations.

## Command map

| Before                                              | After                                                        | Notes                                             |
| --------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------- |
| `agw resource describe-kind TARGET`                 | `agw resource explain TARGET`                                | Same target forms and config-independent behavior |
| `agw resource describe KIND/NAME` for relationships | `agw graph show KIND/NAME`                                   | Defaults to one hop in both directions            |
| `agw resource describe KIND/NAME` for card facts    | Existing inventory, doctor, edit, and kind-specific commands | No replacement generic card                       |
| `agw resource schema --write`                       | `agw resource schema --install`                              | Same whole-set fixed destination                  |
| `agw resource sample KIND --write PATH`             | Unchanged                                                    | `--write` remains path-valued                     |

## Removed-card fact map

| Removed fact                                      | Destination                                                                                         |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Inbound declared dependents                       | Migrated to `graph show`                                                                            |
| Live-instance usage                               | Migrated to `graph show`                                                                            |
| Outbound declared dependencies                    | Newly projected from the existing resource graph to complete the one-hop neighborhood               |
| Identity, description, and origin                 | Existing inventory and kind-specific commands                                                       |
| Not-ready state and full reason                   | `resource list` marker and JSON reason; `doctor` diagnostic explanation                             |
| Disabled state                                    | `resource list --include-disabled` and `doctor`; the old detail sentence is redundant derived prose |
| Secret-specific card and contextual relationships | Existing `secret describe`, unchanged                                                               |

## Contract cutover

### CLI and services

1. Seat the graph and resource-access facts independently of rendering, moving parser, identity,
   origin, and edit-location assertions to those additive owners.
2. Add the graph traversal service, human renderer, and JSON projector, moving declared and live
   relationship assertions to that additive owner.
3. Rename explain while retaining its config-free reference service.
4. Rename schema installation mode while retaining its writer service.
5. Remove resource describe and its presentation service after relationship and fact-map tests pass.

Each commit leaves owned tests and documentation coherent. The implementation plan owns the
single-vehicle exception; commits remain separated by responsibility.

### Machine output

- Remove the closed `resource.describe` command ID and payload fixture.
- Add `graph.show` with a new closed JSON v1 node-and-edge shape.
- Keep `secret.describe` and every unrelated command ID and payload unchanged.

### Completions

- Replace the `resource.describe-kind` target mapping with `resource.explain` and retain its
  config-free resource-kind completer.
- Remove `resource.describe` completion entries.
- Add graph target, direction, depth, and output completion without adding a kind filter.
- Make all eight `resource list --names-only` completion paths registry-only while preserving the
  exact healthy-database candidate identities and order.
- Regenerate and verify bash, zsh, and PowerShell artifacts.

### Documentation and guide coordination

Update active instructions in one sweep: the command reference, `cli/README.md`, sample config,
resources and active platform guides, 0.14 upgrade guide, surviving module READMEs, hints, help, and
examples.

PR #556 has deleted the redundant guide routes. Preserve that landed surface: do not restore or
regenerate its resource topics, schema adapters, or generic guide projection. Run an
active-reference sweep for removed spellings across tracked source, configuration, documentation,
completion, and test files. Only upgrade guidance and clearly historical records may retain them.

Historical ADRs and locked SDDs are not mechanically rewritten.

## Verification order

1. Pin current explain, schema-install, resource-describe inbound/live relationships, and removed
   card facts. Pin outbound declarations independently from the frozen resource graph.
2. Prove graph defaults to the one-hop dependencies, dependents, and live-usage neighborhood with
   retained relationship verbs and provenance, without claiming outbound parity with resource
   describe.
3. Prove each direction and finite or complete-closure depth, including the
   platform-to-site-to-live-VM dependents case, has deterministic cycle-safe traversal. Prove `both`
   may change direction at each expansion rather than combining two monotonic traversals.
4. Prove demand-driven sources, read-only database access, no secret resolution, and no interaction.
   A resource at the depth bound does not demand its live projection, and live-instance nodes are
   terminal.
5. Prove `secret describe` human and JSON contracts are unchanged.
6. Cut command registrations, IDs, completions, and active documentation together.
7. Run focused tests, full repository gates, generated completion checks, and live CLI acceptance.
8. Run required independent reviews before merge intent.

## Rollback

Before 0.14.0 ships, rollback is a commit revert within the draft PR or release branch. There is no
persisted-data migration or compatibility state to unwind. If graph cannot meet its read-only or
relationship-parity requirements, revert the command cutover as a unit rather than restoring an
alias or shipping both ownership models.

After release, incompatible changes to graph JSON or grammar use the normal versioned-output and
upgrade process.
