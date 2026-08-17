# Agentworks Assistance Packaging Low-Level Design

- Status: Current corrected destination
- FRD: `frd.md` R6
- HLA: `hla.md`, Bootstrap projection
- Plan: Phase 4 correction

The immutable implementation journey is in `plan.md`. This LLD describes only the current package
and projection contract.

## Purpose and limits

Agentworks ships one short prompt for any capable external assistant plus optional native Claude
Code and Codex packages. The prompt identifies the product, points to the public repository,
installs `agentworks-cli>=0.14`, and hands continuing assistance to `agw guide --agent`.

The bootstrap is not an installer framework, version resolver, prerelease policy, prompt parser,
source-review workflow, authorization lesson, or assistant state machine. Ordinary package
resolution chooses a compatible release. The installed CLI and its help own all later behavior.

## Canonical inputs

`packaging/agentworks/assistance.md` is the only authored bootstrap body. It is LF-terminated UTF-8
Markdown without frontmatter. Its content remains concise and contains:

- a description of Agentworks as a CLI for development environments, workspaces, and coding-agent
  sessions;
- the public repository URL;
- the `agentworks-cli` PyPI identity and Python 3.12 minimum;
- `uv` as the recommended tool installer, without excluding other Python tool installers;
- `uv tool install --upgrade 'agentworks-cli>=0.14'`; and
- the `agw guide --agent` handoff.

`packaging/agentworks/metadata.json` owns package identity and machine metadata. The current package
version is `1.0.1`; the minimum CLI version is `0.14.0`. The package version changes whenever an
installed generated artifact changes and remains independent of the CLI version.

## Generated outputs

`scripts/generate-agentworks-package.py` owns these outputs:

1. `.claude-plugin/marketplace.json`;
2. `.agents/plugins/marketplace.json`;
3. `plugins/claude-code/agentworks/.claude-plugin/plugin.json`;
4. `plugins/claude-code/agentworks/skills/agentworks/SKILL.md`;
5. `plugins/codex/agentworks/.codex-plugin/plugin.json`;
6. `plugins/codex/agentworks/skills/agentworks/SKILL.md`; and
7. the marked assistance region in `README.md`.

The two generated skill bodies are byte-identical to `assistance.md` after their generated
frontmatter. The README uses a generated outer backtick fence longer than any run in the canonical
body, preserving the body bytes exactly. The website builder reads the same canonical file and fails
unless the README projection has byte parity; generated website output is disposable and stays
outside the repository.

The generator has normal write mode and `--check`. It validates metadata, generated-root inventory,
README marker cardinality and order, UTF-8 and LF framing, and exact output bytes. Writes are
atomic. It performs no network or process work and does not parse the prompt's meaning.

## Native packages

Both native packages use the neutral name `agentworks` and install identity `agentworks@agentworks`.
They contain one skill plus the harness manifest, with no hooks, commands, MCP servers, apps,
scripts, or pre-approved tools. Loading a package grants no permission.

Claude Code installation uses the explicit HTTPS repository URL:

```shell
claude plugin marketplace add https://github.com/WayfarerLabs/agentworks.git
claude plugin install agentworks@agentworks
```

Codex installation uses the repository marketplace:

```shell
codex plugin marketplace add WayfarerLabs/agentworks
codex plugin add agentworks@agentworks
```

Claude and Codex manifest descriptions, publisher data, interface fields, category, installation
policy, and authentication policy are generated from `metadata.json`. Both skill frontmatter blocks
carry package version `1.0.1` and minimum CLI version `0.14.0`.

## Structural safeguards

Repository tests prove:

- generator write and check modes are deterministic and check mode is read-only;
- every generated path and generated-root inventory is exact;
- both skills and the README contain the canonical body bytes;
- the website projects the same source bytes after HTML escaping;
- package manifests and marketplaces retain the required neutral identities and Codex policy;
- the packages contain no executable extension surface;
- metadata rejects unknown fields and invalid versions;
- changed installed package fingerprints require a strictly increasing package version; and
- malformed markers, stale outputs, unexpected files, or failed atomic replacement fail closed.

Tests protect these structures and projection relationships. They do not assert or blacklist the
authored prompt's sentences.

## Documentation and configuration impact

`README.md`, `docs/agentworks-assistance-packages.md`, `cli/README.md`, and `website/README.md`
describe the short install-and-handoff contract. The guide and completion contracts are documented
with the CLI, not copied into the bootstrap.

The package adds no Agentworks setting. `cli/agentworks/sample-config.toml` is unaffected.
