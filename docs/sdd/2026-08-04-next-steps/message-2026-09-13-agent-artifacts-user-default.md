# Agent artifacts: revised inactive VM routing

Authenticated operator direction supersedes the earlier successor note's inactive-VM-to-session
fallback. In the agent-artifacts SDD and PR #794, an inactive VM facet routes its captured inputs to
**user**. An inactive user or workspace facet passes its applicable inputs to session. An activated
VM facet still chooses user, workspace or session; an activated but unimplemented facet is an error.

This lets user activation alone handle VM inputs at that actual user's native locations. VM results
remain reusable for every user, without inspecting downstream activation or broadcasting into both
sides of the user/workspace diamond. Each deferral has one destination. Admin and agent remain
separate users; configuring the administrator does not configure an agent. Existing activated user
results that have not handled the newly inherited inputs become stale and require their own setup.

The current effort updates its FRD, HLA, implementation, tests and operator guides together. The
predecessor's historical artifacts remain unchanged; this message records the successor ruling.

-- agw-ns-harness-facets
