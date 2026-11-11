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

The source root accepts only `mcpServers` and optional string `$schema`. Validate the document and
all server definitions against this profile, reject duplicate object keys, and capture only the
selected normalized definition. Unselected bodies and source comments do not become persisted
artifact members. The optional schema URL is editor metadata and is never fetched. Do not use a
regex comment stripper or execute the source to parse it.

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
controls, OAuth fields and tool-specific blocks. These can be added deliberately later; they are not
silently passed through. This profile uses Rulesync's `http` spelling while the operator-facing
transport name is Streamable HTTP. Native SSE fallback is not a claim of support for canonical SSE
definitions.

Keep existing canonical artifact name rules. Reject NUL and unsupported control characters in
strings and invalid env/header names. Command, args and URL are literal target-runtime values.
Relative executable lookup uses the harness's actual PATH, not the workstation PATH. Stdio inherits
the harness's normal working directory; no separate cwd setting is introduced. No automatic
dependency installation or command probe happens at capture/application.

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

The owner supplies values through ordinary env configuration, for example
`MCP_DOCS_TOKEN: {secret: docs-token}`. Direct `{secret: ...}` inside the MCP definition is
rejected: acquired content does not join a second secret dependency graph. Env references remain
names in canonical JSON, captures, digests and native definitions. Existing eager secret resolution
supplies the normal integration execution environment and redaction boundary. Check known reference
availability before native application; do not dump a missing variable's value.

Claude/Grok receive supported native interpolation strings. Codex receives same-name `env_vars`,
`env_http_headers` and `bearer_token_env_var`, with public literals in its literal maps. Never emit
`${NAME}` as a supposedly expanded Codex literal. If native policy or a binding cannot represent the
definition faithfully, defer the entire entry with the variable/field name and reason. Dedicated MCP
variable names avoid native provider-credential restrictions. An external process that did not
receive Agentworks env is outside the managed-launch guarantee.

## Canonical pipeline and compatibility

Add `ArtifactType.MCP_SERVER`, `mcp_servers` on each immutable group, and a strict definition model.
Use the canonical serialized definition as MCP content; do not retain a second full raw source
document. Canonical JSON orders object keys while preserving argument/list order. Source location,
comments, newline style and unselected entries do not change the selected connection's identity.
Original owner, canonical key and type continue to determine logical identity.

Write capture codec version 4, retain v2/v3 decoding under their original four-type interpretation,
and reject MCP data in a legacy envelope. Preserve legacy content identities and declaration
fingerprints when MCP maps are empty. Unknown future capture versions retain existing refusal and
repair behavior. Use existing instance-state storage, not a SQL schema or parallel MCP database.

## Native names and smallest ownership unit

Derive each server name from the existing original-owner/type/key identity, independent of content
digest, bundle and receiving facet. A bounded `agentworks-` prefix plus identity digest avoids
user/workspace/session same-name shadowing; inspection displays the logical name and derived native
identity. Check proposed native identities for duplicates before publication. Source names do not
need to match native names.

User/workspace registration edits the harness's fixed current native MCP role: Claude user
`mcpServers`, Claude workspace `.mcp.json`, or the Codex/Grok `mcp_servers` table. Resolve actual
native-home conventions before writing; a recorded path is evidence rather than unrestricted file
authority. Preserve unrelated entries/keys semantically through existing native JSON/TOML
parsers/serializers. Comments and formatting may change as with current settings mapping; document
that limitation. Use expected-content writes to detect concurrent edits. Never overwrite a whole
config from a stored snapshot.

Reuse ordinary `NativeClaim` and checkpoint storage. A claim records role, native name, fixed
destination identity, origin identity and a canonical native-entry fingerprint; it contains no
document or credential body. Do not invent a second receipt ledger. Reuse an existing suitable
fingerprint field rather than overloading source text with encoded config.

For each desired entry: absent creates; equal can be adopted; a confirmed owned entry converges;
different unowned content is retained and that entry is deferred with a conflict reason. For a
removed entry: absent drops the stale claim; unchanged owned content is removed; modified content is
retained with its claim and warning. Checkpoint confirmed progress so retry heals interruption. Do
not erase other owners' keys or OAuth credentials. Private session files instead use existing
whole-file/run ownership and retirement.

Mapped Codex settings and desired MCP entries are prepared together. Compare contributions at the
derived native key before writes. Equal entries agree; conflicting explicit values leave that
canonical entry unhandled. The mapping's selected strategy still governs unrelated keys. Removal of
a mapping keeps its current retention contract. No extra integration activates implicitly.

## Session carriers

- Claude: `session-mcp-config` writes a private JSON file under the current session/run directory
  and appends `--mcp-config` with that file. Do not add `--strict-mcp-config` or replace other
  sources. Existing explicit conflicting native flags need a clear unhandled reason.
- Codex: `session-mcp-overrides` supplies typed invocation-only `-c` leaves for the derived server
  table. Encode literal values with the TOML serializer; env bindings remain variable names. Do not
  write the project/user config or replace the entire `mcp_servers` table.
- Grok: evaluate `session-mcp-plugin` using one private MCP-bearing plugin and `--plugin-dir`. Prove
  native loading, naming, additive flags and resume isolation before enabling this option. If it
  cannot meet that contract, report unsupported with no shared-catalog fallback.
- Shell: canonical JSON files join the existing private session index; no native workaround or
  server launch is needed.

No MCP session carrier runs by default. Preserve unrelated artifact delivery when selecting MCP
inputs, and explicitly handle/defer MCP in every adapter so omission cannot mean applied.
