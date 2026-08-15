# CLI Grammar Migration Strategy

- Status: Draft, pending FRD and HLA approval
- Date: 2026-08-15
- Release boundary: 0.14.0, before a stable release of the current spellings

## Compatibility posture

This is an atomic pre-0.14 cutover. The old spellings are removed without aliases, warnings, or a
dual-support window. Carrying aliases would enlarge completions, tests, documentation, and support
surface for commands that have not shipped as stable contracts.

The upgrade guide is the compatibility mechanism. It names every removed spelling and its new
operator question.

## Command map

| Before                                                                | After                                                   | Notes                                               |
| --------------------------------------------------------------------- | ------------------------------------------------------- | --------------------------------------------------- |
| `agw resource describe-kind TARGET`                                   | `agw resource explain TARGET`                           | Same target grammar and config-independent behavior |
| `agw resource describe KIND/NAME` for relationships                   | `agw graph show KIND/NAME` with the approved direction  | Direction default and flags settle in HLA           |
| `agw resource describe KIND/NAME` for identity, status, or kind facts | Existing list, doctor, edit, and kind-specific commands | No replacement generic card                         |
| `agw resource schema --write`                                         | `agw resource schema --install`                         | Same whole-set fixed destination                    |
| `agw resource sample KIND --write PATH`                               | Unchanged                                               | `--write` remains explicitly path-valued            |

## Contract cutover

### CLI and services

1. Seat the graph fact service and approved initial query independently of rendering.
2. Add the graph command, human renderer, and JSON projector over that service.
3. Rename the explain command while retaining its existing reference service.
4. Rename schema installation mode and keep its writer service unchanged.
5. Remove the resource describe command and its now-unowned presentation service after graph parity
   tests pass.

Each commit should leave the branch's owned tests and docs coherent. The draft PR remains the single
vehicle, but the commits should be reviewable and revertible by responsibility.

### Machine output

- Remove the closed `resource.describe` command ID and payload fixture.
- Add the `graph.show` command ID with a new closed JSON v1 data shape.
- Do not reuse `resource.describe` or preserve its card-shaped payload. The graph contract is nodes
  and edges.
- Keep all unrelated command IDs and schemas unchanged.

### Completions

- Replace the `resource.describe-kind` target mapping with `resource.explain` and keep its
  config-free resource-kind completer.
- Remove `resource.describe` completion entries.
- Add graph target, kind, direction, depth, and output completion from sources that respect the
  HLA's demand boundary.
- Regenerate and verify bash, zsh, and PowerShell completion artifacts through the repository's
  normal completion gates.

### Documentation and teaching

Update active instructions in one sweep:

- `cli/command-reference.md` and its machine-output section;
- `cli/README.md` and `cli/agentworks/sample-config.toml`;
- `docs/guides/resources.md` and active platform guides;
- `docs/guides/upgrading-to-0.14.md` with the exact before-and-after map;
- capability and module READMEs, embedded guide teaching, and generated or internal hints;
- command help, errors, and examples.

Before the cutover is complete, run an active-reference sweep for every removed spelling across
tracked source, configuration, documentation, completion, and test files. Only upgrade guidance and
clearly historical records may retain a removed spelling.

Do not mechanically rewrite historical ADRs and locked SDDs. Amend one only if it is still an active
instruction whose stale command would mislead an operator.

## Verification order

1. Pin existing explain and schema-install behavior under service tests before renaming the CLI.
2. Pin current resource describe relationship facts as migration fixtures.
3. Prove the graph service produces equivalent relationship facts plus the approved traversal
   semantics.
4. Prove graph source-demand, no-write, no-secret-resolution, and no-interaction boundaries.
5. Cut command registrations, IDs, completions, and docs together.
6. Run focused tests, full repository gates, generated completion checks, and live CLI acceptance.
7. Run the required independent reviews before changing the draft PR to merge intent.

## Rollback

Before 0.14.0 ships, rollback is a normal commit revert within the draft PR or release branch. There
is no persisted-data migration and no compatibility state to unwind. If graph cannot meet its
read-only or relationship-parity requirements, revert the command cutover as a unit; do not restore
only an alias or ship both ownership models.

After release, any incompatible change to graph JSON or command grammar requires the repository's
normal versioned-output and upgrade process. That later obligation does not justify a pre-release
dual surface now.
