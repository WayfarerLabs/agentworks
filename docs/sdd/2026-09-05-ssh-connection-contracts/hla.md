# SSH Isolation and Consolidation: High-Level Architecture

- Status: Draft for reduced-scope design review
- Updated: 2026-09-09
- Requirements: [frd.md](frd.md)
- Source baseline: main `c7dce3d4`

## Architecture

Retain installed OpenSSH subprocesses and existing transport interfaces. One connection value
describes host, user, port, configured identity, agent selection and explicit trust-file references.
It contains no VM name, workload state or command. Existing VM factories and Remote Lima placement
configuration produce it; there is no separately persisted host resource or routing framework.

One leaf SSH-policy builder supplies common options for SSH and scp, accounting for their different
port syntax. `ssh.py` and `transports/ssh.py` share buffered execution, including stdin handling,
logging, sensitive-output suppression and existing retry behavior. Interactive, streaming, archive,
backup and tunnel paths retain their current subprocess/I/O lifetimes but use that same policy.
Delete superseded builders once all consumers move; do not force streams through buffered capture.

The shared policy ignores ambient config with `-F none`, disables connection sharing, selects only
the configured identity, disables agent/X11 forwarding and uses explicit trust files. Operation
options select PTYs, EOF or inherited input, environment, timeouts/keepalives and requested
forwards. Do not use ClearAllForwardings in a way that removes requested tunnel listeners; request
ExitOnForwardFailure for tunnel setup. Validate actual identity offers, including agent signing,
rather than assuming an identity filename alone excludes other keys or discovered certificates.

Fresh authentication per operation preserves
[ADR 0015](../../adrs/0015-abandon-ssh-controlmaster.md). No AsyncSSH, mux passenger/proxy client,
custom completion channel, new connection pool or background SSH service is introduced.

## Virtualization-host access

Replace Remote Lima's alias-only outer SSH target with the common explicit connection value. The
host transport remains platform-neutral; Lima keeps its login-shell PATH handling, limactl command
rendering, guest selection and transfer staging. Do not replace limactl shell/copy with direct guest
SSH or add a generic virtualization adapter hierarchy.

Audit Lima's provider-generated SSH configuration and invocation behavior on both local and remote
hosts. Operator config isolation must hold at the inner hop too. Use bounded provider-supported
configuration controls if needed; an inability to establish that boundary is an escalation, not a
reason to silently exempt it. Provider guest trust is not replaced by the outer host's trust store.
Document existing provider trust limitations without presenting them as newly verified host trust.

## Migration without a trust reset

Placement configuration supplies explicit host/user/identity/port. The operator supplies values
previously hidden in aliases; Agentworks does not run ssh -G, Match exec, or import arbitrary
config. The minimum agent selector supports an explicit endpoint, inherited SSH_AUTH_SOCK when
chosen, or the supported native platform agent. It never inherits an IdentityAgent directive.
Identity selection remains independent of agent selection, and forwarding stays disabled.

Trust migration is explicit and operator-controlled. Before an existing connection is used, copy the
applicable known_hosts files into an Agentworks-owned location and reference them explicitly. Keep
complete files rather than extracting/reinterpreting hashed names, CA patterns or revocations; keep
connection-specific trust sources scoped to that connection. Preserve a required HostKeyAlias as an
explicit connection field. If RevokedHostKeys was used, reference an owned copy of that file too,
including KRL format; OpenSSH remains responsible for interpreting it.

Migrated connections use strict host verification. Missing files, unmatched trust, changed keys or
inconsistent source policies cause refusal and migration guidance, not automatic acceptance. A
genuinely new managed target may retain the existing accept-new enrollment policy only when
explicitly established as new, never merely because its owned trust file is absent. The later
migration plan must specify paths, config fields and first-use setup before code handoff. No custom
trust parser, automatic discovery, CA-to-pin conversion or implicit update of operator files is
required. If a source policy cannot be preserved by explicit files and aliases, stop and escalate.

Use OpenSSH's version-specific default algorithms; no custom algorithm-policy surface is added.
Custom Ciphers, KexAlgorithms, MACs, HostKeyAlgorithms and authentication-signature restrictions
from operator configuration are not honored. Likewise, no ProxyCommand or ProxyJump route is
migrated; remove unused explicit jump plumbing. Operators needing those policies/routes cannot use
this reduced connection surface until a separately approved extension exists. Do not weaken policy
or silently choose another route to make a connection work.

## Diagnostics and integration

Keep SSHResult/SSHError and the merged ExecTransport/Transport split from
[#746](https://github.com/WayfarerLabs/agentworks/pull/746). Correct status-255 warnings to explain
ambiguity, preserving the numeric status and post-terminal-cleanup ordering. Neither OpenSSH process
status nor a nested limactl status establishes which connection failed. No automatic reconnect or
shared outcome migration follows from this wording correction.

Preserve [ADR 0020](../../adrs/0020-close-ssh-stdin-instead-of-forcing-a-tty.md) and
[#737](https://github.com/WayfarerLabs/agentworks/pull/737) for stdin and PTY behavior. Update
permanent transport documentation, configuration examples, help/completions and affected guide
topics with the implementation, not ahead of observable behavior.

## Verification and next artifacts

Inventory every direct SSH/scp builder and provider-inner invocation. Behavioral fixtures must
exercise hostile config, multiple agent keys, explicit trust/mismatch/revocation, stdin and binary
streams, PTY restoration, copy/tunnel cleanup and unchanged exit codes. Include a migration with
existing trust so an empty-store reset cannot pass unnoticed. Verify all listeners before tunnel
readiness and preserve operation-owned access holds through cleanup.

The implementation plan will map these checks to invocation families and workstation/platform beds,
with observed results and explicit gaps. Keep protocol experiments out of acceptance: no exact
classification or reconnect is being delivered. The remaining plan/migration details are ordinary
implementation preparation, not permission to add an SSH protocol implementation. If inner isolation
or trust migration requires substantial redesign, return to the operator.

Independent project and complexity reviews plus gates precede this draft checkpoint. Its new
two-round feedback allowance applies after republication. No lockfile or implementation-completion
claim belongs in this artifact handoff.

## Primary references

- [OpenSSH client manual](https://man.openbsd.org/ssh.1): config isolation and ambiguous process
  status 255.
- [OpenSSH configuration manual](https://man.openbsd.org/ssh_config.5): identity, agent selection,
  explicit trust files, algorithm defaults, forwarding and connection sharing.
- [OpenSSH copy manual](https://man.openbsd.org/scp.1): copy options and explicit SSH configuration.

This reduced design follows the existing OpenSSH execution pattern; a new SSH-library selection
study is not an implementation dependency.
