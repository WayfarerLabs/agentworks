---
description: "Follow the standard agentic development process on every effort"
globs: ["**/*"]
---

# Development Process

Every development effort, large or small, follows the same shape. Hold it in mind from the first
step, not only when you remember to.

- **Size up the work first.** Large or significant efforts (new subsystems, contract or schema
  changes, wide or hard-to-reverse work) go through the `sdd` skill and are implemented by
  delegating each step to `agentworks-dev` subagents, so the lead keeps a concise context and holds
  the overall picture. Small, well-patterned changes skip SDD and are done directly.
- **Review every step** with the `agentworks-reviewer` subagent, using a model at least as capable
  as the dev that produced the work. Push back on incorrect findings; otherwise fix anything valid,
  and iterate until everyone is happy.
- **Commit and push at regular intervals**, and open a non-draft PR when close to merge-ready,
  triaging Copilot's automated review of new pushes.
- **Escalate the significant** (necessary redesigns, wrong requirements, decisions that are the
  operator's) and otherwise keep pushing while the road is clear.

The `agentic-dev-process` skill is the full playbook, including deliberate model-tier selection and
the commit/PR details; load it when running a real effort.
