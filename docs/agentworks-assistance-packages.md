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
result, and then invokes the guide. If no compatible stable release is available, it does not
install or update the CLI or invoke the guide, and the operator should retry after the release is
published. It does not use a pre-release, a lower version, or an unpinned latest version. It does
not offer or perform repository source inspection.

The bootstrap adds no authorization, security-setting, or harness-posture teaching. Ordinary harness
approvals and restrictions apply independently of the package. The guide owns any later security or
authorization context needed for Agentworks work.

## Guide discovery and degraded context

`agw guide` without a topic is a short trail sign. Agent mode points to onboarding, management,
troubleshooting, release notes, migration, secrets, and bug reporting. Human mode offers two paths:
onboarding for a new installation and management for an existing installation. This request does not
load the topic catalogs, configuration, registry, or state database. Use shell completion or
`agw guide --names-only` to discover the complete installed topic inventory.

`concept-onboarding` is the first-setup and current-adoption destination. It contains the startup
security posture, reports available current facts, and derives the next first-VM or first-session
step. Its related topics include `concept-source-review`, which separately owns the optional focused
and full read-only source-review actions.

A selected topic always renders its installed teaching when that teaching is valid. If
configuration, the registry, the state database, or live projection is unavailable, the response
shows one warning, names the omitted topic blocks, and leaves a short placeholder at each omission.
The command exits 0 because the requested guidance rendered; use `agw doctor` to determine
installation health. Invalid topic syntax, unknown topics, malformed verification evidence, and
invalid requested guide content remain errors.

`agw guide --names-only` prints one valid topic name per line. It always returns static names and
adds live resource names when they can be established. A live-context failure omits only those
best-effort names, emits no diagnostic prose, and does not fail shell completion.

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
