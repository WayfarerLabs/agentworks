# Agent artifacts: prior-art research

## Status and terminology decision

Terminology research, 2026-09-12. This first pass addresses the operator's naming questions and the
skill content standard. Acquisition options, Rulesync reuse and the complete native support matrix
remain open research work. No runtime dependency, wire schema or implementation is approved.

The operator accepted **agents** as shorthand for **agent personas** on 2026-09-12. An agent persona
is a reusable definition of an agent's behavior, whether used for a primary agent or delegated work
where the harness supports it. The definition may include instructions and supported execution
settings. This terminology does not imply a shared standard format. Keep **subagent** for an agent
executing delegated work. Use **agent persona** and **agent resource** where the artifact and the
existing Agentworks resource could otherwise be confused.

## Findings and design implications

### Agent Skills defines the content format

The [Agent Skills specification](https://agentskills.io/specification) defines a skill as a
directory with `SKILL.md`, containing YAML metadata followed by Markdown. Name and description are
required; supporting scripts, references, assets and other files may accompany it. Its progressive
disclosure model loads metadata first, the body when activated, and supporting resources when
needed.

Decision: use standard Agent Skills content, preserving the whole package during acquisition and
normalization. Do not invent a parallel skill document format. Text normalization to Unix LF must
preserve binary supporting assets. The operator's rule definition has different loading semantics:
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

The old source spelling, declaration map, refresh-on-reinit schedule and snapshot codec are
historical proposals, not selected interfaces for this SDD. The new `artifact-bundle` resource owns
ingestion; source refresh and the normalized carrier still need design. Likewise, the old helper
names are research leads, not a claim that current transfer helpers implement package capture.

## Remaining research

- Specify which agent persona fields are portable or native.
- Research Rulesync's canonical representations and generators, including its agent definitions.
- Compare acquisition and distribution formats and define capture/update behavior.
- Design ingestion against the recovered safety constraints, including conservative text
  classification and byte-preserving fixtures, without restoring the old declaration/wire design.
- Complete the artifact type and facet matrix for every shipped integration, including primary
  versus delegated persona support and unsupported delivery outcomes. State what counts as handled
  for shell file delivery and for each native loading mechanism.

## Sources

| Source                                                              | Quality and angle                                                       |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| [Agent Skills specification](https://agentskills.io/specification)  | Primary standard; skill package structure and loading model.            |
| [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) | Primary product documentation; primary and delegated use.               |
| [Codex subagents](https://developers.openai.com/codex/subagents)    | Primary product documentation; custom definitions for spawned sessions. |
