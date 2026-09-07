# Native Harness Setup Research

- Status: Implementation research in progress
- Checked: 2026-09-07
- Scope: native user plugins/marketplaces and native user/project settings

## Findings and implementation consequences

The existing Agentworks Claude helper performs add/install commands and warns on failure. It offers
no ownership probes or uninstall evidence (`cli/agentworks/vms/initializer/driver.py:764` at the
implementation baseline). The new reconciliation therefore needs measured native identities and side
effects; successful legacy commands cannot establish ownership.

The locally installed Codex CLI is 0.153.4 and Claude Code is 2.1.263. Read-only help inspection
confirms these native surfaces:

| Tool        | Query                                                  | Provision                                               | Remove                                          |
| ----------- | ------------------------------------------------------ | ------------------------------------------------------- | ----------------------------------------------- |
| Codex       | `plugin list --json`, `plugin marketplace list --json` | `plugin add`, `plugin marketplace add`                  | `plugin remove`, `plugin marketplace remove`    |
| Claude Code | `plugin list --json`, `plugin marketplace list --json` | `plugin install --scope user`, `plugin marketplace add` | `plugin uninstall`, `plugin marketplace remove` |

Codex add/remove accepts a qualified plugin selector and supports JSON results. Its marketplace
commands distinguish configured sources from available plugin entries. Do not treat every available
entry as installed. Claude's existing explicit user install flag is retained. Help output
establishes command syntax, not native applicability, repeated-operation behavior or a stable JSON
schema.

Official [Codex plugin documentation](https://learn.chatgpt.com/docs/plugins) describes marketplace
installation and enablement. The
[Claude plugin reference](https://code.claude.com/docs/en/plugins-reference) describes native plugin
components and management. These support using native commands rather than inventing an Agentworks
plugin package mechanism. Neither substitutes for isolated observations of commands writing native
config and caches.

Settings policies require semantic parsing/serialization, not preservation of comments. Python's
stdlib JSON and TOML readers cover parsing; a TOML serializer is needed for the mapped document. The
settings implementation selects a maintained serializer after checking its current stable package
release instead of hand-writing TOML escaping.

## Not established by current evidence

- User-independent workspace plugin provisioning: still excluded by the approved applicability
  boundary. A project settings file alone does not prove it.
- Exact native list payload fields, registration source normalization and installed package
  identity.
- Whether native removal changes settings outside the explicit managed association or affects
  dependencies used by unowned plugins.
- Atomicity and file preservation across native add/remove commands and the mapped settings write.

These questions need local fixture observations before runtime reconciliation relies on them. Use
separate scratch native homes, local marketplace repositories and two users/workspace contexts as
applicable. Do not query or change the operator's installed plugins as test fixtures.

## Source quality

| Source                                     | Evidence                                      | Limit                                           |
| ------------------------------------------ | --------------------------------------------- | ----------------------------------------------- |
| Agentworks code at `3641ea8c`              | Existing caller semantics and ownership gaps  | Does not describe new native reconciliation     |
| Local Codex and Claude CLI help            | Actual installed command spelling and options | No mutation or scope evidence                   |
| Official native documentation linked above | Intended native plugin model                  | Requires version-specific runtime verification  |
| Isolated fixture observations              | Pending                                       | Must be recorded before native setup acceptance |

Artifact formats, bundle ingestion and Rulesync runtime reuse belong to the separate successor SDD.
This research does not introduce artifact mechanisms into the current framework.
