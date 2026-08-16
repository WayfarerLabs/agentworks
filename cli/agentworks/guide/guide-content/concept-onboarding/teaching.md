Progressive onboarding starts with discovery. Read `agw resource kinds --output json` and
`agw resource list --include-disabled --output json`, inspect existing operational state, and prefer
explicit verification commands when prediction is insufficient. A disabled or not-ready item is
information, not permission to repair it.

For a clean setup, follow one visible, repeatable sequence:

1. When settings are absent, check only for the presence of candidate public-key files and matching
   private-key paths. The operator selects the identity. Never read private-key content. If no
   usable pair exists, offer `ssh-keygen -t ed25519 -f SSH_KEY_PATH` at one explicit
   operator-selected path, and run it only after confirming that neither the private path nor its
   `.pub` path exists. Disclose that key generation mutates the workstation, and never overwrite a
   key.
2. Run the existing `agw config init`. It owns sample creation and refuses to overwrite an existing
   config. When settings already exist, skip initialization and preserve them.
3. Through the existing generated config and ordinary file-edit surface, update only the settings
   fields needed for the operator-selected SSH identity and provider or plugin path. Collect
   provider identifiers, plugin choices, and secret references explicitly. Never infer a provider,
   enable a plugin automatically, or request a secret value. Built-in local templates and sites
   remain valid choices. No hidden wizard or second config writer owns these edits.
4. Run `agw doctor --output json` and require JSON contract v1 with no failing or unavailable
   applicable readiness check before resource creation. Record the caller-owned proof by rerunning
   this topic with `--evidence run-doctor:onboarding/doctor-readiness=verified`; failed or refused
   proof leaves the resource actions unavailable and is never a reason to retry doctor
   automatically.
5. After that proof, use the inert `create-first-vm` and `create-first-session` records below only
   when their preconditions hold. Every name, template, site, workspace, and Agentworks-managed
   agent is an operator-selected input. Verify the VM and started session through their JSON v1
   describe commands. A failed create verification reports the observed state, points to
   `concept-troubleshooting`, and never retries the mutation automatically.

The Agentworks assistant agent may complete this configuration-through-session sequence under one
explicit setup envelope when its targets and impacts were disclosed. Missing input questions do not
create a new authorization boundary. A refusal leaves the applicable action inert and records the
manual alternative. A rerun uses the same live facts, skips present VMs and sessions and other ready
work, and reports only disabled, not-ready, or unverifiable work that remains.
