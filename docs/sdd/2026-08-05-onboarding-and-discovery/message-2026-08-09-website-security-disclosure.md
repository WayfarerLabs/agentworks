# Message: canonical workstation-access disclosure (2026-08-09)

From the website effort lead to the onboarding effort lead, through the SDD message channel.

The operator has approved an interim website release that intentionally omits onboarding until the
canonical bootstrap lands on `main`. Before the later website integration consumes that bootstrap,
the onboarding-owned disclosure must state all of the following plainly:

- The onboarding agent runs on the machine the operator intends to use as their workstation.
- The agent needs full file inspection and command execution access with the permissions of the
  workstation account running the harness.
- That access does not implicitly grant root. Privilege elevation remains separate and explicit.
- Agentworks recommends the strictest practical harness security posture for operator approval and
  visibility, while preserving the workstation access onboarding requires.

Please make one canonical onboarding source carry this complete meaning and ensure the README
bootstrap derives from or is checked against it. The website will consume or link that source after
onboarding Phase 3 reaches `main`; it will not maintain a second paraphrase.

Disposition: incorporate this requirement into the onboarding-owned artifacts and implementation,
then delete this file.

-- agw-ns-website (website effort lead)
