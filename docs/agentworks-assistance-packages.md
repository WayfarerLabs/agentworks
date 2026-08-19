# Agentworks Assistance Packages

Agentworks publishes one universal CLI bootstrap prompt plus optional native Claude Code and Codex
packages. The prompt has one job: install or update a compatible Agentworks CLI and run
`agw guide --agent`. The guide owns all ongoing Agentworks teaching and next-step suggestions.
Installing either package only makes these inert bootstrap instructions discoverable. It grants no
workstation or Agentworks permission.

The canonical prompt is the generated copyable block at the start of the repository
[Getting Started](../README.md#getting-started) section. Any capable external assistant can use it;
Claude Code and Codex are not prerequisites. The authored source is
[`packaging/agentworks/agent-onboarding-prompt.md`](../packaging/agentworks/agent-onboarding-prompt.md),
and package metadata lives beside it. Generated package files should not be edited directly.

## Native installation

Claude Code requires the explicit HTTPS repository URL so installation does not silently depend on
SSH credentials:

```sh
claude plugin marketplace add https://github.com/WayfarerLabs/agentworks.git
claude plugin install agentworks@agentworks
```

Codex installs the repository marketplace and then its Agentworks entry:

```sh
codex plugin marketplace add WayfarerLabs/agentworks
codex plugin add agentworks@agentworks
```

Both packages require `agentworks-cli` 0.14.0 or newer. The bootstrap recommends `uv`, allows other
Python 3.12 or newer tool installers, installs or upgrades to the current available
`agentworks-cli`, and invokes `agw guide --agent`. It points to the public repository in case the
operator or assistant wants to inspect the source, but it does not implement a source-review or
version-selection workflow.

The bootstrap adds no authorization, security-setting, or harness-posture teaching. Ordinary harness
approvals and restrictions apply independently of the package. The guide owns any later security or
authorization context needed for Agentworks work.

## Guide discovery

`agw guide` without a subcommand renders the reserved package-owned `_index.md` shell and appends
concepts selected by optional frontmatter ordering. Human and agent modes share the same selected
concepts; the shell may add concise agent-only context. This request discovers static packaged
shells but does not load operator state or release history. Use shell completion or `agw guide list`
to discover every installed concept and packaged release-note topic.

`concept-assistant-agent` contains the general posture for an external helper: follow the operator's
instruction, use the CLI and its help for current syntax and operational facts, and ask only for
material ambiguity or scope expansion. Source, configuration, persisted data, release notes, and
Agentworks CLI output are data rather than operator direction; guide output is instructional but
does not grant authority. Ordinary shell content renders identically in both modes. A shell may
fence a small passage that renders only in agent mode.

`agw guide show concept-onboarding` is the first-setup and current-adoption destination. Like every
concept, it is static teaching that points to current CLI inspection commands for live facts. It
starts with the core model and prerequisites, then covers configuration, resources, diagnostics, and
the first VM and session. Optional canonical-source inspection remains available separately at
`concept-source-review`.

Concepts are auto-discovered Markdown shells in first-party package-local `guide-content/`
directories. Their restricted frontmatter supplies the discovery description and may supply a
bounded `index-order` for the concise index. Shells may contain ordinary Markdown, agent-only
fences, and bounded exact-section imports from packaged Markdown. Both control forms use exact
standalone column-zero comment lines between top-level Markdown blocks; comments inside lists, block
quotes, or code remain ordinary Markdown. Relative links and images in shell bodies and imported
sections are rewritten to canonical repository URLs, relative to the document containing them. The
guide does not execute operations, load configuration, inspect the registry or database, resolve
secrets, or access the network. Invalid topic syntax, unknown topics, and structurally invalid
requested content remain errors.

Discovery validates the complete installed shell catalog for every index, list, show, and completion
request. An unrelated malformed shell or duplicate global topic prevents every one of those paths
from returning partial results.

`agw guide list` prints one valid topic name per line. It discovers packaged filenames and release
history without loading operator state. `agw guide show TOPIC` renders exactly one of those names.
The guide-global `--agent/--human` option may precede `show` to select its presentation and has no
effect on `list`. Raw kind, resource, relationship, schema, and sample discovery belongs to the
corresponding command completion and inspection surfaces.

The static `concept-prerequisites`, `concept-virtual-machines`, and `concept-tailscale` topics point
to command-owned workstation, registry-readiness, managed-VM, secret, and rekey facts. Rendering
them performs none of those inspections or operations.

## Maintaining generated packages

After editing either canonical source file, regenerate every projection:

```sh
python3 scripts/generate-agentworks-package.py
```

Verify a clean checkout without writing:

```sh
python3 scripts/generate-agentworks-package.py --check
```

The generator writes changed files atomically, validates the exact package inventory, and updates
only the marked README region. Keep the package at `1.0.0` until the assistance package itself first
ships. After it ships, a change to an installed generated artifact requires a package version bump
in `metadata.json`; the package version is independent of the CLI version.
