# LLD (e): operator surfaces (secret CLI + doctor)

Implements HLA [component 8](./hla.md). Owns the `secret list` / `secret describe` output, the new
doctor secret-backends group, the readiness-aware secret rows, the exact operator strings, the
`--reveal-secrets` to `--resolve` rename (and the alias decision), the docs, and the completion
regen. Governs FRD R6 (surface strings), R9.1, R9.7, R9.8.

**Acceptance line (called out first):** the **interactive-optimism preview is unchanged**. Readiness
is a new **offline, honest** layer _under_ the optimistic interactivity preview; a `prompt` /
biometric backend is still previewed optimistically on `would_attempt` alone (`backends.py:80-81`).
Readiness (offline: is `op` on PATH) and interactivity (the interaction, optimistically assumed) are
orthogonal, and no surface conflates them.

## The vocabulary every surface must keep straight (the hotspot)

A backend, for a given secret, is:

- **present**: a node exists (a built-in, or an installed plugin whether or not enabled).
- **enabled**: turned on (the plugin / three-tier axis; "enabled/disabled" lives **here only**). No
  backend is ever disabled in this effort; latent until the plugin work.
- **ready**: host-usable (its offline `not_ready`, e.g. `op` on PATH).
- **opted-in**: named in `secret_config.backends` (the resolution chain, selection + order).
- **would-attempt**: has a mapping, or is mapping-optional, for **this** secret (a pure
  `(secret, mapping)` function).

Each surface states exactly which it means. "enabled/disabled" is **never** reused for opt-in or
readiness.

## `secret list` grid (R9.7)

Columns stay the **opted-in** backends (`secret_config.backends`), in chain order (unchanged). A
not-opted-in backend has **no column**. The cell for an opted-in backend, replacing today's
overloaded `disabled` / `enabled` literals (`secrets/inspect.py:191-200`):

| Condition                                                                      | Cell                                                           |
| ------------------------------------------------------------------------------ | -------------------------------------------------------------- |
| would-attempt, has a static identifier                                         | the identifier (`AW_SECRET_X`, `op://...`), truncated as today |
| would-attempt, no static identifier (e.g. `prompt`)                            | `would attempt` (replaces today's bare `enabled`)              |
| **not ready** (host tool missing)                                              | `not ready: <reason>` (e.g. `not ready: op not installed`)     |
| won't-attempt (`false` opt-out, or a mapping-required backend with no mapping) | `won't attempt` (replaces today's overloaded `disabled`)       |

Precedence when both apply: **not-ready wins over the identifier** (an `onepassword` column for a
secret with an `op://` mapping shows `not ready: op not installed`, not the ref, because it cannot
be used here). Won't-attempt and not-ready are mutually exclusive with would-attempt. Enablement and
not-opted-in are **not** grid states (a disabled or not-opted-in backend never has a column); they
surface in `secret describe` and the doctor backend group.

The two empty-state messages are unchanged (`No secrets in the resource registry.`,
`No active secret backends.`).

## `secret describe` (R9.1)

"Backend mappings" and "Resolution preview" become readiness-aware:

- **Backend mappings**: each mapped backend's line notes `(not ready: <reason>)` when its node is
  not-ready; the mapping itself is still shown (the config is real; it just can't run here now).
- **Resolution preview**: walks **present ∧ enabled ∧ ready ∧ opted-in** candidates in chain order.
  A not-ready backend is shown as skipped (`skipped: not ready: <reason>`) and **does not** count
  toward "would resolve via X". The optimistic interactivity preview is preserved: a ready `prompt`
  in the chain still previews as the resolving backend (readiness is honest; interactivity stays
  optimistic).
- Enablement (future) and not-opted-in backends are shown here (describe is the detail view),
  clearly labeled with their own axis words, never as "disabled" for readiness.

## Doctor (R6, R9.1)

- **New secret-backends group**, parallel to `_check_vm_platforms`: one readiness row per backend,
  `[ok]` or `[not ready]: <reason>`, reading the **stored** `readiness_of` off the graph (backends
  are capabilities now). This is the R9.7-promised backend visibility.
- **`_check_vm_platforms` / `_check_vm_sites`** read stored readiness off the graph instead of
  recomputing `unsupported_reason` / `site_disabled_reason` ad hoc (`doctor.py:229,272`). The live
  `preflight` (network) stays the deeper op-boundary check, now cleanly separated from the offline
  verdict. An installed host-unsupported platform now shows as a not-ready row (R9.5), where today
  it is absent.
- **`_check_secrets`** stays one row per secret but becomes readiness-aware (a secret whose only
  opted-in backend is not-ready is flagged as at-risk, consistent with the resolution skip). Note
  the R9.11 granularity regression: a `backend_mappings.<typo>` now fails `build_registry`
  **inside** doctor, collapsing the registry-dependent tail to one "Resource registry: FAIL" row
  instead of pinpointing the secret. This is acknowledged, acceptable (the typo is a hard error
  everywhere now), and pinned by a test.
- Every remaining "disabled" doctor string for host readiness (`doctor.py:231,245,261,275,298,322`)
  adopts the readiness vocabulary (R6).

## `env show --reveal-secrets` to `--resolve` (R9.8)

- Rename the flag to `--resolve`, aligning with the resolution vocabulary.
- **Alias decision: keep `--reveal-secrets` as a hidden, deprecated alias** for one release cycle,
  emitting a single deprecation warning to stderr when used
  (`--reveal-secrets is deprecated; use --resolve`). This spares operators' existing scripts and
  muscle memory a hard break. Because the old flag still works, the change is **not** a
  `BREAKING CHANGE` for release-please; the alias is removed at the next major with a proper
  breaking note. The help text documents `--resolve`; the alias is hidden from help.
- The `preview_resolution` **preflight predictor** (`orchestration/secrets.py:85`) becomes
  readiness-aware in lockstep (the same readiness-aware `would_attempt` predicate as LLD d's walk),
  so it never predicts "would resolve via onepassword" for a backend resolution will skip.

## Docs and completions (DoD-docs, the always-consider rules)

Updated **in phase 5**, lockstep with the surface change that makes each claim true:

- `docs/guides/resources.md` "Secrets: backends and the chain": document the present / enabled /
  ready / opted-in / would-attempt vocabulary, the not-ready skip-with-warning resolution behavior,
  and the offline backend readiness. This is the **permanent home** for the model (the
  SDD-not-permanent promotion; do not anchor it to the SDD path).
- `sample-config.toml`: the `secret_config.backends` opt-in chain comment; note that a not-ready
  backend is skipped at resolution (always-consider-sample-config rule).
- `cli/README.md` (~line 787): the `--reveal-secrets` mention becomes `--resolve` (note the alias).
- Command/section help strings for `secret list`/`describe`, `env show`, and `doctor`.
- **Completions** (always-consider-completions rule): regenerate the completion tree; the
  `--resolve` rename should flow through the Typer-extracted spec, **verify** it does and that the
  deprecated hidden alias is handled sanely (hidden aliases should not clutter completions).

## `sessions/templates.py:175` note

The resolve-time `harness_for(...).validate_config(...)` call (caller inventory section A) is
confirmed here: it validates a **resolved** session template's harness config at template-resolution
time, distinct from the finalize `validate` pass over declared rows. It splits to `validate(config)`
(same throwing shape). Whether it is redundant with the finalize pass depends on whether resolved
templates are always also declared rows; the phase-1/phase-4 dev confirms and, if redundant, removes
it (a resolved template that is a finalized row is already validated), else keeps it as the
resolved-view check. Either way it is not a new behavior, just the split shape.

## Acceptance

- R9.7: the grid shows `would attempt` / identifier / `not ready: <reason>` / `won't attempt`, never
  bare `enabled` / `disabled`; not-ready wins over the identifier.
- R9.1: no operator-facing "disabled" string denotes host readiness anywhere; "enabled/disabled" is
  reserved for opt-in.
- R9.8: `--resolve` works; `--reveal-secrets` works with a deprecation warning and is hidden from
  help and completions.
- The doctor secret-backends group lists one readiness row per backend; a missing `op` shows
  `[not ready]: op not installed`.
- Interactive-optimism preview is unchanged (a ready `prompt` still previews as resolving; a
  biometric `op` is not probed for readiness), pinned by a test.
- Completions regenerated and verified for the `--resolve` rename.
