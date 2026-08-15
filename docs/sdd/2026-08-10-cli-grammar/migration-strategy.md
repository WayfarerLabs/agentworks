# CLI Grammar Migration Strategy

- Status: Revised after operator disposition, pending HLA
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
| `agw resource describe KIND/NAME` for relationships | `agw graph show KIND/NAME`                                   | Fixed one-hop, both-directions view               |
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

1. Seat the graph fact service for the fixed neighborhood independently of rendering.
2. Add `graph show`, its human renderer, and its JSON projector.
3. Rename explain while retaining its config-free reference service.
4. Rename schema installation mode while retaining its writer service.
5. Remove resource describe and its presentation service after relationship and fact-map tests pass.

Each commit leaves owned tests and documentation coherent. The draft PR remains the single vehicle,
with commits separated by responsibility.

### Machine output

- Remove the closed `resource.describe` command ID and payload fixture.
- Add `graph.show` with a new closed JSON v1 node-and-edge shape.
- Keep `secret.describe` and every unrelated command ID and payload unchanged.

### Completions

- Replace the `resource.describe-kind` target mapping with `resource.explain` and retain its
  config-free resource-kind completer.
- Remove `resource.describe` completion entries.
- Add graph target and output completion without adding kind, direction, or depth options.
- Regenerate and verify bash, zsh, and PowerShell artifacts.

### Documentation and guide coordination

Update active instructions in one sweep: the command reference, `cli/README.md`, sample config,
resources and active platform guides, 0.14 upgrade guide, surviving module READMEs, hints, help, and
examples.

A separate effort owns deletion of redundant guide routes. Rebase or coordinate before the
documentation sweep, do not edit a route it deletes, and do not recreate that route through
generated teaching. Run an active-reference sweep for removed spellings across tracked source,
configuration, documentation, completion, and test files. Only upgrade guidance and clearly
historical records may retain them.

Historical ADRs and locked SDDs are not mechanically rewritten.

## Verification order

1. Pin current explain, schema-install, resource-describe inbound/live relationships, and removed
   card facts. Pin outbound declarations independently from the frozen resource graph.
2. Prove graph produces the fixed outbound, inbound, and live-usage neighborhood with retained
   provenance, without claiming outbound parity with resource describe.
3. Prove demand-driven sources, read-only database access, no secret resolution, and no interaction.
4. Prove `secret describe` human and JSON contracts are unchanged.
5. Cut command registrations, IDs, completions, and active documentation together.
6. Run focused tests, full repository gates, generated completion checks, and live CLI acceptance.
7. Run required independent reviews before merge intent.

## Rollback

Before 0.14.0 ships, rollback is a commit revert within the draft PR or release branch. There is no
persisted-data migration or compatibility state to unwind. If graph cannot meet its read-only or
relationship-parity requirements, revert the command cutover as a unit rather than restoring an
alias or shipping both ownership models.

After release, incompatible changes to graph JSON or grammar use the normal versioned-output and
upgrade process.
