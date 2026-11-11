# MCP artifacts: prior art

## Summary and evidence limits

Research date: 2026-11-11 UTC. Repository source: `834eb715`. This is static source and primary
documentation research, not a native connection, authentication, model, resume or cleanup result.
The design adopts named server definitions and native integration boundaries; acceptance must prove
the advertised paths independently.

## Rulesync and the protocol

[Rulesync file formats](https://rulesync.dyoshikawa.com/reference/file-formats.html#rulesync-mcp-jsonc)
use a named `mcpServers` object in JSONC. Shared definitions include stdio command/args/env and
remote URL/headers. Its current schema also carries many tool-specific extensions and permissive
objects. Agentworks borrows a strict shared profile, not that passthrough surface, target-selection
model or runtime generator. Its ordinary bundle selection and per-owner propagation remain
authoritative.

The inspected
[Rulesync schema](https://github.com/dyoshikawa/rulesync/blob/54267ffc28387184383b7c8c1cb2c435a1d5ecb7/src/types/mcp.ts)
is pinned independently of the moving documentation. Fields that control trust, approval or tools
cannot safely disappear during translation. The operator elected to reject them in the initial
profile. OAuth is phase 2 of this same effort, with research and the unresolved workflow recorded in
the [OAuth phase boundary](oauth-phase.md).

The
[MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
defines stdio and Streamable HTTP as standard transports. Configuring either does not install a
server or guarantee connectivity. This effort is client registration, with no separate service
supervisor or protocol implementation.

## Native evidence

| Native source                                                                | Documented primitive                                                                         | Design consequence                                                                 |
| ---------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| [Claude MCP](https://code.claude.com/docs/en/mcp)                            | User/project registration, env expansion, native scope precedence and endpoint deduplication | Separate user role from settings; qualify managed names; retain native limitations |
| [Claude CLI](https://code.claude.com/docs/en/cli-reference)                  | Invocation `--mcp-config`; strict mode changes other sources                                 | Private opt-in file without strict mode                                            |
| [Codex MCP](https://developers.openai.com/codex/mcp)                         | Native MCP config, env forwarding and native OAuth flow                                      | Register, preserve references, leave login to the operator/harness                 |
| [Codex config](https://developers.openai.com/codex/config-reference)         | `env_vars`, `env_http_headers`, `bearer_token_env_var`                                       | Use a small explicit portable reference grammar                                    |
| [Codex CLI](https://developers.openai.com/codex/cli/reference)               | Repeatable invocation config overrides                                                       | Test additive session leaves rather than project writes                            |
| [Grok MCP](https://docs.x.ai/build/features/mcp-servers)                     | Native user/project registration and env interpolation                                       | Native config tables, with cross-catalog discovery disclosed                       |
| [Grok settings](https://docs.x.ai/build/settings/reference)                  | Stdio/remote fields and bearer env binding                                                   | Map only supported fields                                                          |
| [Grok plugins](https://docs.x.ai/build/features/skills-plugins-marketplaces) | MCP-bearing plugins and extra plugin directories                                             | Private session plugin is a testable candidate, not proven behavior                |

Claude's user MCP document differs from `.claude/settings.json`; local scope is a user-private
project record, not session isolation. Its list/get operations can connect, and removal can also
delete local OAuth state. Offline inspection and entry-local tracked-file cleanup avoid those side
effects. Native project trust and normal approvals must remain intact.

Codex does not document generic `${VAR}` interpolation in config strings. Same-name env forwarding,
whole-header env binding and bearer env binding have dedicated fields. Do not emit a literal
reference and assume it expanded. Native invocation overrides need fresh/resumed isolation tests;
the existence of `-c` alone does not prove every mode.

Grok also discovers some Claude/Cursor MCP config, so a Grok-specific native table does not imply
cross-integration catalog isolation. Its documented plugin composition warrants a bounded
session-carrier experiment; missing documentation for a flag is not proof that no flag exists.

## Current Agentworks extension points

- `artifacts/declarations.py` and `bundle.py`: entry replacement and per-type maps already exist.
- `artifacts/model.py`, `capture.py` and `codec.py`: add normalized structured content and an
  explicit codec version; captured bodies are persisted, not secret-scrubbed.
- `artifacts/state.py:63`: declaration hashes currently include empty maps. Preserve old hashes when
  the new map is empty.
- `harness_setup/lifecycle.py:91`, `inputs.py:69`: capture precedes eager resolution; ordinary
  env-secret targets already participate in the resolution boundary.
- `artifacts/application.py` and `publication.py`: whole-file ownership fits private files, not
  shared native entries. Existing native claims/checkpoints provide the latter seam.
- `plugins/_harness_native/native.py`: shared settings mapping and expected-content publication are
  existing patterns, including semantic JSON/TOML rewriting.
- `artifacts/native/common.py`, native plugin `artifacts.py`: every new type needs explicit handling
  or deferral; absence of a generated file does not itself prove unhandled input.

These paths are relative to `cli/agentworks`. Research read actual current source rather than
inferring behavior from predecessor plans.

An additional check at `07feb4a4` confirmed that `artifacts/session.py:123-131` copies only the
Agentworks-composed env, while `sessions/tmux.py:618-642` later executes the workload through
`$SHELL -lic`. Guest startup exports can supply or change MCP variables after preparation. An absent
map key is not evidence of a missing native binding, and a separate probe would run a different
process at another time. Preserve native runtime resolution instead.

## Refuted assumptions and open proof

Do not rely on universal env-string interpolation, config-only native removal, read-only native
inventory, whole-file ownership for a shared table, or owner-qualified names preventing endpoint
deduplication. None is a sound cross-harness guarantee.

Native-home overrides, trust behavior, independent-entry updates, interrupted cleanup and each
session carrier still need actual supported-version/platform evidence. Shell evidence cannot
substitute for those native cases. No broad minimum native version or repeated prompt/model probe is
proposed. A failed candidate remains unsupported with a clear warning.

All external links above are primary project/specification sources. Rulesync's pinned schema is
strong evidence for its syntax; native manuals are strong evidence for supported knobs, with runtime
composition and interoperability deliberately left to acceptance.
