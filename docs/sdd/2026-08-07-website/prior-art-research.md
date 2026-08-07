# Prior Art Research: The agentworks.build Website

- Date: 2026-08-07
- Scope: first-slice hosting, deployment, content sharing, accessibility, and browser behavior

## Executive summary

The smallest architecture that satisfies the FRD is a one-page, repository-native static site
published to GitHub Pages by GitHub Actions. The repository is public, the site is an open-source
project front door rather than a transaction surface, and the required deployment automation already
lives beside GitHub Actions. GitHub Pages can publish a custom Actions artifact, serve an apex and
`www` custom domain, provision HTTPS, and redirect between the two domain forms. This avoids a cloud
account, infrastructure state, a new package ecosystem, or moving DNS away from GoDaddy.

The site should use semantic HTML, local CSS, system fonts, and only a small progressive-enhancement
script for the copy button. The bootstrap remains selectable without JavaScript. The build consumes
the onboarding effort's canonical bootstrap source after that source lands on `main`, and tooling
proves that the built page, canonical source, and README fenced block are byte-identical. Other
product claims are generated from uniquely selected permanent-doc paragraphs; site-owned labels and
instructions make no product claims.

## Findings

### F1. GitHub Pages is the natural first-slice host

GitHub Pages supports publishing from a custom GitHub Actions workflow. GitHub's documented flow is
checkout, optional build, upload a Pages artifact, and deploy it through the `github-pages`
environment. This matches R3 without a generated branch or committed build output.

The current repository is public under the WayfarerLabs organization and already uses GitHub
Actions. GitHub Pages is available for public organization repositories. The published-site limits
(1 GB site size, 100 GB monthly soft bandwidth, and a 10-minute deployment timeout) are far beyond
this first slice. GitHub's usage policy excludes sites primarily used for commerce or commercial
SaaS; the first slice is an open-source project page linking to GitHub and PyPI, with no
transactions or service backend, so it fits the documented purpose. A future commercial transaction
surface would trigger a hosting reassessment.

Design tie-in: use a custom Actions workflow and uncommitted build artifact. Keep hosting behind a
replaceable static-artifact boundary so moving later does not reshape the site.

### F2. GoDaddy DNS can point the apex directly at GitHub Pages

GitHub documents four stable `A` records for an apex domain and a `CNAME` from `www` to the
organization's `github.io` hostname. When both forms are configured, Pages redirects one to the
configured canonical domain. GitHub recommends verifying the domain at the organization level with a
persistent TXT record before connecting it, which prevents another GitHub account from claiming the
domain if the Pages site is ever detached. It also recommends avoiding wildcard DNS records.

GoDaddy supports ordinary apex `A` and subdomain `CNAME` records when it hosts the nameservers. No
nameserver migration is needed. DNS changes can take up to 24 hours to propagate, and HTTPS may take
additional time after the records resolve.

Dated snapshot (2026-08-07): `ns11.domaincontrol.com` and `ns12.domaincontrol.com` are
authoritative; the apex resolves to GoDaddy parking addresses `76.223.105.230` and `13.248.243.5`;
`www` is a CNAME to the apex; the queried AAAA, MX, apex TXT, CAA, and GitHub-verification TXT names
return no answers; and GitHub's Pages API reports no configured Pages site for this repository.
Go-live replaces only the parking path after rechecking this inventory.

Design tie-in: make `https://agentworks.build` canonical, configure `www.agentworks.build` as the
redirecting variant, verify the domain before changing traffic, retain the verification TXT record,
record and recheck every DNS record type before mutation, ensure any restrictive CAA policy permits
GitHub's certificate issuer, and verify the approved DNS delta, redirect, certificate, and HTTPS
enforcement explicitly at go-live.

### F3. The alternative hosts add machinery without first-slice value

- Cloudflare Pages provides static deployment and custom domains, but an apex Pages domain must be a
  Cloudflare zone using Cloudflare nameservers. That moves authoritative DNS away from the stated
  GoDaddy setup for no requirement-level benefit.
- Azure Static Web Apps provides managed certificates and external custom domains, but Microsoft's
  documentation calls out apex limitations at registrars such as GoDaddy and recommends Azure DNS,
  forwarding to `www`, or a less-preferred regional `A` record path. It introduces an Azure resource
  and identity boundary that the first slice does not otherwise need.
- An AWS secure static site composes at least object storage, CloudFront, and a certificate. S3
  website endpoints do not support HTTPS themselves. AWS is sound when its control or integration
  benefits are needed, but those components are avoidable here.

Design tie-in: do not stand up infrastructure for the first slice. Revisit hosting if Pages policy,
limits, response-header control, preview requirements, or a future dynamic surface become real
constraints.

### F4. A plain page is more robust for both humans and agents

WCAG 2.2 recommends testable, technology-neutral accessibility criteria. Relevant first-slice
properties include semantic structure, keyboard operation, visible focus, text and component
contrast, named controls, and reflow without two-dimensional page scrolling at narrow widths. Plain
HTML provides those properties with fewer failure modes than a client-rendered application.

The clipboard `writeText()` API is widely available but requires HTTPS and a user activation. The
bootstrap must therefore remain real text inside a code element. The copy button is a convenience,
not the only access path, and reports success or failure through an accessible status message.

Design tie-in: target WCAG 2.2 AA, retain meaningful source order and landmarks, make all controls
keyboard operable, honor reduced-motion preferences, and keep the complete bootstrap readable and
selectable with CSS or JavaScript unavailable.

### F5. Shared sources are stronger than synchronized copies

The onboarding-and-discovery HLA and plan define one canonical bootstrap body that generates the
README and harness wrappers. The website is another rendering target of that same content, not an
independent owner. The source has not landed on `main` yet, so its eventual file path and generator
API are not coordinated facts.

Design tie-in: the website implementation waits for onboarding Phase 3 to land. Its LLD binds to the
merged canonical source. The website build reads that source directly, while a separate contract
test extracts the README block and decoded HTML code text and compares both byte for byte. The site
does not parse rendered README HTML or copy prose from the onboarding branch.

Problem and principle passages are always generated from uniquely selected paragraphs in permanent
repository docs through a closed normalization transform. Site-owned copy is limited to labels and
instructions that make no product claim. The page links to the permanent, fuller explanation in
`docs/why-agentworks.md` on GitHub.

## Refuted or rejected claims

- **"GitHub Pages needs Jekyll."** Rejected. GitHub documents custom Actions workflows that upload
  an arbitrary static artifact.
- **"The apex domain requires moving DNS."** Rejected for GitHub Pages. Ordinary GoDaddy `A` records
  cover the apex and a `CNAME` covers `www`.
- **"A copy button makes the bootstrap machine-copyable."** Rejected. The real contract is text in
  semantic markup; the button is only progressive enhancement.
- **"A framework is needed to leave room for future guide/reference pages."** Rejected for this
  slice. The future content contracts are not stable, and a directory of static inputs plus a build
  boundary can adopt a generator later without shipping speculative machinery now.
- **"Analytics are necessary at launch."** Rejected by the FRD default and the onboarding effort's
  no-telemetry first-release posture. No cookie, beacon, log-processing pipeline, or tracking script
  is added.

## Open questions not resolved by prior art

- The exact canonical bootstrap source path and generation interface remain owned by onboarding
  Phase 3 and must be read from `main` when that phase lands.
- GitHub supplies serving logs only through its own platform operations. The first slice has no
  product analytics, so there is no operator-facing traffic report to design.

## Sources

| Source                                                                                                                                                                     | What it establishes                                           | Quality and angle                          |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------ |
| [GitHub Pages publishing sources](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)           | Custom Actions artifact and deployment flow                   | Primary platform documentation             |
| [GitHub Pages custom domains](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site) | Apex and `www` records, redirects, HTTPS, propagation         | Primary platform documentation             |
| [GitHub Pages domain verification](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/verifying-your-custom-domain-for-github-pages)  | Organization TXT verification and takeover protection         | Primary security documentation             |
| [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)                                                              | Capacity and usage-policy boundary                            | Primary platform policy                    |
| [GoDaddy A-record guidance](https://www.godaddy.com/help/add-or-edit-an-a-record-42546)                                                                                    | GoDaddy-hosted DNS supports apex A records                    | Primary DNS-provider documentation         |
| [Cloudflare Pages custom domains](https://developers.cloudflare.com/pages/configuration/custom-domains/)                                                                   | Apex domains require a Cloudflare zone and nameservers        | Primary alternative-platform documentation |
| [Azure Static Web Apps custom domains](https://learn.microsoft.com/en-us/azure/static-web-apps/custom-domain)                                                              | Managed certificates and GoDaddy apex caveat                  | Primary alternative-platform documentation |
| [Amazon S3 website endpoints](https://docs.aws.amazon.com/AmazonS3/latest/userguide/WebsiteEndpoints.html)                                                                 | S3 website endpoint lacks HTTPS; CloudFront or Amplify needed | Primary alternative-platform documentation |
| [WCAG 2.2](https://www.w3.org/TR/WCAG22/)                                                                                                                                  | Current accessibility recommendation and AA criteria          | Normative web standard                     |
| [MDN Clipboard `writeText`](https://developer.mozilla.org/en-US/docs/Web/API/Clipboard/writeText)                                                                          | HTTPS and activation requirements, browser maturity           | High-quality implementation reference      |
