# Native Harness Setup Research

- Status: Local native fixtures verified; live VM acceptance pending
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

## Isolated native observations

The committed `cli/tests/test_native_harness_setup.py` fixture suite drives actual Codex 0.153.4 and
Claude Code 2.1.263 against local marketplace directories under fresh homes, with outbound proxies
blocked. It observes native files and inventories after setup, identical retry, plugin and
marketplace removal, interrupted mutation and mapped settings publication. These observations
establish the following implementation boundaries:

- Codex inventory separates available plugins from installed plugins. Installed identity includes
  marketplace source and installed version. Source metadata includes optional ref and sparse paths;
  mapped settings cannot silently add or change these for an explicit marketplace.
- Claude inventory carries user/project scope. An unowned project dependency blocks removal of an
  owned marketplace; uninstall retains plugin user data with the native keep-data option.
- Native commands can modify settings. Mapping publication preserves unrelated changes and refuses
  unexpected concurrent drift, while retaining already confirmed plugin receipts for recovery.
- Native source discovery runs in a private temporary home. Actual-user login PATH discovery occurs
  once before staging; prepared env wins. A launcher that cannot operate with an isolated HOME is
  refused without guessing its underlying installation paths.
- Missing marketplace inventory can make an installed Codex plugin unobservable. That cleanup stays
  pending with ownership evidence retained; the implementation does not infer cache paths.
- Unowned matching registrations and plugins are refused. Known owned entries reconcile, while
  changed source identity refuses cleanup until drift is resolved through the native CLI.

These are Linux local native-command observations, not live Agentworks VM acceptance. Two actual
users/workspaces, remote transports and native Windows remain separate acceptance surfaces. Native
command multi-step changes are not atomic; per-step receipts preserve the last confirmed prefix.
User-independent workspace plugin provisioning remains excluded by the approved applicability
boundary; a project settings file alone does not prove it.

## Source quality

| Source                                     | Evidence                                             | Limit                                            |
| ------------------------------------------ | ---------------------------------------------------- | ------------------------------------------------ |
| Agentworks code at `3641ea8c`              | Existing caller semantics and ownership gaps         | Does not describe new native reconciliation      |
| Local Codex and Claude CLI help            | Actual installed command spelling and options        | No mutation or scope evidence                    |
| Official native documentation linked above | Intended native plugin model                         | Requires version-specific runtime verification   |
| Isolated fixture observations              | Actual native inventories, writes, retry and removal | Linux local fixtures; live VM acceptance pending |

Artifact formats, bundle ingestion and Rulesync runtime reuse belong to the separate successor SDD.
This research does not introduce artifact mechanisms into the current framework.
