# Message: secret preview post-merge follow-ups

- Date: 2026-08-24
- From: the secret-preview effort lead
- To: the next-steps saga lead

Secret-preview contract PR #619 merged and its child SDD is locked. The parent saga's
`child-sdds.md` does not yet record that child effort, so the saga lead should add the ledger entry
and final disposition under the parent's ownership. This message does not reopen or amend the locked
child artifacts.

The operator accepted the following release evidence. Env-var and prompt paths, plus a same-sentinel
positive control, ran through the real shipped CLI on a remote Lima VM. The effort did not run a
fake-`op` live CLI, a real 1Password or desktop-auth path, or provider error-token live cases.
Hard-failure batch doom and the doctor `FAIL` path remained unit-level evidence. The operator
accepted those substitutions and reported that testing revealed no issues.

A bounded post-merge PR owns the product follow-ups found after the final review window:

- scope every interaction broker view to the exact names in one backend call and preserve core
  invariant failures at that boundary;
- preview the declared doctor secret union in one batch;
- add compact batch, broker, ambient-env, and 1Password provider-text regressions; and
- repair stale operator, backend-author, and release-transition documentation.

The following observations route to existing or parent-level ownership rather than expanding that
product follow-up:

- issue #603 already owns moving SSH key-file existence checks from config-load time to use time;
- the quiet successful `Resolving Secrets` header predates the secret-preview contract and is an
  optional CLI UX question, not a contract defect;
- VM template binding at create time is a test-method note, not a product defect;
- an interrupted Lima create left a database row without `instance_name`, after which
  `agw vm delete --force` could not remove it. Issue #184 covers related partial Lima instances and
  manual cleanup, but not this database-row and force-delete variant, so the saga should route that
  additional case to the issue owner or a dedicated follow-up; and
- the child plan's final ready and handoff checkboxes remained unchecked by construction and are now
  immutable. Server-side PR history records completion; the locked child should not be edited.

-- agw-ns-secrets (secret-preview effort lead)
