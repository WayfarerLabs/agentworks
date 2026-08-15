The `apt` system plugin owns Agentworks' shipped optional apt catalog: five apt sources and the five
package sets that depend on them. Core still owns the `apt-source` and `apt-package` kinds,
validates references, and installs each selected source before its package set.

The plugin is installed but disabled by default. Enable it only when a VM template selects one of
its catalog entries. Operator-declared YAML remains the right path for entries you own yourself.
