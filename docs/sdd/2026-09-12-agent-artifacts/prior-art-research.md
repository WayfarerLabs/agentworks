# Agent artifacts: prior-art research

## Status and terminology decision

Research for the initial HLA, checked 2026-09-12 and 2026-09-13. It covers terminology, existing
Agentworks source handling, Rulesync reuse, and documented native delivery mechanisms. Native
behavior still needs implementation-time verification against installed versions; this is not live
acceptance evidence.

**Agents** is shorthand for **agent personas**. An agent persona is a reusable definition of an
agent's behavior, whether used for a primary agent or delegated work where the harness supports it.
The definition may include instructions and supported execution settings. This terminology does not
imply a shared standard format. Keep **subagent** for an agent executing delegated work. Use **agent
persona** and **agent resource** where the artifact and the existing Agentworks resource could
otherwise be confused.

## Findings and design implications

### Agent Skills defines the content format

The [Agent Skills specification](https://agentskills.io/specification) defines a skill as a
directory with `SKILL.md`, containing YAML metadata followed by Markdown. Name and description are
required; supporting scripts, references, assets and other files may accompany it. Its progressive
disclosure model loads metadata first, the body when activated, and supporting resources when
needed.

Decision: use standard Agent Skills content, preserving the whole package during acquisition and
normalization. Do not invent a parallel skill document format. Text normalization to Unix LF must
preserve binary supporting assets. The artifact rule definition has different loading semantics:
rule guidance is always loaded into context wherever it applies; skills disclose content as needed.

### Claude Code supports primary and delegated use of a definition

[Claude Code's subagent documentation](https://code.claude.com/docs/en/sub-agents#invoke-subagents)
documents `claude --agent <name>`: the main session adopts the selected definition's system prompt,
tool restrictions and model. The same documentation describes delegated invocation of these
definitions. Its native term therefore does not restrict the definition to delegated execution.

Implication: naming our artifact type only for subagents would encode an unnecessary restriction.
The agreed agent persona term describes reusable behavior independently of invocation position.
Exactly how Agentworks exposes selection and translates fields belongs in the support matrix and
HLA; this finding is not a promise to expose every native setting.

### Codex calls these custom agents

[Codex's custom-agent documentation](https://developers.openai.com/codex/subagents#custom-agents)
describes TOML definitions with a name, description and developer instructions, loaded as
configuration layers for spawned sessions. Supported session settings can also specify behavior,
such as model choice and sandbox configuration.

Implication: persona must mean more than tone or personality. This source establishes delegated use;
it does not establish an equivalent primary-session selection interface. Do not infer parity with
Claude Code from the shared concept. Native filenames and configuration keys keep native names.

## Refuted or unsupported claims

- A definition called a subagent by a harness can only configure delegated work: contradicted by
  Claude Code's primary-session support.
- Persona means only personality or writing style: too narrow for the documented behavioral and
  execution settings. Our proposed definition explicitly includes those settings.
- There is a portable standard persona format analogous to Agent Skills: not established here.
- Writing a file proves its content entered model context: not established. The support matrix must
  distinguish native loading from shell's explicit filesystem delivery contract.

## Recovered acquisition safety design

The [seed-time review](https://github.com/WayfarerLabs/agentworks/pull/794#issuecomment-5647882851)
identified acquisition work removed when the predecessor deferred artifacts. The primary historical
source is its
[HLA at commit 5fd6a542, lines 613-700](https://github.com/WayfarerLabs/agentworks/blob/5fd6a542/docs/sdd/2026-09-06-harness-scope-framework/hla.md#L613-L700).
That commit is reachable from merged main. It can also be read locally:

```sh
git show 5fd6a542:docs/sdd/2026-09-06-harness-scope-framework/hla.md
```

These are recovered design constraints, not implemented behavior or new live-test evidence. The FRD
carries their safety requirements forward. This record preserves the concrete cases and the former
design choices that the bundle ingestion design must account for.

| Concern                 | Recovered constraint and design consequence                                                                                                                                                                                             |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Revision consistency    | Resolve a repository/reference once per capture to an immutable commit; every selected path uses it. Record requested ref and resolved commit without credentials.                                                                      |
| Non-executing Git reads | Use workstation authentication and committed tree contents without repository hooks, checkout filters or export substitutions. Do not use guest credentials.                                                                            |
| Complete selection      | Do not honor `export-ignore` by silently dropping selected members. Reject selected submodules and unresolved LFS pointers.                                                                                                             |
| File metadata           | Preserve relative paths and executable intent from Git/local file modes; include them in normalized content identity.                                                                                                                   |
| Repository metadata     | A Git object reader does not export `.git`. Reject Git metadata actually present in a selected local package instead of silently excluding it.                                                                                          |
| Package boundaries      | Explicit skill root, full standard metadata and supporting files; no recursive skill discovery. Refuse links, special files, absolute/escaping paths and portable-path collisions. Never execute skill scripts to validate the package. |
| Text normalization      | Normalize hint/rule text and `SKILL.md` to UTF-8/LF, including CRLF and lone CR, before validation, hashing and delivery. Do not rewrite the source. LF is Agentworks policy, not an Agent Skills requirement.                          |
| Byte preservation       | Normalize supporting files only under the explicit text classifier below. Keep unknown formats opaque even if UTF-8 decoding succeeds. The ASCII-only PDF case is a required preservation fixture.                                      |
| Stable capture          | Validate before native writes; share captured content across integrations and downstream scopes. No live source mounts or repeated fetching by integrations. A failed capture cannot claim an old snapshot as newly captured.           |
| Bounded lifecycle       | Limit acquisition time, storage, member count, member/total size and depth; reject observed local mutation and clean temporary storage on both success and failure.                                                                     |

The former classifier recognized `.md`, `.txt`, `.py`, `.sh`, `.bash`, `.zsh`, `.ps1`, `.js`,
`.mjs`, `.cjs`, `.ts`, `.json`, `.jsonc`, `.yaml`, `.yml` and `.toml`, and required valid UTF-8
without NUL. Other supporting members stayed byte-for-byte intact, including scripts without
filename extensions. Exact contained paths in the former `preserve_bytes` control could exempt
recognized supporting files, but never `SKILL.md`. Preserve that byte-sensitive fixture use case
when choosing the new bundle configuration shape; do not treat a successful text decode as
permission to rewrite an unknown format. The persisted normalized representation must preserve both
opaque bytes and text.

The old source spelling, declaration map, refresh schedule and snapshot codec are historical
proposals. The [HLA](hla.md) makes its own current choices under the new `artifact-bundle` resource;
recovering the safety constraints did not itself approve the old interfaces. Likewise, the old
helper names are research leads, not a claim that current transfer helpers implement package
capture.

## Existing source and inspection boundaries

At `1e11e6ee`, [sources.py](../../../cli/agentworks/sources.py) supplies `SourceRef` and parsing for
workstation paths, `file::` and Git references with a subpath/revision. Its
`snapshot_workstation_file` is a precedent for reading bytes on the invoking workstation before
native writes. Reuse these conventions and shared primitives where their contracts fit.

The dotfiles transfer functions have a different contract. `fetch_dir` ignores Git subpaths, and its
Git path operates on a mutable guest checkout. Agent dotfiles can execute an installer. The simple
workstation snapshot helper follows links and does not capture a directory's executable metadata or
detect observed mutation across a package capture. Those behaviors cannot implement the FRD's
acquisition boundary unchanged. Extend common source handling with validated package capture; do not
call the dotfiles installation workflow as artifact ingestion or change its existing behavior
incidentally. These are code observations, not live acquisition tests.

[agw env show](../../../cli/agentworks/cli/commands/env.py) provides familiar scope selectors and a
thin command over a typed service. Its [service](../../../cli/agentworks/env/show.py) shows resolved
env winners, accepts overrides of inferred parents, and supplies admin env without an agent. Those
semantics are not artifact routing: artifacts need actual parent relationships, all relevant
origins, and declared versus applied evidence. Reuse resource resolution and the evidence concepts
in [instance_description.py](../../../cli/agentworks/instance_description.py) and
[instance state](../../../cli/agentworks/db/instance_state.py), with a dedicated artifact
projection.

## Rulesync reuse decision

[Rulesync's source declarations](https://rulesync.dyoshikawa.com/guide/declarative-sources.html)
separate acquisition from generation and record resolved revisions. Its source-selection and
distribution examples are useful prior art; this HLA chooses workstation and Git inputs, leaving
packaged distributions as a later reader of the same normalized form.

[File formats](https://rulesync.dyoshikawa.com/reference/file-formats) provide a useful common
persona model: name, description, instruction body and tool-specific options. However, local source
discovery follows links, remote discovery can skip links, and its own precedence/cleanup choices
differ from this FRD's complete-package refusal and owner routing. Reuse the concepts and standard
skill content, not those ingestion or lifecycle semantics. Full Rulesync format compatibility is not
implied.

The 2026-09-14 operator-approved authoring amendment adopts optional rule `description` frontmatter
and strict rule/persona field validation. Rulesync's rule schema also supports `globs`, `targets`,
`root`, `localRoot` and tool-specific blocks. Those do not fit this delivery's always-loaded rules
and facet-owned placement. Its persona model retains a shared body and tool-specific options;
Agentworks keeps that model with explicit `native_options` keyed by integration, rather than
introducing portable model or tool names. Agent Skills content remains standard. See the
[rule schema](https://github.com/dyoshikawa/rulesync/blob/main/src/features/rules/rulesync-rule.ts)
and
[persona schema](https://github.com/dyoshikawa/rulesync/blob/main/src/features/subagents/rulesync-subagent.ts).

The [programmatic API](https://rulesync.dyoshikawa.com/api/programmatic-api) is a Node/TypeScript
generation API. Calling it would introduce a runtime boundary and still require Agentworks-owned
acquisition, scope routing and applied-state handling. The HLA therefore proposes small native
adapters in the existing Python integrations, without a Rulesync runtime dependency. Revisit shared
generation code only if a concrete adapter warrants it; a broad import/export service is not part of
this delivery.

## Native delivery findings

### Claude Code

[Memory documentation](https://code.claude.com/docs/en/memory) supports unconditional Markdown rules
in user and project `.claude/rules` directories when conditional paths are omitted.
[Skills documentation](https://code.claude.com/docs/en/skills) covers user, project and plugin Agent
Skills. The [CLI reference](https://code.claude.com/docs/en/cli-reference) documents a session-only
plugin directory and an additive system-prompt file. These allow private session publication without
installing a project plugin or modifying shared settings.

Plugin skills acquire a namespace; that visible name must be reported rather than silently
pretending it is unchanged. Session personas can use `--agents` JSON, avoiding plugin-specific
persona-field omissions. The subagent documentation cited above establishes primary and delegated
use, but does not make every native option portable.

Resume can reuse a saved system prompt. The documented `--system-prompt-snapshot off` changes that
behavior; the native compatibility tests must establish the supported version and prove changed
rules take effect. Merely rewriting an appended prompt file is insufficient evidence.

### Codex

[AGENTS.md discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md) has override
files and a combined size limit, so writing an extra user/project file is not an additive rule
mechanism. The
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference.md) documents
`developer_instructions` for additional guidance and `agents.<name>.config_file` for a role's TOML
file. Per-invocation overrides can therefore carry rule text and point at private persona files;
native verification still gates implementation claims.

[Skills](https://learn.chatgpt.com/docs/build-skills) are discovered under user and repository
`.agents/skills`. The [official schema](https://developers.openai.com/codex/config-schema.json)
describes `skills.config.path` as a selector, not an arbitrary discovery path. No session-only skill
discovery mechanism is established here. The HLA treats that placement as unsupported instead of
simulating a native skill through prompt text.

[CODEX_HOME](https://learn.chatgpt.com/docs/config-file/environment-variables) redirects
credentials, sessions and other state as well as configuration; using it as an artifact-only root
would hide a larger lifecycle change. Native `.codex/rules` files govern execution policy, not the
context rules defined in this SDD. Neither mechanism is the chosen artifact delivery path.

### Grok Build

The shipped integration targets [xai-org/grok-build](https://github.com/xai-org/grok-build).
[Project rules](https://docs.x.ai/build/features/project-rules) document user/project discovery and
gitignore exclusions.
[Skills and plugins](https://docs.x.ai/build/features/skills-plugins-marketplaces) document
user/project skill directories, with some metadata fields not enforced. Preserve standard skill
content, and do not promise those fields enforce runtime permissions.

The [CLI reference](https://docs.x.ai/build/cli/reference) documents additive `--rules` text. The
[CLI source](https://github.com/xai-org/grok-build/blob/37949780c144e37df692e3d669051a21fec24f20/crates/codegen/xai-grok-pager/src/app/cli.rs)
exposes top-level `--agent` and `--agents`, but the inspected `--plugin-dir` declaration belongs to
the ACP subcommand. Broad plugin documentation alone does not establish private skill loading in the
interactive command Agentworks launches. The HLA leaves interactive session skills unsupported until
that exact entry point is proven, and requires native verification of persona injection.

## Remaining native verification and detailed design

- Verify the matrix against installed native versions, including resume, discovery exclusions,
  persona options and Claude plugin skill names. No model/API calls were made for this research.
- Specify the supported persona option schemas and normalized codec, preserving the recovered
  acquisition constraints and byte-sensitive fixtures.
- Keep Codex and Grok interactive session skill delivery unsupported unless a documented and tested
  native mechanism is established. Do not redirect their whole user home or emulate native skills.

## Sources

| Source                                                              | Quality and angle                                                       |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| [Agent Skills specification](https://agentskills.io/specification)  | Primary standard; skill package structure and loading model.            |
| [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) | Primary product documentation; primary and delegated use.               |
| [Codex subagents](https://developers.openai.com/codex/subagents)    | Primary product documentation; custom definitions for spawned sessions. |
