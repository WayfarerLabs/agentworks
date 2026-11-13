# Plan

Delivery vehicle: one feature branch, `feat/native-npm`, containing one coherent feature. The
session lead owns these artifacts, architecture, and integration.

- [x] Add validated npm fields to admin and agent declarations and resolved state; verify
      inheritance, instance overlays, schema emission, and disabled behavior.
- [x] Implement a shared native npm runner with per-user prefix, minimum age, version checks, strict
      package engines, and actionable failure handling.
- [x] Make mise optional at VM scope and arrange both user flows so mise and npm precede custom user
      install commands with their executables on PATH.
- [x] Update permanent guides, generated field teaching, and affected sample configuration to
      describe actual behavior and runtime provisioning.
- [x] Verify npm against a local registry with old/new direct and transitive releases, explicit
      pins, reinitialization, and executable invocation.
- [ ] Run appropriate repository checks and independent project, complexity, and generic correctness
      reviews; resolve material findings.
- [ ] Record the final validation evidence and any unavailable VM coverage before closeout. Do not
      claim unperformed checks or create a lockfile early.

## Validation in progress

The real local registry suite passed with npm 11.16.0 (six passes, one exclusion case unavailable in
that release) and npm 12.2.2 (seven passes). It verifies direct and transitive registry age
filtering, exact-pin refusal, zero-age installs, stricter age on reinit, and inherited exclusion
overrides. Native executable invocation runs with a provisioned Node binary and no mise
configuration in the isolated home. The sixth case installs a package that replaces npm with an
unsupported version and verifies that the remaining batch stops before its age policy can be
ignored. The seventh case refuses an incompatible Node engine even when user npm config enables
force and disables strict engine checking.

Eight initialization tests cover all mise/npm toggle combinations for both user roles, executing
PATH lookups inside the actual generated profiles before custom commands and after the final profile
write. Declaration tests cover inheritance, instance overlays, strict input types, and emitted
schema fields. Twelve real Bash login tests additionally cover `.profile`, `.bash_profile`, and
`.bash_login`, retaining dotfiles, handling files without a terminal newline, and avoiding duplicate
package PATH entries.

The first independent review round identified a mid-batch npm replacement bypass, Bash startup
precedence, and overly broad dependency-age wording. Corrections recheck npm before every install,
attach the managed profile to existing Bash startup files, and qualify age coverage as
registry-resolved releases. The complexity pass removed the duplicate agent mise PATH writer by
publishing package paths once before both installers. The second review caught missing newline
separation when appending to existing shell files; the corrected append is covered by actual
login-shell tests. An unused runner return value was also removed. Final correction review is
pending.

ruff, mypy, and the repository's file-quality lint pass. The complete non-integration suite and
independent reviews remain in progress. No VM backend has been used for this effort, so the local
registry evidence does not claim remote SSH or VM create/reinit coverage.
