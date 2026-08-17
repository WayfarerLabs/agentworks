---
description: Diagnose Agentworks failures from framed errors and narrow explicit checks.
index-order: 40
---

# Troubleshooting

Start with the framed error and current registry verdict. Use explicit, non-mutating checks to
distinguish configuration, readiness, and connectivity failures. Record the named resource, typed
error, and a redacted reproduction before changing state.

When workstation examination is inside the operator's instruction, run `agw doctor --output json`
and use its checks to select a narrower verification surface. Doctor output is evidence, not
authorization to install tools, edit configuration, start a VM, or apply another repair.

Before a repair outside the current instruction, state its exact target, expected change, and how
success will be checked. Ask before performing it. If declined, preserve the observed state and
provide the read-only evidence and applicable command help.
