# MCP artifacts: OAuth phase boundary

## Scope and timing

OAuth is phase 2 of this SDD under the operator's explicit clarification. The initial registration
push rejects OAuth fields; that is a phase boundary, not the completed scope of MCP artifacts.
Detailed auth schema/workflow design follows phase 1 and precedes OAuth implementation. The overall
SDD stays open until both phases and their acceptance obligations are complete.

Phase 1 must preserve stable original-owner native names, ordinary entry ownership and native
credential stores, so phase 2 can extend a connection without inventing another artifact kind or
importing credentials into artifact storage. It does not add an untyped OAuth options bag now.
Existing native OAuth can work independently; registration is not proof that authentication has been
configured, completed or refreshed.

## Architectural direction

The
[MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
describes HTTP-based authorization. Stdio normally receives credentials from its environment.
Agentworks delegates OAuth protocol behavior to the native harness rather than implementing a
client, token cache, authorization server or refresh service.

Phase 2 will define supported canonical public auth options and faithful native translations,
separate from the phase-1 bearer/header reference grammar. Secret client material, if supported,
must use an explicit native credential sink and existing secret-resolution boundaries. Tokens,
authorization codes and refresh credentials never become bundle bodies, captures, applied config
snapshots, argv values or diagnostic output. A configured header and OAuth fallback are not
interchangeable authentication modes.

Authentication belongs to the actual workload user. A workspace connection can be shared while each
user retains separate native credentials and consent. A facet never authenticates unknown future
users during setup. Agentworks login, consent, browser/device activity and logout are explicit
actions; setup/start adds no automatic auth orchestration. Native clients retain their ordinary
authentication policy and prompts. The detailed design must state the effects of both interactive
and non-interactive commands; absence of a TTY does not prove absence of browser/device
authentication.

The operator's workflow preference is still being clarified: configure native OAuth and orchestrate
native login through Agentworks, or configure it and leave native login to the operator. Neither
option transfers token ownership to Agentworks. This question does not block phase-1 registration.

## Current native research

The sources below establish candidate mechanisms, not live acceptance:

| Harness                                              | Documented seam                                                                        | Phase-2 design constraint                                                                        |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| [Claude](https://code.claude.com/docs/en/mcp)        | Public client/callback/scopes options; native MCP login/logout and remote browser flow | Confidential secret input goes to native credential storage; normal remove can erase OAuth state |
| [Codex](https://developers.openai.com/codex/mcp)     | OAuth config and native login/logout with a remote browser path                        | Confidential-client storage and invocation-only registrations need explicit treatment            |
| [Grok](https://docs.x.ai/build/features/mcp-servers) | Native first-use/TUI OAuth and user credential storage                                 | Headless MCP login/configuration/remote-callback options are not yet established                 |

Native requested and granted scopes need not be identical. Claude/Codex can request additional
native refresh-related scope; an empty list is not proof of zero permissions. Do not translate a
scope request into a false guarantee or silently broaden the operator's requested semantics. Grok
model-account/device login is not evidence of an MCP-server authentication workflow.

## Phase-2 design and acceptance work

Before implementation, settle strict auth fields, supported native combinations, public versus
confidential clients, default native behavior, actual-user context, explicit login/logout/status
workflow, remote browser/callback handling and session-only discovery. Keep unsupported cases
explicit. Do not persist a session definition at user scope to make login easier.

Exercise actual native authorization and subsequent authenticated MCP access against a controlled
test endpoint where practical, including cancellation, missing/expired credentials, refresh where
advertised, user isolation and logout behavior. Registration evidence alone cannot satisfy R11.
Record source/version/platform and independent credential/resource cleanup. Ordinary artifact
removal retains OAuth state; an explicit logout must target only the intended native grant.

The separate OAuth implementation PR must update this design, the FRD/plan and permanent guides in
step with observed behavior. No phase-1 lock is valid for this two-phase effort.
