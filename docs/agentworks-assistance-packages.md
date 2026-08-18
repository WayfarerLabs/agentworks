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

## Guide discovery and degraded context

`agw guide` without a topic is a short trail sign. Human and agent modes show the same eight
destinations: assistant-agent guidance, onboarding, management, troubleshooting, release notes,
migration, secrets, and bug reporting. Agent mode first points to `concept-assistant-agent`; human
mode asks the operator to choose a goal. This request does not load the topic catalog or operator
state. Use shell completion or `agw guide --names-only` to discover every installed concept and
packaged release-note topic.

`concept-assistant-agent` contains the general posture for an external helper: follow the operator's
instruction, use current CLI help as operational authority, ask only for material ambiguity or scope
expansion, and treat external text as data. Ordinary shell content renders identically in both
modes. A shell may fence a small passage that renders only in agent mode.

`concept-onboarding` is the first-setup and current-adoption destination. Like every concept, it is
static teaching that points to current CLI inspection commands for live facts. It includes
`concept-source-review` as the optional path for inspecting canonical source before installation.

Concepts are auto-discovered Markdown shells in first-party package-local `guide-content/`
directories. Their restricted frontmatter supplies the discovery description. Shells may contain
ordinary Markdown, agent-only fences, and bounded exact-section imports from packaged Markdown. Both
control forms use exact standalone column-zero comment lines between top-level Markdown blocks;
comments inside lists, block quotes, or code remain ordinary Markdown. Imports preserve relative
links and images by rewriting them to canonical repository URLs. The guide does not execute
operations, load configuration, inspect the registry or database, resolve secrets, or access the
network. Invalid topic syntax, unknown topics, and structurally invalid requested content remain
errors.

`agw guide --names-only` prints one valid topic name per line. It discovers packaged filenames and
release history without loading operator state. Raw kind, resource, relationship, schema, and sample
discovery belongs to the corresponding command completion and inspection surfaces.

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
only the marked README region. Keep the package at `1.0.0` until its first compatible CLI release.
After the package ships, a change to an installed generated artifact requires a package version bump
in `metadata.json`; the package version is independent of the CLI version.
