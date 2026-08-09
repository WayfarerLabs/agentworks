# Migration strategy: built-in installer rows to system plugins

- Status: Independent artifact review clean, pending roadmap review
- Date: 2026-08-08
- Snapshot: `main` at `615aa0da`

## Current and target state

Agentworks currently publishes 16 optional installer rows as built-ins:

- five `apt-source` rows and five dependent `apt-package` rows
- six `user-install-command` rows

The target changes only their provider:

| Rows                                   | Current provider          | Target provider                                  |
| -------------------------------------- | ------------------------- | ------------------------------------------------ |
| Five apt sources and five apt packages | Built-in manifest package | `apt` system plugin manifest package             |
| Six user install commands              | Built-in manifest package | `install-command` system plugin manifest package |

Names, specs, dependency edges, template fields, and execution paths are stable. There is no data
rewrite and no compatibility alias because the selectors do not change.

The universal C4 foundation also changes replacement behavior for all currently shipped declarable
rows. Today an operator declaration can silently win over the 16 built-in allow-kind rows and over a
disabled plugin manifest. After this effort, each same-name operator declaration requires the exact
`resource_policy.disabled` selector.

The existing plugin-manifest collision inventory is:

| Plugin   | Selectors affected when an operator declares the same name                                                                                                                               |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `azure`  | `system-install-command/az-cli`                                                                                                                                                          |
| `claude` | `agent-template/example-claude`, `user-install-command/claude`, `session-template/example-claude-strict`, `session-template/example-claude-auto`, `session-template/example-claude-yolo` |
| `codex`  | `agent-template/example-codex`, `user-install-command/codex`, `session-template/example-codex-strict`, `session-template/example-codex-auto`, `session-template/example-codex-yolo`      |

The 16 rows listed in the inventory are also affected while they still publish as built-ins. Other
built-ins use reserved names or separately declared synthesized-default semantics and do not acquire
a silent replacement path.

## Operator transition

A 0.13-shaped resource declaration remains valid. The operator adds the owning plugin to the
settings list before using a moved selector.

For example, this VM template keeps its existing field and value:

```yaml
apiVersion: agentworks/v1
kind: vm-template
metadata:
  name: dev
spec:
  apt_packages:
    - gh
```

Its settings add `apt`:

```toml
[plugins]
system = ["apt"]
```

Likewise, an admin or agent template that already selects `uv` keeps that selection and enables
`install-command`:

```toml
[plugins]
system = ["install-command"]
```

When other plugins are already enabled, the diagnostic and upgrade guide show the complete
replacement list rather than a fragment. For example:

```toml
[plugins]
system = ["onepassword", "claude", "apt", "install-command"]
```

No migrator edits operator files. A missing opt-in fails before mutation and names the selected
resource, its owning plugin, and the exact settings line.

## Explicit replacement

An operator manifest may keep a moved name only after explicitly disabling the shipped provider:

```toml
[resource_policy]
disabled = ["apt-package/gh"]
```

```yaml
apiVersion: agentworks/v1
kind: apt-package
metadata:
  name: gh
spec:
  apt:
    - gh-custom
```

Without the policy selector, the collision is a hard error. With it, the operator row is active and
the displaced plugin provider remains visible in describe and doctor provenance. Merely leaving the
`apt` plugin disabled does not authorize replacement.

## Sequencing

1. Land the generic resource-policy, collision, provenance, and enabled-edge invariants while the 16
   rows still publish as built-ins.
2. Add `apt`, copy the ten apt rows byte-for-byte at the manifest-entry level, remove them from the
   built-in package, and update the apt-facing permanent docs in the same change.
3. Add `install-command`, copy the six command rows byte-for-byte at the manifest-entry level,
   remove them from the built-in package, and update the command-facing permanent docs in the same
   change.
4. Run cross-plugin completion, package-data, upgrade, isolated-home CLI, and live create/reinit
   validation.

Each row has exactly one app-shipped provider at every mergeable commit. Tests compare the decoded
target entries with the dated source snapshot so command quoting, URLs, paths, and installed checks
cannot drift during the move.

## Compatibility boundaries

The migration does not change:

- VM, admin, or agent template schemas
- apt or install-command kinds
- install predicate semantics
- Phase A or Phase B ordering
- system, admin, or agent execution
- PATH and rc behavior
- mise or tmuxinator defaults
- snap, mise, dotfiles, tmuxinator, or Claude ownership

The intentional breaking changes are the new plugin opt-in for the 16 moved rows and the explicit
disable requirement for any same-name operator replacement in the inventory above. The 0.14 upgrade
guide lists every affected selector and is the durable migration surface.

## Risks and safeguards

| Risk                                                      | Safeguard                                                                                      |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| A row is lost or changed during relocation                | Exact roster and decoded-entry parity tests cover all 16 selectors.                            |
| A source row becomes enabled while its plugin is disabled | Plugin opt-in marks all manifest rows and finalize rejects enabled referrers.                  |
| An operator silently replaces a shipped provider          | Complete claim collection and one resolution pass require explicit disable.                    |
| Existing same-name operator rows fail after upgrade       | The upgrade guide inventories all 27 affected selectors and shows the exact policy entry.      |
| Disable provenance disappears after replacement           | Frozen substitution records retain the displaced origin and authorizing mark.                  |
| Source checkout passes while wheel data is absent         | Build and install the wheel, then load both YAML and guide Markdown through package resources. |
| Docs teach stale built-in ownership                       | Permanent docs ride the behavior change, with sample-config and roster drift tests.            |

## Rollback posture

Before release, reverting a plugin move restores the corresponding built-in file and removes that
plugin module in the same commit. After 0.14 ships, restoring implicit built-in enablement would
reverse an operator-facing contract and requires a new compatibility decision rather than a silent
rollback.
