# Files config: plan

Status: requirements in progress. The [FRD](./frd.md) is a draft awaiting the operator.

## Design

- [ ] Have the operator accept the FRD.
- [ ] Write the HLA: the declaration schema; application early in each scope's create, reinit and
      repair; the applied set and pending-change reporting; how changes are applied (existing verbs
      or a dedicated apply); the lock entities and re-checks each application uses under the native
      lock hierarchy; inspection; how `env` would map onto the same model; and the ADR 0013
      amendment for secret files at rest.
- [ ] Write the LLD for path rules and application records, citing the code at HEAD.
- [ ] Run the private review loop on the design (project review and complexity pass), address its
      findings, then mark the design PR ready and address published feedback.
- [ ] Merge the design PR.

Done when the FRD is accepted, the HLA and LLD agree with each other and with the code at HEAD, no
review finding is left undisposed, and the design PR is merged.

## Implementation

Sequenced in the HLA. Implementation starts after the 0.20.0 release, which carries the native lock
hierarchy files builds on.

## Closeout

- [ ] Write `locked.md`.
