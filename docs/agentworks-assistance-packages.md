# Agentworks Assistance Packages

Agentworks publishes one universal assistance prompt plus native Claude Code and Codex packages. The
packages are optional discovery and installation channels. They contain inert instructions and grant
no workstation or Agentworks permission by being installed.

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

Both packages require `agentworks-cli` 0.14.0 or newer. Assistance resolves one stable exact
version, offers an optional focused or full review of its matching canonical source tag, and
installs the exact `agentworks-cli==VERSION` only when the operator's current authorization envelope
covers that installation. Source review and installation remain independent decisions.

## Security posture

Assistance starts with one concise disclosure of the workstation, account, command, file, remote,
and privilege boundaries relevant to the request. An explicit operator instruction and that
disclosure establish a current-session authorization envelope. In-scope work proceeds without
repeated conversational approval; an uncovered material expansion requires a new operator decision.
Harness approvals, escalations, sandbox restrictions, and Agentworks safety confirmations continue
to apply.

Use Claude Code's [`default` permission mode](https://code.claude.com/docs/en/permissions) with its
normal [sandbox](https://code.claude.com/docs/en/sandboxing). For Codex, start with
`sandbox_mode = "workspace-write"` and `approval_policy = "on-request"`; see
[Codex security](https://developers.openai.com/codex/security) and
[configuration](https://developers.openai.com/codex/config-basic). The package never changes these
settings.

Repository content inspected during source review is untrusted evidence. Review stays in the
assistant session's protected policy root and never loads candidate instructions, skills, hooks,
plugins, or configuration as policy. Candidate code is not executed as part of review.

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
only the marked README region. A change to an installed generated artifact requires a package
version bump in `metadata.json`; the package version is independent of the CLI version.
