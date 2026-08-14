# Migration Strategy: Built-In Installer Rows to System Plugins

- Status: Revised for artifact review
- Date: 2026-08-14
- Snapshot: `main` at `6771c02a`

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

## Operator apt overrides

Same-name row precedence remains stable, while dependencies keep their own enablement. An operator
`apt-package/gh` continues to replace the app-shipped package row. If that custom row still names
`apt-source/github-cli`, the source is now owned by `apt` and the standard recipe gate reports it as
disabled while the plugin is off.

The operator has two existing-contract choices:

1. Enable `apt`, allowing the custom package row to win while its app-shipped source dependency is
   enabled.
2. Keep `apt` disabled and replace or remove the source dependency as well. A same-name
   operator-declared source wins the disabled plugin row through the current precedence contract.

This effort adds no composite override exception or specialized message.

## Compatibility and downgrade

The operator waived the default warning runway on 2026-08-14. No compatibility alias, warning,
automatic enablement, migrator, or special remediation ships. Existing configurations receive the
standard disabled-resource message and enable the owning plugin manually.

Downgrade to 0.13 after adding either new plugin name is unsupported. This effort provides no
downgrade rewrite or compatibility test path.

## Sequencing

1. Register both descriptors; move each manifest entry to its owning package; remove the old
   built-in files; and update the payload, provider, enablement, and composite-override tests in one
   green slice.
2. Add guide-scoped content loading and operator teaching with their package, isolation, completion,
   and documentation tests in a second green slice.
3. Build and inspect the wheel, then exercise the shipped CLI with plugins disabled and enabled.

The move is atomic at PR scale. No mergeable commit publishes a selector twice or omits it.

## Risks and safeguards

| Risk                                          | Safeguard                                                                                           |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| A payload changes while moving                | Decode and compare the exact 16-entry oracle.                                                       |
| A selector has zero or two shipped providers  | Assert the exact registry roster and origin for both plugin states.                                 |
| A manifest or guide is missing from the wheel | Load both asset families from an installed wheel through package resources.                         |
| A broken guide asset affects ordinary CLI use | Keep imports I/O-free and isolate loader failures during guide-scoped catalog construction.         |
| A custom apt package retains a moved source   | Test plugin-enabled and operator-replaced dependency paths plus the standard disabled gate.         |
| Docs still teach built-in ownership           | Update source comments, sample config, CLI/plugin docs, resource guide, and upgrade guide together. |
| Completion exposes stale state                | Exercise existing resource and guide completion projections across plugin states.                   |

## Rollback

Before release, revert each plugin package, installed-index entry, and corresponding built-in file
as one unit. After 0.14 ships, downgrade and restoration of implicit built-in enablement are outside
this effort's support contract.
