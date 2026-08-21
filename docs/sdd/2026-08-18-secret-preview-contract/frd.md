# FRD: Value-free secret resolution preview

- Status: Draft for review
- Date: 2026-08-18
- Amended: 2026-08-21
- Parent saga: `docs/sdd/2026-08-04-next-steps/`
- Seed problem: `task-2026-08-18-non-tty-secret-resolution.md`
- Requirements owner: operator
- Effort lead: `agw-ns-secrets`
- Acting role: operator-authorized effort lead

## Purpose

Agentworks needs to answer whether a secret would resolve without delivering its value to the caller
and without surprising the operator with work they did not authorize. The current model cannot do
that faithfully because one backend-level `interactive` boolean stands for both operator impact and
terminal capability. That makes an out-of-band 1Password approval unusable from a non-TTY command
while treating a stdin prompt as the same kind of event.

This effort replaces that conflation with a backend preview contract. Core states how much operator
impact a preview may cause. The backend uses its provider knowledge and current execution facts to
produce its best value-free result within that allowance. Certainty is an output, never a second
caller policy.

The result is not a yes/no flag. It distinguishes ordinary absence from uncertainty, an execution
limitation, and a failure. That distinction lets core preserve normal source fallback while stopping
on evidence that a configured source is broken.

## Terminology

- **Operator impact**: an action the operator would have to take, such as answering a prompt,
  approving a request, or completing biometric authentication, as classified by the backend and its
  source config.
- **TTY interaction access**: whether core may expose terminal input to a backend. It is available
  only when usable terminal input exists and global `--non-interactive` is absent. The flag means
  exactly "do not use the TTY for interactions, even if one is present." It says nothing about
  biometric approval, an app dialog, or any other out-of-band operator action.
- **Preview**: a value-free attempt to determine a source's resolution disposition for a named
  secret. A backend may acquire a value internally and discard it before returning.
- **Missing**: the backend performed a valid lookup and established ordinary absence. It is safe to
  try the next source.
- **Indeterminate**: the backend exhausted every permitted route, but broader operator-impact
  authority could change the answer. It is not failure or absence.
- **Blocked**: an execution, TTY-access, readiness, or applicability limitation prevents the lookup
  in the current operation. A source-level block may fall through; the limiting reason is retained.
  A chain with no candidate is the core-owned aggregate `blocked/no-candidate`, never an attempt
  result or ordinary missing.
- **Failed**: the configured lookup or provider operation failed. The source chain stops so a lower
  precedence source cannot hide a broken higher precedence source.
- **Definitive disposition**: any result other than `indeterminate`. It says no broader
  operator-impact allowance can improve this attempt, but it does not promise an existence judgment:
  `blocked` and `failed` remain possible.

## Requirements

- R1. Backend preview returns one closed tagged result: `available`, `missing`, `indeterminate`,
  `blocked`, or `failed`. The result carries no resolved value.
- R2. The only policy that controls how much operator impact preview may cause is the allowed
  operator impact. There is no requested-certainty flag. TTY interaction access is orthogonal: it
  controls only terminal use and never lowers permission for out-of-band actions.
- R3. A backend always goes as far as it safely can within the allowed operator impact. It returns
  `indeterminate` only after exhausting every permitted route and only when additional authority
  could change the outcome.
- R4. The maximum operator-impact allowance guarantees a definitive disposition: a conforming
  backend never returns `indeterminate` at that level. It does not turn provider failure, timeout,
  missing terminal input, or invalid configuration into an existence judgment.
- R5. TTY interaction access has three exact states: available, physically unavailable, or disabled
  by global `--non-interactive`. Neither unavailable nor disabled lowers operator-impact permission,
  prevents an out-of-band backend from running, or authorizes a backend to read stdin. A backend
  receives a prompt broker only when it declares that it supports TTY interaction, access is
  available, and the selected operation permits prompting.
- R6. Missing terminal input is `blocked/tty-unavailable`; explicit terminal refusal is
  `blocked/tty-interaction-disabled`. Both are distinct from ordinary `missing`, `indeterminate`,
  and `failed`, and neither is a provider exception.
- R7. A backend may fetch a secret value to establish presence, but the value is discarded inside
  the backend boundary. Preview never returns a value to the resolution core, CLI, renderer,
  machine-output projection, exception, or log.
- R8. Backends receive intent and decide how to honor it using provider knowledge. Source config may
  classify backend-specific actions for impact purposes, including an operator choice that treats
  1Password app authentication as non-disruptive. The exact static `supports_tty_interaction`
  capability grants only eligibility to receive a broker; it never predicts whether interaction will
  occur and never gates non-TTY provider work.
- R9. Backend results contain no remediation field, free-form failure text, provider message,
  arbitrary metadata, or caller-flow instruction. Core derives command-specific hints from the
  closed result tag and reason.
- R10. Backends classify semantic outcomes; core owns their fixed disposition:
  - `available` stops successfully;
  - `missing` falls through silently;
  - `indeterminate` falls through during preview and preserves higher-precedence uncertainty;
  - `blocked` falls through and preserves the reason if the chain exhausts;
  - `failed` stops the chain immediately.
- R11. Callers own fixed preview semantics:
  - preflight requests `OperatorImpact.NONE`, accepts `available` or `indeterminate`, rejects
    `missing` or `blocked`, and rejects `failed` unless an earlier higher-precedence attempt is
    indeterminate;
  - default inspection and doctor requests are non-disruptive and may report `indeterminate`;
  - explicit inspection or verification opt-in requests maximum impact and therefore receives no
    `indeterminate` result;
  - actual resolution remains authoritative and delivers a value only through its existing scoped
    resolution boundary.
- R12. Actual resolution has no operator-impact allowance. It may perform provider work that causes
  out-of-band operator action, including biometric or app approval. Global `--non-interactive` means
  exactly "do not use the TTY for interactions, even if one is present"; it disables prompt input
  and does not alter presentation, color, or out-of-band behavior. Preview's `--allow-interaction`
  opt-in is orthogonal and may be combined with global `--non-interactive` to allow out-of-band work
  while keeping TTY interaction disabled.
- R13. Preview respects active-source order, source readiness, mapping applicability, hard-failure
  versus fallthrough semantics, and first-source-wins behavior. The aggregate reports the
  current-impact disposition: a later success is `available`, and a later hard failure is `failed`.
  Earlier uncertainty remains visible in ordered attempts but does not mask either disposition. A
  failed configured source must not be hidden by fallback.
- R14. Invalid mapping structure fails configuration validation when knowable there. A mapping that
  passes structural validation but is rejected ambiguously by its provider returns
  `failed/lookup-rejected` and hard-stops. A lookup returns `missing` only when the backend can
  establish ordinary absence. An ambiguous provider error fails closed and hard-stops rather than
  pretending to be missing.
- R15. The secret-backend contract and every in-tree implementation are rewritten atomically. There
  is no compatibility adapter, deprecation track, or parallel old/new runtime. The descriptor and
  every implementation declare `contract_version = 1`; this pre-external-plugin rewrite establishes
  that value as the sole supported secret-backend contract version. The broad `interactive` flag is
  gone; `supports_tty_interaction` is the only static TTY-broker capability.
- R16. Human and machine-facing diagnostics distinguish `indeterminate`, missing TTY, provider or
  mapping failure, and ordinary absence without exposing provider text or secret data.
- R17. Permanent backend-authoring, operator, CLI, JSON, completion, sample-config, and guide
  collateral changes ship with the code that makes them true. The secret-backend README becomes the
  self-contained permanent contract authority for result variants, reason ownership, core flow,
  preview impact, TTY broker capability and access rules, lifecycle constraints, value containment,
  conformance, and a complete implementation example; it does not depend on this SDD.
- R18. The existing JSON v1 shapes remain compatible. Both `secret describe`'s
  `source_mappings[].would_attempt` and `secret list`'s `sources[].would_attempt` remain additive
  compatibility projections derived from structured lookup disposition. Secret checks in doctor may
  add an optional closed `secret_preview` object without changing existing fields.

## Acceptance criteria

- AC1. With a ready 1Password source and no TTY, an ordinary resolving command invokes `op read` and
  can complete after an out-of-band app approval whether or not global `--non-interactive` is set.
- AC2. With no TTY, the prompt backend never reads stdin. Preview reports `blocked/tty-unavailable`,
  and actual resolution falls through with the same truthful cause.
- AC3. With a usable TTY and global `--non-interactive`, prompt performs no broker or stdin access
  and reports `blocked/tty-interaction-disabled`; env-var and OnePassword resolution proceed
  normally. A fake OnePassword path that records a biometric-equivalent approval is invoked in this
  mode. Human output retains the same color decision it would make without the flag.
- AC4. A non-disruptive preview reports `indeterminate/operator-impact-limited` only after the
  backend has exhausted every permitted way to answer.
- AC5. A maximum-impact preview returns no `indeterminate` rows. It returns `available` or `missing`
  when existence is established, `blocked` for an execution limitation, and `failed` for timeout,
  invalid mapping, provider, value, protocol, or unexpected failure.
- AC6. `agw secret describe NAME --allow-interaction` and
  `agw secret verify NAME --allow-interaction` can request a maximum-impact, value-free disposition.
  Their default forms do not authorize operator impact.
- AC7. Preflight fails for `missing` or `blocked` aggregate results and proceeds for `available` or
  `indeterminate`. It fails for `failed` unless that aggregate retains an earlier higher-precedence
  indeterminate attempt; in that case it proceeds because greater authority could avoid reaching the
  failure. Resolution still completes before the consuming operation mutates external state.
- AC8. Unambiguous ordinary absence falls through to a lower source. A locally invalid 1Password
  reference produces `failed/invalid-mapping`; an ambiguous provider rejection produces
  `failed/lookup-rejected`. Both stop the chain and are not hidden by a lower source. Ambiguous
  1Password not-found text remains rejected unless sanitized real-provider evidence establishes a
  narrower stable absence marker for the supported CLI version.
- AC9. Provider authentication, connectivity, external, and deadline failures hard-stop the current
  secret's source chain by default. No generic warn-and-continue policy exists in this contract.
- AC10. Sentinel secret values do not appear in preview objects, serialized output, human output,
  exceptions, logs, or representations, including when a backend fetched and discarded the value.
- AC11. Backend conformance rejects malformed tagged results, invalid result maps, provider-authored
  text, legacy result shapes, preview `indeterminate` at maximum impact, and preview-only reasons in
  actual-resolution results before those results reach an operator surface. It also rejects a TTY
  block from a backend that declares no TTY-interaction support.
- AC12. Existing env-var, prompt, and OnePassword source precedence remains intact; ordinary absence
  and execution blocks retain fallback, while provider and mapping failures retain or gain explicit
  hard-stop behavior.
- AC13. Actual resolution performs one authoritative source-first pass without preview staging or an
  operator-impact frontier. It completes before the consuming operation mutates external state, and
  it never reuses a preview result or discarded preview value.
- AC14. Core validates every backend-produced diagnostic identifier before retaining or rendering
  it. Control, format, line-separator, and paragraph-separator characters cannot forge an output
  row.

## Non-goals

- Predicting whether a specific biometric mechanism, app dialog, MFA flow, or provider UI will
  appear when the provider itself cannot know before invocation.
- Returning, hashing, comparing, persisting, or rendering previewed values.
- Treating readiness as proof that a particular secret exists.
- Adding a generic provider message, remediation string, arbitrary metadata bag, backend-selected
  halt flag, or backend-selected fallback policy.
- Adding an outage fallback mode. A future operator-configured fallback policy would be a separate
  core/source design, not an implicit backend decision.
- Making every provider operation side-effect-free. Preview is bounded by its operator-impact
  allowance; actual resolution is not. Both retain value containment and bounded provider work, not
  zero network traffic or zero provider audit events.
- Reworking secret-source declaration, source precedence, or credential-minting boundaries.

## Settled operator rulings

- Preview and maximum-impact probing are one backend method. The operator-impact allowance
  determines what the backend may do; certainty is a result.
- A backend may fetch and discard a value internally. Full value-bearing resolution through core or
  the CLI is not a valid preview implementation.
- `indeterminate` is the uncertainty term. A backend uses it only when broader impact could improve
  the answer after all permitted work has been exhausted.
- Maximum impact guarantees a definitive disposition, not a successful provider call or a forced
  yes/no existence judgment.
- Ordinary absence, execution blockage, and failure are separate tagged results. Missing falls
  through, blocked falls through with evidence, and failed hard-stops.
- A chain with no candidate is `blocked/no-candidate`; it did not perform a lookup and is not
  missing.
- Backends report semantic facts; they do not select halt behavior, remediation, or free-form prose.
- Missing TTY is distinct from ordinary absence and provider failure.
- Global `--non-interactive` means exactly "do not use the TTY for interactions, even if one is
  present." It does not prohibit a biometric unlock, app approval, or any other out-of-band operator
  action, and it does not change output presentation. Actual resolution has no operator-impact
  policy; impact classification exists only for preview.
- This authenticated operator ruling from 2026-08-21 supersedes the seed requirement for an explicit
  fully unattended, fail-fast resolution path. This effort intentionally defines no general
  unattended-resolution mode; adding one requires separate operator authority and design.
- There are no external secret-backend plugins. Rewrite the contract and all implementations in one
  atomic change and reset the secret-backend descriptor and implementations from the current
  internal sentinel `2` to `1`. This is authenticated operator direction from 2026-08-19: because
  the sentinel is registration-only and no external implementation exists, `1` is deliberately
  re-established as the sole supported pre-publication contract rather than minting `3`.
