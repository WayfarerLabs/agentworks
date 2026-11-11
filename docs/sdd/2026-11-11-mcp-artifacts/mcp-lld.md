# MCP artifacts: format and delivery design

This proposed LLD makes the [HLA](hla.md) implementable. It describes prospective schema and native
behavior, not current CLI support.

## Declaration shape

```yaml
kind: artifact-bundle
name: development-tools
mcp_servers:
  docs:
    definition:
      type: http
      url: https://mcp.example.test/mcp
      headers:
        Authorization: Bearer ${MCP_DOCS_TOKEN}
  inventory:
    definition:
      type: stdio
      command: inventory-mcp
      args: [serve]
      env:
        MCP_INVENTORY_TOKEN: ${MCP_INVENTORY_TOKEN}
  repository-tools:
    source: file::~/agent-content/mcp.jsonc
```

Each entry specifies exactly one of `definition` or `source`. Both forms produce the same immutable
canonical definition. Source supports existing workstation and Git file references. It must select
one regular UTF-8 JSON/JSONC file, with normalized line endings; no directory discovery or server
execution occurs. The selected artifact key must exist under `mcpServers`:

```jsonc
{
  "mcpServers": {
    "repository-tools": {
      "type": "stdio",
      "command": "repository-mcp",
      "args": ["serve"],
    },
  },
}
```

The source root accepts only `mcpServers` and optional string `$schema`. Validate whole-document
syntax/root shape and reject duplicate object keys, then apply the strict definition profile only to
the selected same-named server. Unused definitions do not become dependencies of that artifact.
Capture only its normalized definition. Unselected bodies and source comments do not become
persisted artifact members. The optional schema URL is editor metadata and is never fetched. Do not
use a regex comment stripper or execute the source to parse it.

Existing skill/persona frontmatter guards against embedded `mcpServers`/`mcp_servers` remain. Only
this top-level artifact type supplies declarative MCP registration; do not remove those guards when
updating the general support statement.

## Definition profile

| Field         | Accepted value and meaning                                                             |
| ------------- | -------------------------------------------------------------------------------------- |
| `description` | Optional nonempty public description                                                   |
| `type`        | Required `stdio` or `http`; `http` means Streamable HTTP                               |
| `command`     | Required nonempty executable string for stdio; never a command array or implicit shell |
| `args`        | Optional string array for stdio, preserving argument boundaries                        |
| `env`         | Optional string map for stdio; literals or supported env references                    |
| `url`         | Required absolute HTTP(S) endpoint for HTTP; no userinfo or env interpolation          |
| `headers`     | Optional string map for HTTP; public literals or supported env references              |

Stdio rejects HTTP fields; HTTP rejects stdio fields. Other fields fail, including `transport`,
`local`, `sse`, `ws`, `streamable-http`, `cwd`, `disabled`, `enabled`, tool filters, trust, approval
controls, OAuth fields and tool-specific blocks in phase 1. OAuth gains a deliberate phase-2 schema
under this same SDD; no opaque reserved auth bag is added now. Other extensions can be added
deliberately later; they are not silently passed through. This profile uses Rulesync's `http`
spelling while the operator-facing transport name is Streamable HTTP. Native SSE fallback is not a
claim of support for canonical SSE definitions.

Keep existing canonical artifact name rules. Reject NUL and unsupported control characters in
strings and invalid env/header names, including case-insensitive duplicate header names. Command,
args and URL are literal target-runtime values. Relative executable lookup uses the harness's actual
PATH, not the workstation PATH. Stdio inherits the harness's normal working directory; no separate
cwd setting is introduced. No automatic dependency installation or command probe happens at
capture/application.

Errors identify bundle, artifact and field, not rejected values or parser excerpts. Reject ambiguous
duplicate keys and invalid or contradictory types before native mutation. Reuse the ordinary
acquisition limits and its no-links/special-files/Git-metadata policy.

## Reference grammar

Values are either public literal strings or these complete reference forms:

- `env.NAME: ${NAME}` forwards the same named runtime variable to a stdio child.
- `headers.Header-Name: ${NAME}` uses the entire runtime variable as a header value.
- `headers.Authorization: Bearer ${NAME}` uses the variable as a bearer token.

`NAME` uses the existing POSIX env-name rule. No default expressions, interpolation in command, args
or URL, variable renaming, embedded fragments, shell substitution or escape language is accepted.
Reject other `${...}` forms explicitly. This intentionally small shared subset maps to documented
native primitives without resolving a secret into a durable artifact or argv value.

The actual consuming workload receives values through ordinary env configuration, for example
`MCP_DOCS_TOKEN: {secret: docs-token}`. Direct `{secret: ...}` inside the MCP definition is
rejected: acquired content does not join a second secret dependency graph. Env references remain
names in canonical JSON, captures, digests and native definitions. Existing eager secret resolution
supplies declared env and its redaction boundary. All facets validate representation only; a
workspace registration may intentionally reference a future user's variables. Session preparation
also lacks a complete native environment: `$SHELL -lic` startup runs after its env map is captured.
Do not infer missing bindings from that map, inspect profiles or add a separate probe. Native
clients resolve references in their real process environment and report failures normally. Keep this
distinct from registration, with no new Agentworks launch gate or implicit repair. Never dump a
variable's value.

Claude/Grok receive supported native interpolation strings. Codex receives same-name `env_vars`,
`env_http_headers` and `bearer_token_env_var`, with public literals in its literal maps. Never emit
`${NAME}` as a supposedly expanded Codex literal. If native policy or a binding cannot represent the
definition faithfully, defer the entire entry with the variable/field name and reason. Dedicated MCP
variable names avoid native provider-credential restrictions. An external process that did not
receive Agentworks env is outside the managed-launch guarantee.

## Canonical pipeline and compatibility

Add `ArtifactType.MCP_SERVER`, `mcp_servers` on each immutable group, and a strict definition model.
Use canonical definition JSON in `ArtifactContent.metadata_json`, with empty instruction text and
members for both inline and sourced definitions. Keep its public description in the ordinary
description field. Do not retain a second full raw source document. Canonical JSON orders object
keys while preserving argument/list order. Source location, comments, newline style and unselected
entries do not change the selected connection's identity. Original owner, canonical key and type
continue to determine logical identity.

Write capture codec version 4, retain v2/v3 decoding under their original four-type interpretation,
and reject MCP data in a legacy envelope. Preserve legacy content identities and declaration
fingerprints when MCP maps are empty. Unknown future capture versions retain existing refusal and
repair behavior. Use existing instance-state storage, not a SQL schema or parallel MCP database.

## Native names and smallest ownership unit

Derive each server name from the original-owner/type/key identity, independent of content digest,
bundle and receiving facet: `agentworks-<key-prefix>-<origin-digest-prefix>`. Use up to 24 key
characters, trim a trailing hyphen, and use 20 digest characters; the full key participates in the
existing `origin_identity` hash. This bounds names to 56 ASCII characters while keeping them
readable and stable across content changes and workspace rehome. Check proposed native identities
for duplicates. Inspection shows logical and native names. Source names need not match native names.

User/workspace registration edits the harness's fixed current native MCP role: Claude user
`mcpServers`, Claude workspace `.mcp.json`, or the Codex/Grok `mcp_servers` table. Resolve actual
native-home conventions before writing; a recorded path is evidence rather than unrestricted file
authority. Preserve unrelated entries/keys semantically through existing native JSON/TOML
parsers/serializers. Comments and formatting may change as with current settings mapping; document
that limitation. Use expected-content writes to detect concurrent edits. Never overwrite a whole
config from a stored snapshot.

Reuse ordinary `NativeClaim` and checkpoint storage: `role` is `artifact-mcp-server`, `identifier`
is the derived native server name, `destination` is the current native config path, `source` is the
stable original-owner identity, and `sha256` fingerprints canonical native-entry content. Update
that field's current compatibility-only comment for its new MCP use; historical settings fields
retain their read-compatibility meaning and settings ownership is not revived. No document or
credential body enters claims, and there is no second receipt ledger.

For each desired entry: absent creates; equal can be adopted; current content matching the recorded
owned fingerprint can be updated. Other differing or externally modified content is retained and
that entry is deferred with a conflict reason. For a removed entry: absent drops the stale claim;
unchanged owned content is removed; modified content is retained with its claim and warning.
Checkpoint confirmed progress so retry heals interruption. Do not erase other owners' keys or OAuth
credentials. Private session files instead use existing whole-file/run ownership and retirement.

Mapped Codex settings, prior MCP claims and desired entries are prepared together from one live
snapshot. Compute safe obsolete-entry retirements in a working document, then apply the mapping with
its original file-existence decision and reconcile desired canonical entries. Compare actual
policy-winning contributions at the derived native key before publication. Equal entries agree;
conflicts leave that canonical entry unhandled. Publish the final document once with the observed
file hash, then checkpoint claims. Never publish an intermediate deletion and rely on a second
reinit to restore a mapped key.

For an unchanged MCP-owned entry withdrawn while the mapping source still names that key:

| Mapping strategy  | One-pass result                                                                                                               |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `replace`         | Source entry survives in the final document; release the MCP claim                                                            |
| `merge-overwrite` | Source entry survives in the final document; release the MCP claim                                                            |
| `merge-preserve`  | Retired owned value no longer masks the source; source entry survives and the claim is released                               |
| `skip-existing`   | If the original file existed, ignore the source and retire the MCP entry; if absent, apply source and release the stale claim |

MCP cleanup alone preserves modified obsolete values. An explicit policy-winning settings
contribution can take over that native key, or an authoritative replacement can remove it. Release
the obsolete artifact claim in the same pass; do not retain a stale fingerprint or repeated
incomplete warning after that authorized result. Suppressed source contributions and unrelated
merges keep the modified entry and its protection. Removal output distinguishes a missing native
entry from a still-present entry whose artifact ownership ended. A removed mapping alone retains
native values. `contributions(initial)` is not enough for this plan: suppressed source keys may
become applicable after safe retirement. Use the prepared source and existing parser/serializer
APIs, not a second state ledger or reread. No extra integration activates implicitly.

## Workspace paths, relocation and removal notices

`ArtifactApplication.shared_paths` is transient confirmed native-document metadata for workspace
entry effects. It is separate from `files`, which grants ordinary file/section ownership. Core
validates paths against the current workspace and feeds them alongside file placements into the
existing Git-exclude helper. A path report grants no file-write or whole-document cleanup right. Use
existing exclusion claims for retention, removal and current-Git metadata transfer, including
tracked-file warnings; add no persistent file-birth registry.

After successful copy in workspace rehome, relocate MCP claim destinations from the actual old root
to the verified new root through ordinary state checkpoints, retaining identifiers and hashes. Keep
destination identity and coexisting file-placement metadata coherent. Unsafe/unprovable recorded
destinations get bounded diagnostics instead of becoming I/O authority. Preserve existing error
propagation and incomplete-state handling, not a new rollback or deletion gate. A retained old
directory remains the operator-requested copy; managed evidence follows the active workspace.

When a registration is removed, identify the native server and warn that any native OAuth
credentials remain. The guide gives logout-before-withdrawal instructions for verified native
commands and explains restoring a definition when logout needs it. Do not probe whether a grant
exists, run logout automatically or invent a Grok credential-cleanup command. Explicit OAuth
workflow belongs to phase 2.

## Session carriers

- Claude: `session-mcp-config` writes a private JSON file under the current session/run directory
  and appends `--mcp-config` with that file. Do not add `--strict-mcp-config` or replace other
  sources. Existing explicit conflicting native flags need a clear unhandled reason.
- Codex: `session-mcp-overrides` supplies typed invocation-only `-c` overrides for each derived
  server table. Encode literal values with the TOML serializer; env bindings remain variable names.
  Do not write the project/user config or replace the entire `mcp_servers` table.
- Grok: the private MCP-bearing plugin candidate remains unsupported. Actual stable 1.0.77 rejects
  the root `--plugin-dir` flag, including with `--help`. Do not advertise `session-mcp-plugin` or
  use a shared-catalog fallback. A future supported binary needs independent fresh/resume/isolation
  proof before that workaround is introduced.
- Shell: canonical JSON files join the existing private session index; no native workaround or
  server launch is needed.

Codex config overrides deep-merge with lower layers, including when the override names a whole
server table. They cannot clear stale fields from a differing same-named native entry. Before a
selected session carrier is rendered, read only the fixed native user/project MCP roles and compare
that selected derived key. Equal entries are safe; differing or unreadable native entries defer that
MCP item with a value-free reason. This is a registration conflict check, not native CLI inventory,
credential discovery or an environment-binding probe. Preserve independent legacy delivery. Explicit
modeled/raw Codex profiles defer this workaround: profile overlays can introduce the same
incompatible merge, and this phase does not inspect their precedence. Ordinary native profiles
remain usable.

Resolve Claude's actual user MCP role separately from its rules/settings root. Without a native
override it is `$HOME/.claude.json`; with explicit `CLAUDE_CONFIG_DIR` it is the `.claude.json`
under that directory. A wrapper-synthesized rules root is not evidence that an override was present.
Use the workload's existing login environment to observe only HOME and the native-home variable,
never credential variables or profile-file content.

The Codex command budget is decided against the complete selected fresh/resume/picker command,
including ordinary flags, notification setup, initial input and quoting. If optional MCP makes it
too large, defer its generated overrides and rebuild that same selected command once. Do not repeat
discovery/adoption or weaken the legacy artifact limit. Cheap raw-argv rejection can avoid building
an obviously oversized carrier, but it is not sufficient final admission.

No MCP session carrier runs by default. Preserve unrelated artifact delivery when selecting MCP
inputs, and explicitly handle/defer MCP in every adapter so omission cannot mean applied.
