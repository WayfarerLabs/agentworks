# Azure Key Vault implementation plan

Delivery vehicle: one Azure feature branch and one PR. The continuing operator goal authorizes PR
delivery and up to three feedback/fix rounds per opened PR. The target is a merge-ready PR; live
validation follows the integration-testing pipeline after the ready handoff.

## Design

- [x] Trace the secret-source framework, version-3 backend contract, 1Password implementation, Azure
      plugin, and existing Azure VM authentication.
- [x] Research Azure SecretClient, credential interaction, URI validation, and provider timeout
      semantics using Microsoft documentation and source.
- [x] Establish a new effort through authenticated operator direction and draft this short SDD.
- [x] Settle explicit service-principal credential bootstrap with the operator.

## Implementation

- [x] Add typed source and mapping models, SDK client lifecycle, preview/resolution, and structured
      failure handling in the Azure plugin.
- [x] Register the capability and add the latest stable Key Vault Secrets SDK dependency.
- [x] Add behavioral coverage at provider and resolver boundaries, including exact multiline and
      empty values, version selection, missing versus denied access, preview interaction, no-I/O
      registration, cleanup, and sensitive diagnostic containment.
- [x] Update Azure plugin metadata, secret examples, sample config, and concise online-documentation
      signposts; verify plugin-owned guide discovery if adding a topic.

## Validation and review

- [x] Run focused tests, Ruff, strict mypy, relevant file linters, and required repository checks.
- [x] Run independent project review, complexity review, and generic correctness/security review at
      one pinned implementation state; disposition material findings.
- [x] Identify an authorized live test vault and credentials; exercise the shipped CLI if available,
      otherwise report that coverage gap and request a bounded test fixture.
- [x] Report the implementation, observed checks, outstanding decisions, and live-test limits.

Done means the new backend is reachable through existing commands, both authentication modes work at
their boundaries, matching docs ship with behavior, and validation evidence is accurate. Lock the
SDD only when the effort is actually complete.

## Validation and disposition

- Full Linux non-integration suite at `d971b2621`: **15,377 passed, 85 skipped**, using this
  checkout's own `.venv` with Python 3.12.16. The new backend's focused suite passed 163 cases.
- Strict mypy passed for 1,143 source files; Ruff lint and formatting passed. File-quality lint,
  Rulesync drift, and locked-SDD checks passed.
- All three independent review lanes cleared the implementation and subsequent corrections.
  Real-chain regressions cover explicit browser fallback, no-impact desktop/cache exclusions,
  credential selectors, private diagnostics, cleanup, and cancellation.
- The broader suite exposed two test expectations: the schema oracle needed to follow reference
  chains, and the generated field-reference type needed to include structured mappings. Independent
  JSON Schema validation and review confirmed both corrections without changing the product schema.
- The operator selected "No live fixture available" in thread 2, message 2. No Azure service,
  sovereign-cloud endpoint, or native macOS authentication dialog was exercised. No cloud resources
  or permissions were changed.
- Host filesystem tests required normal group semantics outside the sandbox. Unix sockets required
  short session-owned paths under `/tmp/kv-1029-wf-*`; completed fixtures are removed at closeout.
  The checkout-local development environment and its normally installed uv Python runtime remain
  available for further work.
- The optional context-wrapper simplification was declined: the small wrapper follows the existing
  backend pattern. It did not gate delivery. The unchanged closing paragraphs at
  `cli/agentworks/capabilities/secret_backend/README.md:350` still mention contract version 1
  despite its version-3 contract; this is an out-of-scope documentation finding for that document's
  owner.

## Continuing PR delivery

The initial local implementation was completed at `915a028b9`. The operator then established the
continuing secret-work goal recorded in the FRD. Its additional delivery and confirmation work
reopens this unmerged SDD; the former local-completion lock is removed. Earlier checked items above
record the work that was actually completed and remain unchanged.

- [x] Refresh `origin/main`, verify merge compatibility, and rebase the unpublished Azure branch.
      The Azure implementation and its tests are byte-identical after the clean rebase.
- [x] Run the additional local PR gates: Typer isolation, 160 Python website tests, 103 Node website
      tests, and deterministic repeated builds at root and project site bases.
- [x] Publish the ready Azure PR and record its exact head, CI state, and review request.
- [x] Receive and assess the integration tester's live Azure evidence and disposition.
- [x] Receive a complete PR-level review separate from the private quality loop.
- [ ] Batch published feedback after the process reporting window and perform at most three
      authorized fix rounds, each with its required checks, reviews, and handoff.
- [ ] Hand off a merge-ready Azure PR with current CI, review disposition, and live evidence.
- [ ] Record operator confirmation that Azure secret support is working.

The overall goal remains active until the operator confirms all secret work is complete and
everything is working. AWS, GCP, and macOS Keychain each ship separately under that goal. The
operator's thread 3 direction routes Azure live testing through the standard ready-PR pipeline.

## Published handoff and fix round 1

PR [#964](https://github.com/WayfarerLabs/agentworks/pull/964) opened ready at
`740838bd81418ee0c93a4b789aa2b549374b8bc9` on 2026-11-25 at 04:57:01 UTC. All fourteen published
checks passed. Copilot reported insufficient quota and performed no review; complete PR-level review
and live integration evidence remain pending.

A focused follow-up check reproduced an important no-stdin contract violation in ambient native
certificate and workload file reads. With synthetic input, real credentials and fake HTTP,
`/dev/stdin` was consumed at zero-impact preview with terminal access disabled. Regular-file
controls preserved stdin. The lead independently reproduced it and posted the
[critical reading](https://github.com/WayfarerLabs/agentworks/pull/964#issuecomment-7154073983).
Complexity review cleared checked descriptor reads, public SDK credential seams, and native-order
composition as a bounded correction within the accepted architecture.

The operator's continuing goal authorizes at most three feedback/fix rounds. Round 1's batch was
fixed at 2026-11-25 05:57:28 UTC, after the initial reporting window, with that material finding and
the quota report. No unresolved inline threads or live report were present. The PR returned to draft
before correction work. Later feedback belongs to the next batch.

- [x] Implement the checked credential-file read boundary while preserving native authentication
      selection, identity, supported forms, browser policy, and cleanup.
- [x] Add real SDK/HTTP-boundary regressions for regular files, unsafe aliases, refresh replacement,
      selectors, earlier successful credentials, and partial-construction cleanup.
- [x] Run required project, complexity, and correctness/security private lanes on the integrated
      round head and dispose all material findings.
- [x] Run applicable required gates on the cleanup-corrected candidate.
- [x] Publish the coherent round head and summary, and reapply ready for the standard review and
      integration pipeline.

Round count: one started of three authorized; no round is claimed complete yet. The other separate
backend proposals are AWS PR #965, GCP PR #966, and macOS Keychain PR #967, with their requirements
acceptance still pending in authenticated Agention threads 5, 7, and 6 respectively.

The isolated implementation delegate handed off commit `26a9199e9`, integrated as `9c176e824`, with
368 focused tests passing and changed-file typing, lint, formatting, and documentation gates
passing. The integrated head still requires the full required gates and all three private lanes.
Guarded workload credentials use vault-derived public/Government/China authority as the source
architecture requires; this corrects the native default chain's omitted authority forwarding for
that credential without changing the configured tenant/client identity.

Private review of `7809c7eff` cleared project and complexity lanes; the generic lane found one
cleanup defect: descriptor close failure could mask cancellation. The delegate corrected primary
exception preservation and ensured a new cleanup interruption also propagates. The two follow-ups
are `b336cc777` and `36e856a00`, with 56 file/input tests and changed-file typing/style checks
passing. All three lanes must assess the integrated correction before re-handoff.

The first full Linux run at `7809c7eff` passed 15,437 tests with 85 skipped. Its required typing,
style, file-quality, drift, isolation, Node website and deterministic-build gates passed. The
website Python run initially had one browser timing failure in unchanged website code; the isolated
case and subsequent complete 160-test rerun passed. The cancellation correction still requires its
final full-suite run; no final gate or round completion is inferred from the earlier head.

Final local validation at `0cac3963c`: **15,442 passed, 85 skipped** on Linux/Python 3.12.16. All
three private lanes cleared that exact correction. Strict mypy passed 1,147 files; Ruff lint and
formatting passed, as did file-quality, Typer isolation, Rulesync drift, locked-SDD checks, 103 Node
website tests and deterministic builds at both site bases. The complete website Python rerun passed
160 tests. The final plan update changes no runtime or test code. Publication, CI on the published
head, the ready handoff, complete PR-level review and live evidence remain separate pending steps.

Published CI at `8b8b4338d` passed all three Linux Python versions and other gates, but Windows
failed three new credential-file tests (517 passed, 21 skipped). The symlink-parent test assumed
POSIX path semantics. Two subprocess tests failed without captured child diagnostics, so their
underlying cause was not established. The PR remains draft within unfinished round 1.

The test-only correction `e1f17280a`, integrated as `59feb6646`, compares guarded file bytes with
the host's native open using distinct possible path targets. Its synthetic Windows subprocess
environment now supplies the documented required `SYSTEMROOT`, and nonzero child exits expose
escaped synthetic diagnostics. No production code changed. The delegate's 56 focused Linux tests and
changed-file typing/style checks passed. Independent follow-up review, local verification and
Windows CI must confirm this correction; the original child failure is not claimed diagnosed.

The lead repeated all 56 file/input tests successfully at the integrated Windows-fixture correction,
with changed-file mypy, Ruff lint/format and plan file-quality checks passing. All three private
lanes cleared the test-only correction at `541f20357`; complexity and correctness also cleared the
subsequent formatting-only head `065143cfc`. Production code remains identical to the fully tested
cleanup correction. The next published Windows run and all required checks still gate ready; no
third full local suite was repeated for these two test-only changes.

Windows rerun at `2a4d7e513` passed the native-path test but retained two child failures (518
passed, 21 skipped). The improved diagnostic identified the actual fixture failure: `Path.home()`
could not resolve a home during imports, before Azure lookup. The follow-up `b22023c15` gives every
probe an explicit fixture directory, creates its own home there, and sets child `HOME` and Windows
`USERPROFILE` to that directory. It inherits no operator home or credential state and does not
derive home from an unsafe credential-path candidate. The delegate repeated the existing 56
behavioral tests and changed-file typing/style checks successfully. This test-only follow-up still
needs independent review and exact-head Windows confirmation; production code is unchanged.

The
[initial-head integration report](https://github.com/WayfarerLabs/agentworks/pull/964#issuecomment-7154638713)
was published at 06:17 UTC, after round 1's fixed batch. It reports successful real Linux Azure Key
Vault runtime cases and a material documentation defect: the complete secret example omits required
`metadata.description`. The lead verified both the current omission and the model's missing-field
error, and posted a critical reading. That finding belongs to round 2 under the existing allowance,
after the next handoff and reporting window. Initial-head live evidence does not validate the
subsequent credential-file changes. The tester records an empty soft-deleted vault metadata residue
with denied purge permission; the lead has not accessed those resources.

## Feedback/fix round 2

Round 1 closed with its
[ready handoff](https://github.com/WayfarerLabs/agentworks/pull/964#issuecomment-7156181904) at
`7e5168c486b2dcdbe70eb750dcd15d2857f43340` on 2026-11-25 07:43:35 UTC. All fourteen published checks
passed, including Windows (520 passed, 21 skipped). All three private lanes cleared that exact head.
Main remains `1c13df6c1136a147b107ddea59d75369ea6115fa`.

The updated-head integration report independently reviewed all 25 changed files and the SDD, with F1
as its only material source correction. It ran real Linux-to-Key-Vault cases on the exact installed
candidate, including seven additional file cases for precedence and finite errors. The report's
gates passed (15,633 tests passed, 274 skipped); these are its measurements rather than the
implementation session's local counts. Genuine certificate/workload identity success, native
provider behavior and the other stated gaps are not established. This separate full-delta review
satisfies receiving PR-level review; it is not formal approval or final corrected-head clearance.
The lead critically read that report, reconfirmed F1 and distinguished the repeated quota failure
from all fourteen green core/CodeQL checks.

The operator's existing allowance authorizes round 2. Its batch was fixed at 2026-11-25 08:44:08
UTC, after the required reporting window, against `7e5168c48`: repeated F1 and the repeated Copilot
quota report, with no unresolved inline thread or additional finding. The PR returned to draft
before changing its head. The permanent Azure secret example now includes its required description.
No runtime or test code changed. The original literal example failed actual isolated
`agw secret list --output json` with the required-description error; corrected-example validation
and private review are recorded as they complete. No prose-pinning unit test is added.

- [x] Reproduce F1 through the literal declaration and actual isolated CLI; add the required field.
- [x] Confirm the corrected literal declaration loads and run applicable file-quality checks.
- [x] Complete independent project and complexity review of the documentation-only correction.
- [ ] Publish the coherent round-2 head and summary with green CI, then reapply ready.

Round count: two started of three authorized; round 2 is not yet claimed complete. Later feedback
belongs to the next batch. The corrected head's pipeline response, merge-ready handoff and operator
working confirmation remain separate pending steps.

The corrected literal README declaration now loads through actual `agw secret list --output json`
with exit 0, returning the npm-token row and its work-kv mapping. Both before/after receipts are
retained in the session-owned ignored namespace; the CLI used a temporary isolated home, synthetic
configuration and no provider operation. Prettier, Markdown lint, spelling and diff checks pass for
both changed documents. No full local suite is repeated for this documentation-only correction; all
required published CI checks still gate its ready handoff.

Independent project and complexity lanes both cleared `741f29945` without findings. The project
reviewer read the retained exact-head integration report and substantiated receipt of its separate
full-delta review. The complexity lane confirmed one required field is the complete correction. This
final plan update records their completed work only; all source and tests remain unchanged.
Publication and CI on the new head, the ready handoff and its pipeline response remain pending.
