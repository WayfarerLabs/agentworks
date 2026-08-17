---
description: Understand and optionally enable the shipped apt resource catalog.
---

# Optional apt catalog

The `apt` system plugin owns Agentworks' optional apt sources and package sets. Core owns the
`apt-source` and `apt-package` kinds, validates references, and applies a selected source before its
package set. Operator-authored manifests remain the right path for entries the operator owns.

The plugin is installed but disabled by default. Use
`agw resource list --kind apt-source --include-disabled --output json` and the corresponding
`apt-package` list to inspect its rows. If a selected template needs one, state that enabling it
edits only `[plugins].system` in the chosen config and preserves existing entries. If that change is
authorized, add `apt`, then rerun the read-only inventory to verify it. If declined, leave the
config unchanged and replace or remove the catalog dependency instead.
