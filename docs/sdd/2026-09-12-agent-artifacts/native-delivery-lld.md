# Native artifact delivery LLD

**Status:** implementation detail of the approved [HLA](hla.md). The effort lead owns publication,
applied state, routing and session lifecycle. This document specifies the first-party adapters'
native plans and acceptance boundaries.

## Integration boundary

Every first-party integration implements all four facets at contract version 5. VM facets publish no
files. Claude Code, Codex and Grok Build defer VM inputs to the user facet; shell defers them
directly to the session. Neither decision examines downstream activation.

Outer hooks return `ArtifactApplication` after their existing native settings/plugin setup. The
integration selects the exact file bytes, paths, executable intent and native identities. Core
publishes those files and retains ownership in the existing applied-state slice. Retirement returns
an empty plan, letting core remove previously owned artifact files.

Session launch returns the same application with its native command. The optional `artifacts_dir`
field must identify the core-provided private run directory; core exposes it as
`AGENTWORKS_ARTIFACTS_DIR` in the launch environment. Outer facets cannot set this field. All
artifact argv values use `quote_literal_argv`, including JSON and TOML values, so core
command-template substitution cannot interpret literal `{{...}}` in artifact content. Initial
prompts keep their existing fresh-conversation behavior. Artifact guidance applies to each launched
process, including resume.

The adapters do not call one another, fetch sources or modify captured inputs. The modules under
`artifacts/native/` are rendering/probe utilities used by integrations. Core routing does not import
a plugin or select a native format.

## Files and native identities

| Integration | User and workspace files                                                                                                 | Session carrier                                                                                                                     |
| ----------- | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| Shell       | `~/.agentworks-artifacts/user/` and `<workspace>/.agentworks-artifacts/`, containing typed files and an index            | Same layout in the private run directory; `AGENTWORKS_ARTIFACTS_DIR` points there when the run has inputs                           |
| Claude Code | `.claude/rules/agentworks-rule-<name>.md`, one hints rule, complete `.claude/skills/<name>/`, `.claude/agents/<name>.md` | Additive prompt file, private generated plugin for skills, inline `--agents` JSON                                                   |
| Codex       | Complete `.agents/skills/<name>/` and `.codex/agents/<name>.toml`; hints/rules defer to session                          | Composed `developer_instructions`; private role config files selected through `agents.<name>.config_file` and description overrides |
| Grok Build  | Flat `.grok/rules/`, complete `.grok/skills/<name>/`, `.grok/agents/<name>.md`                                           | Composed `--rules` text and inline `--agents` JSON                                                                                  |

User native roots honor `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `GROK_HOME`. Codex's standard user
skills remain under the actual user's `.agents/skills`, independently of `CODEX_HOME`. Workspace
placement uses the workspace root and does not consult an admin's home or native installation as a
substitute for the future session user.

Skill members retain their captured bytes and executable intent. Adapters neither flatten skills
into prompts nor omit supporting files. The Claude session plugin is named `agentworks-artifacts`;
its skills use the native `agentworks-artifacts:<name>` namespace. Its manifest contains only the
generated plugin identity, with no hook or MCP configuration. Codex and interactive Grok Build have
no supported private session skill carrier in this implementation; those inputs remain deferred and
core rejects the final session result.

Native names are checked across session inputs and persisted ancestor ownership metadata. Multiple
members belonging to one skill are one identity. Distinct origins claiming the same native skill or
agent identity are an error, including across user/workspace branches. Session persona definitions
are retained as individually owned files with their native identity; Claude and Grok still receive
the combined definitions in one JSON argument. Previously handled ancestor payloads are not
retransmitted.

## Agent personas

The portable persona has a name, description and instruction body. Native options are supplied under
`native_options.<integration-name>` in the captured persona. Each adapter validates its own options
and rejects unsupported fields rather than silently losing their requested behavior.

| Integration | Accepted native options                                                                 | Encoding                                                                                                                                                     |
| ----------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Claude Code | `model` string; `tools` and `disallowedTools` string lists; `maxTurns` positive integer | Markdown frontmatter for outer facets, native persona JSON for sessions                                                                                      |
| Codex       | `model` and `model_reasoning_effort` strings                                            | Native discovery TOML includes name/description; session role config layers contain only config fields, with identity/description in the selecting overrides |
| Grok Build  | `model` string and `tools` string list                                                  | Markdown frontmatter or inline persona JSON                                                                                                                  |

Hooks, MCP definitions and unknown option fields are not accepted through this surface. Registering
a persona makes its native definition available; it does not silently select the primary persona.
Existing integration `agent` configuration retains its documented behavior. In particular, Codex
primary-persona selection remains prompt-mediated and does not promise that the persona's model or
other runtime configuration applies to the primary thread.

## Native preflight

Preflight resolves the native root in the actual login environment. Declared environment is passed
through the transport's environment channel, not embedded into logged command text. A changed actual
`HOME` is an error because session publication belongs to the Linux user selected by core. A changed
native home that no longer discovers an applied ancestor's files requires correction or owning-facet
reinitialization. User artifact publication honors native-home overrides beneath that user's `HOME`.
A plan that would publish outside `HOME` is explicitly unsupported and fails before settings or
plugin mutations. Persisted ownership and retirement therefore share the same user boundary. A
session can still use an external native home when its artifacts remain inline or in the private run
directory and it has no incompatible ancestor placement.

Codex and Grok guidance is carried directly in native argv; no unused guidance file is published.
Claude's additive prompt file and Codex's selected role configuration files are consumed by their
native CLI carriers.

The native checks use `--version`, applicable `--help` flags and selected configuration facts; no
model is launched. The initial compatibility baselines are Claude Code 2.1.265, Codex 0.153.4 and
Grok Build 1.0.10. Older versions or absent carrier flags fail before publication or session
teardown. These checks establish a supported CLI surface, not proof of model consumption.

Settings checks concern the selected native identities. An unrelated disabled plugin or skill does
not require enabling it. Claude rule exclusions are checked against requested rule paths; extended
exclusion patterns that this adapter cannot interpret are diagnosed explicitly. Codex skill
selectors and Grok disabled skill names are checked against supplied artifacts. Grok workspace paths
are checked against Git ignore rules; user files outside that workspace are not fed to the
workspace's ignore check. A relevant restriction in any inspected configuration layer causes a
conservative refusal. The adapter does not reconstruct native trust or merge precedence, so a later
layer that reenables an artifact does not clear that refusal. Diagnostics identify a configured
restriction without claiming it is the native effective policy.

User root resolution precedes rendering. Policy checks follow existing settings application, so
reinitialization can remove an obsolete exclusion and then publish artifacts. Workspace checks
inspect project policy without assuming the admin's native user setup. Session checks include
ownership metadata for handled ancestors as well as the prospective run's files.

Claude always receives `--system-prompt-snapshot off` when the session consumes artifact context or
has ancestor rules. Its documented default can reuse a previously recorded prompt on resume even if
a later process supplies new append text. Raw carrier overrides and native disable flags are
rejected while artifacts are in use; unrelated raw arguments keep their existing behavior. Codex
profiles and raw feature/config overrides that can disable artifact discovery are explicitly
unsupported with artifact delivery rather than guessed at by a second native settings resolver.

Native argv and the fully quoted returned command each have a conservative 32 KiB UTF-8 limit,
leaving room for launcher wrapping below the operating system's per-argument limit. Oversized text
or persona JSON fails during preparation with guidance to reduce it or use an outer facet's native
file placement. Claude's additive prompt file does not put its body in argv and can carry larger
captured guidance within the source capture limits.

## Evidence and acceptance

Focused tests cover complete skill packages, executable and binary members, native paths and
identities, additive guidance, fresh/resume argv, literal template syntax, unsupported private
skills, ancestor collisions, native home changes and targeted discovery-policy failures. Target-side
probe tests execute the actual probe program against temporary settings, an isolated Git repository
and fake native executables; they do not contact a backend or a model.

Read-only local observations on 2026-09-13 found Claude Code 2.1.265 and Codex 0.153.4. Claude's
local help describes the prompt-snapshot behavior and the required session carriers. The official
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and
[memory documentation](https://code.claude.com/docs/en/memory) describe the native launch and
unconditional rule mechanisms. Codex's official
[configuration schema](https://developers.openai.com/codex/config-schema.json) defines role config
selection and skill selectors.

Grok's CLI and persona parsing were checked against
[official source at `37949780`](https://github.com/xai-org/grok-build/blob/37949780c144e37df692e3d669051a21fec24f20/crates/codegen/xai-grok-pager/src/headless/cli.rs).
Its inline-agent parser maps the `prompt` field to the persona body and supplies the map key as the
native name. Its [settings reference](https://docs.x.ai/build/settings/reference) documents disabled
skill names and per-subagent toggles. No Grok binary was available locally.

An isolated local Codex 0.153.4 check used user `features.multi_agent = false` and a trusted Git
project with `features.multi_agent = true`. Native `codex features list` reported
`multi_agent=true`; the artifact preflight refused the user-layer restriction. This confirms the
conservative boundary, not native disablement. Blindly merging project settings would be incorrect
when Codex declines to trust that project. Claude settings merge semantics and Grok
managed/requirements overlays also remain outside this probe's effective-policy resolution; native
acceptance must exercise the actual policy environment.

Live native acceptance remains required before shipping: verify native discovery, registered
personas, rule and skill loading paths, fresh/resume carrier selection, native policy refusal and
cleanup through native diagnostics and a scoped VM. R10 excludes model calls and external
marketplaces from this acceptance. File existence, native diagnostics and green renderer tests must
not be reported as proof that a model consumed an artifact.
