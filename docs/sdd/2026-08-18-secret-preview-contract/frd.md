# FRD: Value-free secret resolution preview

- Status: Draft for review
- Date: 2026-08-18
- Amended: 2026-08-19
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
- **Execution fact**: an objective capability of the current process, such as whether usable
  terminal input exists. An execution fact does not grant consent.
- **Preview**: a value-free attempt to determine a source's resolution disposition for a named
  secret. A backend may acquire a value internally and discard it before returning.
- **Missing**: the backend performed a valid lookup and established ordinary absence. It is safe to
  try the next source.
- **Indeterminate**: the backend exhausted every permitted route, but broader operator-impact
  authority could change the answer. It is not failure or absence.
- **Blocked**: an execution, authority, readiness, or applicability limitation prevents resolution
  in the current operation. A source-level block may fall through; the limiting reason is retained.
  A chain with no candidate is aggregate `blocked/no-candidate`, never ordinary missing.
- **Failed**: the configured lookup or provider operation failed. The source chain stops so a lower
  precedence source cannot hide a broken higher precedence source.
- **Definitive disposition**: any result other than `indeterminate`. It says no broader
  operator-impact allowance can improve this attempt, but it does not promise an existence judgment:
  `blocked` and `failed` remain possible.

## Requirements

- R1. Backend preview returns one closed tagged result: `available`, `missing`, `indeterminate`,
  `blocked`, or `failed`. The result carries no resolved value.
- R2. The only caller-controlled policy dimension passed to preview is the allowed operator impact.
  There is no requested-certainty flag, TTY policy, or equivalent second input.
- R3. A backend always goes as far as it safely can within the allowed operator impact. It returns
  `indeterminate` only after exhausting every permitted route and only when additional authority
  could change the outcome.
- R4. The maximum operator-impact allowance guarantees a definitive disposition: a conforming
  backend never returns `indeterminate` at that level. It does not turn provider failure, timeout,
  missing terminal input, or invalid configuration into an existence judgment.
- R5. Usable terminal input is an execution fact. Its absence never lowers operator consent, never
  prevents an out-of-band backend from running by itself, and prevents an stdin-reading backend from
  attempting a read or hanging.
- R6. Missing terminal input is `blocked/tty-unavailable`, distinct from ordinary `missing`,
  `indeterminate`, and `failed`. It is an expected execution limitation, not a provider exception.
- R7. A backend may fetch a secret value to establish presence, but the value is discarded inside
  the backend boundary. Preview never returns a value to the resolution core, CLI, renderer,
  machine-output projection, exception, or log.
- R8. Backends receive intent and decide how to honor it using provider knowledge. Source config may
  classify backend-specific actions for impact purposes, including an operator choice that treats
  1Password app authentication as non-disruptive.
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
  - preflight requests `OperatorImpact.NONE`, accepts `available` or `indeterminate`, and rejects
    `missing`, `blocked`, or `failed`;
  - default inspection and doctor requests are non-disruptive and may report `indeterminate`;
  - explicit inspection or verification opt-in requests maximum impact and therefore receives no
    `indeterminate` result;
  - actual resolution remains authoritative and delivers a value only through its existing scoped
    resolution boundary.
- R12. Ordinary resolving commands allow operator impact unless the operator selected global
  `--non-interactive`. This choice is independent of whether stdin is a TTY. `secret verify` retains
  its refusal-shaped default and explicit `--allow-interaction` opt-in.
- R13. Preview respects active-source order, source readiness, mapping applicability, hard-failure
  versus fallthrough semantics, and first-source-wins behavior. Earlier uncertainty must not be
  hidden by a later source. A failed configured source must not be hidden by fallback.
- R14. Invalid mapping structure fails configuration validation when knowable there. A mapping that
  passes structural validation but is rejected by its provider returns `failed/invalid-mapping` and
  hard-stops. A lookup returns `missing` only when the backend can establish ordinary absence. An
  ambiguous provider error fails closed and hard-stops rather than pretending to be missing.
- R15. The secret-backend contract and every in-tree implementation are rewritten atomically. There
  is no compatibility adapter, deprecation track, or parallel old/new runtime. The descriptor and
  every implementation declare `contract_version = 1`; this pre-external-plugin rewrite establishes
  that value as the sole supported secret-backend contract version.
- R16. Human and machine-facing diagnostics distinguish `indeterminate`, missing TTY, provider or
  mapping failure, and ordinary absence without exposing provider text or secret data.
- R17. Permanent backend-authoring, operator, CLI, JSON, completion, sample-config, and guide
  collateral changes ship with the code that makes them true. The secret-backend README becomes the
  self-contained permanent contract authority for result variants, reason ownership, core flow,
  impact and terminal rules, lifecycle constraints, value containment, conformance, and a complete
  implementation example; it does not depend on this SDD.

## Acceptance criteria

- AC1. With a ready 1Password source and no TTY, an ordinary resolving command can invoke `op read`
  and complete after an out-of-band app approval when global `--non-interactive` is not set.
- AC2. With no TTY, the prompt backend never reads stdin. Preview reports `blocked/tty-unavailable`,
  and actual resolution falls through with the same truthful cause.
- AC3. Global `--non-interactive` prevents any action that the selected backend and source config
  classify as operator impact, while still allowing work known not to require an operator.
- AC4. A non-disruptive preview reports `indeterminate/operator-impact-limited` only after the
  backend has exhausted every permitted way to answer.
- AC5. A maximum-impact preview returns no `indeterminate` rows. It returns `available` or `missing`
  when existence is established, `blocked` for an execution limitation, and `failed` for timeout,
  invalid mapping, provider, value, protocol, or unexpected failure.
- AC6. `agw secret describe NAME --allow-interaction` and
  `agw secret verify NAME --allow-interaction` can request a maximum-impact, value-free disposition.
  Their default forms do not authorize operator impact.
- AC7. Preflight fails for `missing`, `blocked`, or `failed` aggregate results and proceeds for
  `available` or `indeterminate`. Resolution still completes before the consuming operation mutates
  external state.
- AC8. Unambiguous ordinary absence falls through to a lower source. An invalid 1Password reference
  produces `failed/invalid-mapping`, stops the chain, and is not hidden by a lower source. Ambiguous
  1Password not-found text also fails closed unless sanitized real-provider evidence establishes a
  narrower stable absence marker for the supported CLI version.
- AC9. Provider authentication, connectivity, external, and deadline failures hard-stop the current
  secret's source chain by default. No generic warn-and-continue policy exists in this contract.
- AC10. Sentinel secret values do not appear in preview objects, serialized output, human output,
  exceptions, logs, or representations, including when a backend fetched and discarded the value.
- AC11. Backend conformance rejects malformed tagged results, invalid result maps, provider-authored
  text, legacy result shapes, preview `indeterminate` at maximum impact, and actual-resolution
  `blocked/operator-impact-limited` at maximum impact before those results reach an operator
  surface.
- AC12. Existing env-var, prompt, and OnePassword source precedence remains intact; ordinary absence
  and execution blocks retain fallback, while provider and mapping failures retain or gain explicit
  hard-stop behavior.

## Non-goals

- Predicting whether a specific biometric mechanism, app dialog, MFA flow, or provider UI will
  appear when the provider itself cannot know before invocation.
- Returning, hashing, comparing, persisting, or rendering previewed values.
- Treating readiness as proof that a particular secret exists.
- Adding a generic provider message, remediation string, arbitrary metadata bag, backend-selected
  halt flag, or backend-selected fallback policy.
- Adding an outage fallback mode. A future operator-configured fallback policy would be a separate
  core/source design, not an implicit backend decision.
- Making every provider operation side-effect-free. The guarantee is bounded operator impact and
  value containment, not zero network traffic or zero provider audit events.
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
- There are no external secret-backend plugins. Rewrite the contract and all implementations in one
  atomic change and reset the secret-backend descriptor and implementations from the current
  internal sentinel `2` to `1`.
