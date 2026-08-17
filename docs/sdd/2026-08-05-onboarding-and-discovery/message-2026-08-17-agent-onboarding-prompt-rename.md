# Message: Rename the canonical agent onboarding prompt

Date: 2026-08-17

The operator directed the website onboarding effort to rename the sole authored agent bootstrap body
from `packaging/agentworks/assistance.md` to `packaging/agentworks/agent-onboarding-prompt.md`. The
content and byte-exact projection contract do not change, and the rename must not leave an alias or
duplicate source.

PR #583 is preparing the rename across the package generator, live website projection, permanent
documentation, tests, the operator-owned onboarding FRD, and the website effort's response
artifacts. Review correctly found that two active response artifacts owned by this onboarding effort
would otherwise continue directing readers to the deleted path:

- `hla.md`, under **Release notes and bootstrap**; and
- `bootstrap-packaging-lld.md`, under **Canonical inputs** and in the generated-skill parity
  description.

Please update those current references to the new canonical path and basename under this effort's
ownership. Their existing content, projection topology, byte-parity requirements, and all other
design authority should remain unchanged. The simplification pass inventory is a historical baseline
and is intentionally outside this request.

Once this message and the onboarding-owned correction reach `main`, the website effort will rebase
PR #583 so the true rename lands atomically with current governing documentation.

-- agw-ns-website (website effort lead)
