# Value-free secret resolution preview: locked

**Locked:** 2026-08-23

This effort is complete in PR #619. The lock takes effect when that PR lands on `main`; until then,
this file records the final reviewed and operator-accepted implementation state.

## What shipped

- The secret-backend contract is atomically rewritten as exact contract version `1`. Every in-tree
  backend implements the new contract, no compatibility adapter or alternate version remains, and no
  external secret-backend plugins exist.
- Preview is a value-free backend operation with one caller input for allowed operator impact. It
  returns one closed disposition: `available`, `missing`, `indeterminate`, `blocked`, or `failed`.
  Backends may fetch and discard a value internally, but values never cross the preview boundary.
- Actual resolution has no operator-impact input. Core passes exact TTY access to a backend and
  never predicts whether interaction will occur. Global `--non-interactive` means only "do not use
  the TTY for interactions, even if one is present"; it does not suppress app approval, biometric
  authentication, other out-of-band work, color, or presentation.
- Missing and TTY blockage fall through to later sources. A configured mapping or provider failure
  hard-stops so a broken higher-precedence source cannot be hidden. Complete resolution stops before
  futile later provider or broker work once the batch cannot succeed, while partial reveal continues
  independent names.
- Env-var, prompt, and OnePassword use the same closed preview and resolution semantics. Prompt can
  receive a broker only when it declares TTY support and exact access is available. OnePassword owns
  one shrinking configured deadline across its source batch and keeps provider text inside the
  backend boundary.
- Preflight, doctor, `secret describe`, `secret verify`, `secret list`, human rendering, and JSON v1
  projections now consume the shared structured model. Existing JSON v1 fields remain compatible;
  doctor adds an optional closed `secret_preview` object only to secret checks.

## Final design and delivery deltas

The delivered design incorporates the operator's simplifications made during review. Preview keeps
operator impact and TTY access as orthogonal facts; actual resolution carries only TTY access. The
client factory has exactly four inputs: config, intent, TTY access, and an optional interaction
broker. Dead source-identity and remaining-time inputs were removed, and source identity remains
core-owned. Static complete-batch viability considers only mappings, readiness, and plugin
enablement; TTY access remains backend-owned runtime information.

Because no external implementation exists, the internal version-2 sentinel was reset cleanly to the
sole supported contract version `1`. The return types and closed reasons distinguish ordinary
absence, impact-limited uncertainty, execution blockage, and hard failure without remediation or
free-form backend text. Core owns fallback, halt behavior, exception selection, and operator hints.

## Validation and review

The final runtime checkpoint is `bf3a9a42c4cff1f0316eef540d512786ed3e6ac6`, based directly on
`origin/main` at `3912ee6542dd15b65bc8b2cc83c7f8a5de2d086d`. The implementation and its permanent
collateral passed:

- 7,258 non-integration tests with one platform-specific skip;
- focused contract, orchestration, doctor, CLI, JSON, conformance, backend, and adversarial suites;
- Ruff check and format plus strict mypy across 696 files;
- file lint, locked-SDD, Rulesync drift, generated-package, lock, Typer-isolation, and diff gates;
- 155 Python and 103 Node website tests plus deterministic root and `/agentworks/` builds; and
- hosted CI on Python 3.12, 3.13, and 3.14, CodeQL, and the aggregate success gate.

Artifact review used the saga lead, Muntz, and integration-testability perspectives with the two
operator-authorized feedback/fix iterations. Implementation review then closed lifecycle, cleanup,
timeout, identity, map-validation, TTY-authority, precedence, batch-doom, documentation, and test
quality findings. The final reviewer-of-record and fresh-eyes passes reported no material finding at
exact head `0a4d427d`; the following `bf3a9a42` commit records their clean disposition without a
runtime change. Late feedback was collected for the full authorized 45-minute window, evaluated as
one batch, and produced no further material change.

The separately operated live run used the shipped CLI from an isolated home. One distinctive
sentinel and one capture mechanism proved eight preview surfaces silent, then proved the intended
`env show --resolve` surface revealed the sentinel on a real remote-Lima VM. Non-TTY prompt checks
returned `blocked/tty-unavailable` without hanging, default and opted-in preview surfaces remained
value-free, human and JSON results matched the closed contract, and doctor kept missing or blocked
secrets as warnings. Real VM provisioning resolved its configured Tailscale key without printing the
value. The VM and workspace were deleted; operator config, isolated-home, remote-Lima, and workspace
residue checks were clean apart from the expected offline tailnet record created with a
non-ephemeral key. The tester and saga lead reported no blocker, and the operator accepted the
report and directed closeout on 2026-08-23.

## Permanent homes and accepted limits

The permanent backend contract lives in `cli/agentworks/capabilities/secret_backend/README.md` and
its exact types, conformance checks, and in-tree implementations. Operator behavior lives in
`cli/agentworks/secrets/README.md`, `cli/README.md`, `cli/command-reference.md`,
`docs/guides/resources.md`, and `docs/guides/upgrading-to-0.14.md`. Sample configuration, generated
schema and completions, plugin authoring guidance, orchestration guidance, and ADR 0013 carry the
same semantics. Nothing in this SDD directory is required to implement, extend, or operate the
feature.

Real OnePassword authentication, desktop approval, and live `op` error-token classification were not
exercised with operator credentials. The fixed-TTY color-parity case was also not repeated in the
live campaign. Fake-provider, contract, CLI-root, and presentation suites cover those paths, and the
operator explicitly accepted these documented test limits after the no-finding live report. The
tester's notes about general SSH-readiness gating in `secret verify`, a quiet successful resolution
section, and template binding time describe existing behavior or test method, not a material defect
in this contract rewrite. No in-scope implementation or review finding remains.

The operator owns merging PR #619. The effort lead does not merge it.

-- agw-ns-secrets (lead)

## Post-lock amendment: 2026-08-24

Authenticated operator direction for PR #644 corrects the single indeterminate reason and doctor's
flat status mapping. `operator-input-required` now identifies the prompt case where usable terminal
input exists and actual resolution will request the value if it reaches that source.
`operator-impact-limited` remains the provider case where zero-impact preview skipped work that
might require operator action, so availability was not checked. Core accepts the new prompt reason
only from a TTY-capable backend at zero impact with terminal input available; every other
combination is a backend-protocol failure.

Doctor maps available and operator-input-required results to `OK`, operator-impact-limited to
`INFO`, missing and blocked to `WARN`, and failed to `FAIL`. Prompt rows state directly that the
value will be provided interactively when needed, while skipped provider rows state directly that
availability was not checked. The proposed generic numbered-note mechanism was removed rather than
added to shared health, JSON, and focused resource diagnostics. Secret descriptions and origin
markers remain absent from doctor and available through the dedicated secret inspection commands.

This amendment supersedes the earlier locked doctor table and completed plan statement that mapped
every indeterminate result to `WARN`. Those historical artifacts remain unchanged as the record of
what PR #619 originally delivered; this dated lockfile amendment records the operator-authorized
contract now implemented by PR #644.
