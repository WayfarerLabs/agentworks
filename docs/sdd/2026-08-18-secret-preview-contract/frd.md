# FRD: Value-free secret resolution preview

- Status: Draft for review
- Date: 2026-08-18
- Parent saga: `docs/sdd/2026-08-04-next-steps/`
- Seed problem: `task-2026-08-18-non-tty-secret-resolution.md`
- Requirements owner: operator
- Effort lead: `agw-ns-secrets`

## Purpose

Agentworks needs to answer whether a secret would resolve without delivering its value to the caller
and without surprising the operator with work they did not authorize. The current model cannot do
that faithfully because one backend-level `interactive` boolean stands for both operator impact and
terminal capability. That makes an out-of-band 1Password approval unusable from a non-TTY command
while treating a stdin prompt as the same kind of event.

This effort replaces that conflation with a backend preview contract. Core states how much operator
impact a preview may cause. The backend uses its provider knowledge and current execution facts to
produce its best value-free answer within that allowance. Certainty is an output, never a second
caller policy.

## Terminology

- **Operator impact**: an action the operator would have to take, such as answering a prompt,
  approving a request, or completing biometric authentication, as classified by the backend and its
  source config.
- **Execution fact**: an objective capability of the current process, such as whether usable
  terminal input exists. An execution fact does not grant consent.
- **Preview**: a value-free attempt to answer whether a source would resolve a named secret. A
  backend may acquire a value internally and discard it before returning.
- **Definitive preview**: a preview whose answer is `yes` or `no`. A typed `no` detail can identify
  an operational limitation such as missing terminal input, authentication failure, or timeout.

## Requirements

- R1. A backend preview returns exactly one of `yes`, `no`, or `maybe`. The answer and its closed,
  typed detail carry no resolved value.
- R2. The only caller-controlled policy dimension passed to preview is the allowed operator impact.
  There is no `allow_maybe`, requested-certainty, TTY policy, or equivalent second input.
- R3. A backend always goes as far as it safely can within the allowed operator impact. It must not
  return `maybe` merely because that answer is legal for the caller.
- R4. The maximum operator-impact allowance guarantees a definitive backend answer. A conforming
  backend never returns `maybe` at that level; it returns `yes`, or `no` with a typed reason for the
  current failure or limitation.
- R5. Usable terminal input is an execution fact. Its absence never lowers operator consent, never
  prevents an out-of-band backend from running by itself, and prevents an stdin-reading backend from
  attempting a read or hanging.
- R6. Missing terminal input is distinguishable from an ordinary negative answer through a closed
  detail. It is an expected result, not a provider exception and not a synonym for mapping absence.
- R7. A backend may fetch a secret value to establish presence, but the value is discarded inside
  the backend boundary. Preview never returns a value to the resolution core, CLI, renderer,
  machine-output projection, exception, or log.
- R8. Backends receive intent and decide how to honor it using provider knowledge. Source config may
  classify backend-specific actions for impact purposes, including an operator choice that treats
  1Password app authentication as non-disruptive.
- R9. Backend preview results contain no remediation field and no free-form failure text. Core
  derives command-specific hints from closed details. Provider-native output remains inside the
  backend.
- R10. Callers own fixed semantics:
  - preflight requests a non-disruptive preview, rejects only a definitive aggregate `no`, and
    accepts `maybe` as not disproven;
  - default inspection and doctor requests are non-disruptive and may report `maybe`;
  - an explicit inspection or verification opt-in requests maximum impact and therefore receives a
    definitive answer;
  - actual resolution remains authoritative and delivers a value only through its existing scoped
    resolution boundary.
- R11. Ordinary resolving commands allow operator impact unless the operator selected global
  `--non-interactive`. This choice is independent of whether stdin is a TTY. `secret verify` retains
  its refusal-shaped default and explicit `--allow-interaction` opt-in.
- R12. Preview respects active-source order, source readiness, mapping applicability, hard-failure
  versus fallthrough semantics, and first-source-wins behavior. Earlier uncertainty must not be
  hidden by a later source.
- R13. The secret-backend contract and every in-tree implementation are rewritten atomically. There
  is no compatibility adapter, deprecation track, or parallel old/new runtime. The contract remains
  in its current 1.0 generation rather than treating this pre-external-plugin rewrite as a new
  compatibility version.
- R14. Human and machine-facing diagnostics distinguish `maybe`, missing TTY, operator-impact
  limits, provider failures, and ordinary absence without exposing provider text or secret data.
- R15. Permanent backend-authoring, operator, CLI, JSON, completion, sample-config, and guide
  collateral changes ship with the code that makes them true.

## Acceptance criteria

- AC1. With a ready 1Password source and no TTY, an ordinary resolving command can invoke `op read`
  and complete after an out-of-band app approval when global `--non-interactive` is not set.
- AC2. With no TTY, the prompt backend never reads stdin. Preview reports `no/tty-unavailable`, and
  actual resolution fails or falls through with the same truthful cause.
- AC3. Global `--non-interactive` prevents any action that the selected backend and source config
  classify as operator impact, while still allowing work known not to require an operator.
- AC4. A non-disruptive preview reports `maybe/operator-impact-limited` only after the backend has
  exhausted every permitted way to answer.
- AC5. A maximum-impact preview returns no `maybe` rows. Network failure, authentication failure,
  timeout, bad mapping, missing TTY, and absence are definitive typed `no` outcomes for that
  attempt.
- AC6. `agw secret describe NAME --allow-interaction` and
  `agw secret verify NAME --allow-interaction` can request a definitive, value-free answer. Their
  default forms do not authorize operator impact.
- AC7. Preflight fails when the active chain is definitively unable to resolve a required secret and
  proceeds when the answer is `yes` or `maybe`. Resolution still completes before the consuming
  operation mutates external state.
- AC8. Sentinel secret values do not appear in preview objects, serialized output, human output,
  exceptions, logs, or representations, including when a backend fetched and discarded the value.
- AC9. Backend conformance rejects a maximum-impact `maybe`, malformed result maps,
  provider-authored text, or legacy contract shape before those results reach an operator surface.
- AC10. Existing env-var, prompt, and OnePassword source precedence and hard-versus-soft failure
  behavior remain intact except where this FRD deliberately changes impact and preview semantics.

## Non-goals

- Predicting whether a specific biometric mechanism, app dialog, MFA flow, or provider UI will
  appear when the provider itself cannot know before invocation.
- Returning, hashing, comparing, persisting, or rendering previewed values.
- Treating readiness as proof that a particular secret exists.
- Adding a generic provider message, remediation string, or arbitrary metadata bag to preview.
- Making every provider operation side-effect-free. The guarantee is bounded operator impact and
  value containment, not zero network traffic or zero provider audit events.
- Reworking secret-source declaration, source precedence, or credential-minting boundaries.

## Settled operator rulings

- Preview and definitive probing are one backend method. The operator-impact allowance determines
  what the backend may do; certainty is the result.
- A backend may fetch and discard a value internally. Full value-bearing resolution through core or
  the CLI is not a valid preview implementation.
- `maybe` is a necessary answer, and non-disruptive consumers accept it.
- Maximum impact guarantees a definitive answer, while typed details preserve why a definitive `no`
  occurred.
- Missing TTY is distinct from an ordinary `no`.
- Backend remediation and free-form failure prose are unnecessary in the initial contract. Core can
  derive a hint from a closed detail.
- There are no external secret-backend plugins. Rewrite the contract and all implementations in one
  atomic change, with contract versions remaining 1.0.
