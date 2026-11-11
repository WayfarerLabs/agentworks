# MCP OAuth: phase-2 design

## Status and dependencies

This is private phase-2 preparation based on registration head `7accf20c`. It does not change the
active registration PR or complete R11. The registration report and any resulting correction must be
absorbed before this dependent increment is handed off. OAuth remains part of this SDD; registration
alone does not justify its lock.

The lead selects documented operator-native login through existing actual-user access as the
conservative implementation. The optional operator preference remains unanswered; this is a lead
decision within the authorized design, not a claim of operator selection. No login wrapper is added.
Confidential-client secrets and custom callback URLs are explicitly unsupported in the canonical
profile. Session-only authentication and supported remote/native modes still need their own evidence
before support is advertised.

## Configuration boundary

Extend the existing HTTP connection definition with a strict typed `oauth` object. Do not add an
artifact type, an authentication resource, an untyped native options bag or a second protocol
client. Stdio keeps its existing environment credential mechanism. Inline definitions and selected
same-key JSONC definitions continue through the same canonical parser.

This boundary follows the
[MCP authorization transport model](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

The public subset is:

```yaml
mcp_servers:
  issue-tracker:
    definition:
      type: http
      url: https://mcp.example.test/mcp
      oauth:
        client_id: public-registered-client
        callback_port: 8180
        scopes:
          - issues:read
```

`client_id`, `callback_port` and `scopes` are optional public values. An omitted field delegates its
selection to the native client; it does not select a cross-harness default. A present empty OAuth
object requests native OAuth defaults. An omitted or null OAuth object retains the phase-1
connection shape and the native client's ordinary authentication behavior. A null optional field
normalizes to absence, following the existing nullable metadata convention. Neither form attests to
a login. Bundle entries retain whole-entry replacement; no per-field OAuth inheritance is added.

Validate this operator-authored boundary strictly: a literal client ID containing non-space text
without control characters; an integer callback port in `1..65535`, excluding booleans; a nonempty
ordered list of unique RFC 6749 scope tokens when scopes are supplied. Reject an empty scope list
rather than claiming that it means no permissions. Reject unexpected keys, coercions and OAuth on a
stdio definition. Preserve requested order rather than changing an authorization request through
sorting.

An explicit OAuth object and an `Authorization` header are conflicting declared authentication modes
and should fail validation together, including case variants of the header. Other public or
env-referenced headers can accompany OAuth for an authenticating proxy. Their faithful native
behavior needs its own acceptance cell; configuration alone does not prove their propagation through
native discovery/token requests.

These names follow Agentworks configuration spelling. Native spelling is a translation concern.
Rulesync supplies useful prior art, but its permissive OAuth passthrough is not this contract.

Confidential-client secrets, issuer/metadata overrides and custom callback URLs are rejected rather
than carried through an untyped options bag. This is the supported portable profile for this phase;
future extensions need explicit fields, native translation and ownership evidence. Unknown structure
remains an error throughout.

## Native translation and ownership

Use the existing pure MCP renderer and shared-document claim operation. Public OAuth options are
part of the rendered entry and therefore its ordinary fingerprint and expected-content write.
Original-owner native names remain stable when public options change. Authentication tokens and
grants are not part of artifact content, declaration hashes, applied-state snapshots or diagnostics.

| Canonical field       | Claude candidate translation   | Codex candidate translation |
| --------------------- | ------------------------------ | --------------------------- |
| `oauth.client_id`     | `oauth.clientId`               | `oauth.client_id`           |
| `oauth.callback_port` | `oauth.callbackPort`           | `oauth.callback_port`       |
| `oauth.scopes`        | Space-separated `oauth.scopes` | Server-level `scopes` list  |

The native spellings and callback differences are documented by
[Claude](https://code.claude.com/docs/en/mcp#use-pre-configured-oauth-credentials) and
[Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli#oauth-client-registration-and-callbacks).
Their documented options are research input, not a blanket support claim for installed versions.

An absent public option must stay absent. Do not insert an arbitrary callback URL, discover an
issuer while publishing config, force one native client-registration mechanism, add scopes, select a
credential-store backend or run native add/remove as part of artifact application. Those operations
have native side effects and auth semantics beyond an owned registration entry.

Claude and Codex public-client translations have bounded isolated native evidence. An additional
Codex 0.167.0 flow used only the public fields, without a callback URL override, and observed a
native-generated callback on the configured fixed port, public login, authenticated MCP discovery
and targeted local logout. Its native config stayed unchanged. That is one owned native mode, not
managed acceptance or a promise about arbitrary issuers and installed versions. Grok's strict
public-option translation is not established. Do not drop unsupported explicit options and call the
entry handled; defer the whole connection with a useful reason. Shell continues to publish canonical
data without authenticating or executing it.

For Grok, an explicit empty object needs only the ordinary URL registration: native HTTP OAuth
defaults remain native, with no invented configuration field. Explicit client ID, callback port or
scope options remain unhandled until a faithful native translation is established. Defer those
connections per entry while preserving prior claims/native bytes and allowing independent entries to
apply. This is a registration disposition, not evidence that a particular native login succeeds.

Current native mechanisms determine which facet can faithfully apply a definition. VM normally
defers native MCP to user; inactive VM still defaults to user and inactive user/workspace pass to
session. An unsupported terminal session item warns. No scope performs login on behalf of unknown
future users. A shared workspace registration does not make its users share credentials or grants.

Ordinary update/withdrawal retains native credential state and existing foreign/modified-entry
protection. Do not adopt, rewrite or delete a credential cache to make a claim match. Explain when
changed endpoint/client options may require authenticating again through the native client; do not
promise that a stable registration name necessarily denotes the same provider grant.

## Capture compatibility

Public OAuth expands the accepted canonical MCP structure. New captures use interpretation version
5, following registration's version 4. Retained versions 2/3/4 must keep their existing meaning and
identity; a retained v4 payload carrying OAuth stays invalid rather than acquiring newly accepted
semantics through the latest parser.

Keep wire-envelope selection, canonical parser admission and instance-state version admission
coherent. Write the new version through ordinary state boundaries; older releases must refuse and
preserve an unknown future capture instead of downgrading it. Preserve the meaning and hashes of
four-type and phase-1 MCP records. Canonical metadata with no OAuth addition should not gain an
extra null/default field merely because the model was extended.

Preserve unchanged declaration fingerprints as well as capture bytes. The separate
`artifacts/state.py:declaration_digest` boundary hashes a model dump that includes nullable
defaults; adding an absent `oauth` member there would incorrectly stale existing inline HTTP
declarations, including workspaces with no reinit path. Omit only this new field when absent/null in
that projection, keeping older fields' existing representation and retaining an explicit OAuth
object. Require a boundary test for unchanged phase-1 declarations and a changed fingerprint when
actual OAuth intent/options are added. Do not globally discard nulls or invent a second freshness
store.

This change needs boundary tests for declaration/source equivalence, typed rejection, retained
capture interpretation, exact digests and unknown-version preservation. Tests should assert behavior
and data, not the wording of the guide or this design.

## Explicit authentication workflow

Authorization, token storage, refresh and local credential removal stay with the actual user's
native harness. Setup/reinit/start performs no automatic browser, consent, login or logout
orchestration. Native behavior during an operator-launched harness remains native behavior and must
be described honestly, including differences when there is no interactive terminal.

Use documented operator-native commands through existing access. `agent shell` and `agent exec`
already select the actual agent user and optional workspace through the existing SSH, login
environment, secret and TTY boundaries. `vm shell` and `vm exec` provide the corresponding
admin-user paths, including workspace selection; the workflow must not require an agent to exist.
Reusing these commands does not prove that a remote OAuth paste flow works through every access
mode. No wrapper command or new capability method is added. The native command reports its own
login/status/logout result through the existing remote execution or terminal path; Agentworks does
not read its token store or translate an unmeasured native auth state into success.

The workflow must identify the original-owner native server name, actual Linux user and workspace
context. A session-only entry must remain invocation-only while authenticating; do not temporarily
register it at user scope for convenience. Document only established native modes and their
terminal/callback requirements, ordinary failure and cancellation behavior. Without a usable
interactive path, leave the connection unauthenticated rather than inventing a browser flow or
pre-seeding credentials. Offline artifact inspection remains offline and does not read native
credentials or probe auth. Configured scopes describe a request, not granted permissions or an
authentication result.

## Remaining support decisions

- **Confidential clients:** unsupported by the canonical artifact profile. Native sinks differ.
  Claude documents secret input separate from public entry metadata; Codex documents storing a
  client secret in its private config. No secret field is accepted in artifact captures, argv,
  workspace files or diagnostic output. Do not equate an ordinary env bearer reference with an OAuth
  client secret. A native-added Codex secret in the same server table conflicts with the current
  whole-entry fingerprint/public rendering: do not exempt arbitrary fields or let a public update
  erase it. Preserve it as an external modification under the existing claim rules. Supporting this
  combination later needs a demonstrated separate sink/reference or a precise revised ownership
  contract.
- **Callbacks:** a fixed listener port is not a portable redirect URI. Native host, callback path,
  server identity and issuer support can affect the URI. Custom remote ingress may differ from a
  loopback listener. Preserve native issuer validation and display the actual callback rather than
  computing a vendor-specific callback identifier in core.
- **Session-only definitions:** verify native login/discovery against the invocation carrier and the
  actual user's store without persisting the definition to a broader scope. Explicitly defer
  combinations that cannot retain this contract.
- **Grok:** establish supported public options and an explicit usable auth path before claiming
  their translation. A model-account/device login is not an MCP-server OAuth workflow.
  [Its MCP guide](https://docs.x.ai/build/features/mcp-servers#in-the-tui) documents a native TUI
  authentication action, which still needs measured validation in the supported context.
- **Permissions:** configured scopes are requests, not an attestation of granted or maximum scope.
  Native clients can request refresh-related scope and authorization servers can grant differently.
  Do not silently broaden the declaration or claim that an empty/native-default set means none.

## Evidence and acceptance

The isolated Linux aarch64 research observed actual Claude 2.1.319 and Codex 0.167.0 native public
DCR/PKCE login, configured public IDs/callback ports without DCR, authenticated MCP discovery,
refresh, cancellation and local logout against owned loopback endpoints. Tokens were public fixture
material. Separate HOME/file-store checks did not establish OS-UID or shared-keyring isolation. The
fixture deliberately granted refresh capability; preconfigured Codex requested read alone.

A separate public-options-only Codex 0.167.0 flow used exactly URL, server-level scopes and
`oauth.client_id`/`oauth.callback_port`, with no callback URL or issuer/metadata override. Native
authorization selected the configured port and a generated callback path; the live listener was
observed before manual redirect paste. Public S256 login and model-free authenticated MCP catalog
discovery succeeded, followed by targeted local logout. The native config stayed byte-identical. The
fixture granted read only and made no provider revocation claim. The lead read the measured
receipts, matched the actual renderer hash and independently verified both listener ports closed and
the exact owned native home absent. Session-only, OS-user and managed acceptance are not inferred.

That is research, not Agentworks R11 acceptance. Test the shipped increment against actual managed
users, including two users of a shared workspace, native credential isolation, remote browser/paste
flow, supported public/confidential cases, missing/expired credentials, refresh/status/logout and
ordinary artifact withdrawal. Keep grant revocation distinct from local token removal. Validate
session-only behavior wherever advertised, preserve original failures and independently verify
credential/resource cleanup. Record the actual source, native version, mode and platform for each
claim; no blanket minimum version or daily-release compatibility policy follows from one run.

The phase-2 implementation PR carries the finished design, any agreed FRD/plan refinements,
permanent guides/examples, installed-package checks, full gates and required independent reviews.
Its actual integration report and all outstanding registration obligations gate overall closeout.
