# Files config: plan

Status: requirements in progress. The [FRD](./frd.md) is a draft awaiting the operator.

## Design

- [ ] Have the operator accept the FRD.
- [ ] Write the HLA: the declaration schema, application at each scope's setup and repair, the
      recorded contributions, inspection, and the ADR 0013 amendment for secret files at rest.
- [ ] Write the LLD for path rules and application records, citing the code at HEAD.
- [ ] Run the private review loop on the design (project review and complexity pass), address its
      findings, then mark the design PR ready and address published feedback.
- [ ] Merge the design PR.

Done when the FRD is accepted, the HLA and LLD agree with each other and with the code at HEAD, no
review finding is left undisposed, and the design PR is merged.

## Implementation

Sequenced in the HLA. Implementation starts after the 0.20.0 release.

## Closeout

- [ ] Write `locked.md`.
