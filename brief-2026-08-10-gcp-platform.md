# Task brief: GCP VM platform (2026-08-10)

From the saga lead, via the branch-seeded brief mechanism. You own this branch from pickup; the saga
lead reviews; the operator merges. Delete this file before the PR goes ready.

Standalone effort, not saga work and not a 0.14 gate (additive, breaking nothing). SDD process per
its size: this is a new platform plugin, so expect FRD/HLA/plan with phased review, with the azure
and aws plugins as the structural templates.

## Charter

Operator ruling (2026-08-10): add a GCP vm-platform plugin. The operator provides the live GCP
inventory and credentials for acceptance.

The shape is deliberately unoriginal: mirror the azure/aws plugins at HEAD — capability-descriptor
registration, one config model, the vm-platform mode contract (an `auth` union: ambient Application
Default Credentials or an explicit credential arm, defaulting to the arm omission historically
selects for new kinds), create/start/stop/delete/status lifecycle, preflight readiness, and
completions/sample-config/docs riding the same PRs.

## Constraints

- **Born conforming to the provider-boundary contract** (wave 3, merged 2026-08-10; read
  `capabilities/vm_platform/README.md` and the secret-sources SDD's five-surface enumeration before
  designing). Concretely for GCP: instance metadata (`user-data`, startup scripts, any
  `instances.insert` metadata item) is provider-retained and retrievable, so the retained bootstrap
  payload is credential-free; the Tailscale key travels post-boot over the provisioning transport
  via the shared stdin join (`EphemeralTailscaleBootstrap`); and a provider-shaped retention test
  inspects the final constructed `instances.insert` request body for a quoting-hostile sentinel,
  exactly as `tests/plugins/test_cloud_bootstrap_secret_boundaries.py` does for azure/aws. GCP
  becomes the sixth row of the durable-surface enumeration; update that enumeration where it lives.
- **Secrets through secret sources**: any explicit credential arm consumes the wave 3 model (no raw
  key material in config); exception objects, argv, and logs stay key-free per the narrowed contract
  (in-memory retention is best-effort by ruling — do not build scrub machinery).
- **Scope discipline**: read `target-state.md`'s "Requirements are priced like code." Mirror the
  siblings; introduce no mechanism they don't have. If GCP genuinely needs a seam the siblings lack,
  raise it to the saga lead before building it.
- **Live acceptance** is operator-gated (their GCP project); design the tester's charter around
  bounded fakes first, one live run at the end, zero-residue verified.
- Saga vocabulary; message-signatures and `Agentworks-Session` trailer rules; the always-consider
  rules (docs, sample config, completions, SDD artifacts) apply as ever.

## Definition of done

`vm-platform/gcp` registers like its siblings and passes the same conformance surface; a GCP VM
creates, initializes, joins the tailnet via stdin, and deletes cleanly in a live run; the retained
request payload is provably credential-free; docs, samples, completions, and the durable-surface
enumeration are in lockstep; gates green; SDD locked truthfully.

-- agw-next-steps (saga lead session)
