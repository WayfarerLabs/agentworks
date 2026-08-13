# Migration Strategy: Built-In Installer Rows to System Plugins

- Status: Draft for artifact review
- Date: 2026-08-13
- Snapshot: `main` at `c7093147`

## Provider change

| Rows                                   | Current provider          | Target provider                 |
| -------------------------------------- | ------------------------- | ------------------------------- |
| Five apt sources and five apt packages | Built-in manifest package | `apt` system plugin             |
| Six user install commands              | Built-in manifest package | `install-command` system plugin |

Names, payloads, references, template fields, and execution paths remain stable. Only the provider,
origin, default enablement, and packaged file location change.

## Operator transition

A VM template that selects `apt-package/gh` keeps its current YAML:

```yaml
spec:
  apt_packages:
    - gh
```

Its Agentworks settings enable the owning plugin:

```toml
[plugins]
system = ["apt"]
```

Admin or agent templates that select any moved user install command keep their current YAML and
enable the other plugin:

```toml
[plugins]
system = ["install-command"]
```

Operators using both families enable both, preserving any other enabled plugin names:

```toml
[plugins]
system = ["apt", "install-command"]
```

No file is rewritten automatically. Current disabled-plugin diagnostics remain unchanged; the 0.14
upgrade guide is the durable inventory and remediation surface.

## Sequencing

1. Register both descriptors and add their guide content.
2. Move each manifest entry to its owning package and remove the old built-in files in the same
   change.
3. Update tests and permanent documentation in the same behavior PR.
4. Build and inspect the wheel, then exercise the shipped CLI with plugins disabled and enabled.

The move is atomic at PR scale. No mergeable commit publishes a selector twice or omits it.

## Risks and safeguards

| Risk                                          | Safeguard                                                                                           |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| A payload changes while moving                | Decode and compare the exact 16-entry oracle.                                                       |
| A selector has zero or two shipped providers  | Assert the exact registry roster and origin for both plugin states.                                 |
| A manifest or guide is missing from the wheel | Load both asset families from an installed wheel through package resources.                         |
| Docs still teach built-in ownership           | Update source comments, sample config, CLI/plugin docs, resource guide, and upgrade guide together. |
| Completion exposes stale state                | Exercise existing resource and guide completion projections across plugin states.                   |

## Rollback

Before release, revert each plugin package, installed-index entry, and corresponding built-in file
as one unit. After 0.14 ships, restoring implicit built-in enablement changes an operator-facing
contract and requires an explicit compatibility decision.
