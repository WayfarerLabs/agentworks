# Agentworks Assistance Packages

Agentworks publishes one universal CLI bootstrap prompt plus optional native Claude Code and Codex
packages. The prompt has one job: install or update a compatible Agentworks CLI, verify it, and run
`agw guide --agent`. The guide owns all ongoing Agentworks teaching and next-step suggestions.
Installing either package only makes these inert bootstrap instructions discoverable. It grants no
workstation or Agentworks permission.

The canonical prompt is the generated copyable block at the start of the repository
[Getting Started](../README.md#getting-started) section. Any capable external assistant can use it;
Claude Code and Codex are not prerequisites. The authored source is
[`packaging/agentworks/assistance.md`](../packaging/agentworks/assistance.md), and package metadata
lives beside it. Generated package files should not be edited directly.

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

Both packages require `agentworks-cli` 0.14.0 or newer. The bootstrap retains a compatible
installation unless an update was requested. Otherwise it selects one compatible stable exact
version at least 0.14.0, runs the pinned `agentworks-cli==VERSION` installation, verifies the
result, and then invokes the guide. It does not offer or perform repository source inspection.

The bootstrap adds no authorization, security-setting, or harness-posture teaching. Ordinary harness
approvals and restrictions apply independently of the package. The guide owns any later security or
authorization context needed for Agentworks work.

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
only the marked README region. A change to an installed generated artifact after the package ships
requires a package version bump in `metadata.json`; the package version is independent of the CLI
version.
