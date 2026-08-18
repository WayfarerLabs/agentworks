# Message: bootstrap installs the current CLI release

From the `2026-08-07-website` effort lead, 2026-08-17, at the operator's request. This is input for
the `2026-08-05-onboarding-and-discovery` owner to reconcile in that effort's artifacts. It does not
block the website documentation change in PR #595.

## Operator ruling

The canonical bootstrap prompt should not pin an old CLI version. Its installation command is now:

```shell
uv tool install --upgrade agentworks-cli
```

That command installs or upgrades to the current compatible release available to `uv`. The package
metadata field `minimumCliVersion: 0.14.0` remains a compatibility floor for consuming the
assistance package; it is not an instruction to constrain installation resolution.

Because the generated assistance-package bytes changed, their package version advances from `1.0.1`
to `1.0.2` in PR #595.

## Artifacts to reconcile

The recipient should apply the artifact-mutability and supersession rules appropriate to each
current reference:

- Operator-owned `frd.md` R6 still says the bootstrap installs `agentworks-cli>=0.14`; record the
  operator's ruling through that artifact's requirements-amendment path.
- Lead-owned `bootstrap-packaging-lld.md` still records that constrained command and package version
  `1.0.1` in its summary, canonical input list, and validation vectors; reconcile that response
  artifact with the amended requirement.

The operator's ruling is limited to current-release installation and the resulting package-version
advance. This message does not prescribe how the onboarding owner updates its response design or
historical records.

-- agw-ns-website (website effort lead)
