---
description: Set up Agentworks or assess an existing installation using current CLI facts.
---

# Agentworks onboarding

Agentworks separates declared resources, capability implementations, and live instances. This is a
repeatable setup path, not a one-time wizard. See `concept-core-model` for the domain model and
`concept-source-review` if the operator wants to inspect the exact canonical release source.

Start by reading current facts. `agw resource kinds --output json` shows the installed vocabulary,
`agw resource list --include-disabled --output json` shows available choices, and the operational
list commands show existing VMs, workspaces, agents, sessions, and consoles. Run
`agw doctor --output json` when examining the workstation is within the operator's instruction. A
disabled, not-ready, or failing item is information, not permission to repair it.

For a clean setup, use this sequence:

1. Run `agw config init` when settings are absent. It owns sample creation and refuses to overwrite
   an existing config. Preserve existing settings when they are present.
2. Ask which VM platform and placement the operator wants. Use `agw resource explain` and
   `agw resource sample` for current configuration shapes, then declare only the required VM site,
   templates, secret references, and optional plugins.
3. Select an existing SSH identity by file presence without reading private-key content. If the
   operator wants a new key, state that `ssh-keygen -t ed25519 -f SSH_KEY_PATH` creates two files at
   that explicit path and verify that neither path exists before asking to run it. If declined,
   leave the workstation unchanged and continue only with a usable existing identity.
4. Run `agw doctor --output json` and resolve failures through the narrowest current command or
   `concept-troubleshooting`. Do not infer providers, enable plugins, request secret values, or
   perform repairs merely because a check failed.
5. Use the current `agw vm create --help` and `agw session create --help` surfaces to create the
   operator-selected first VM and session. State the selected site, template, workspace, agent, and
   infrastructure effect before any mutation not already covered by the request. Verify through the
   matching JSON describe commands. If creation is declined or verification fails, do not retry
   automatically; report the observed state and leave the last verified resources intact.

For an existing installation, compare those same inventories and doctor results with the operator's
goal. Preserve work that is already ready and offer only the smallest missing configuration or
verification step.

<!-- agw:agent-only -->

Ask for choices as they become relevant rather than presenting a questionnaire. Use current CLI
facts to narrow each choice, and distinguish the Agentworks assistant agent from any
Agentworks-managed agent resource being created.

<!-- /agw:agent-only -->
