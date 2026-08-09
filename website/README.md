# Agentworks Website

<!-- cspell:ignore keypaths sdds -->

This directory owns the static source for `agentworks.build`. A standard-library Python builder
combines semantic templates, local CSS and JavaScript, the AGW rocket asset, and selected passages
from permanent repository documentation. Generated output stays outside the repository and can be
published by any static host.

The current release is intentionally useful without guided onboarding. It offers the product
overview, operating principles, permanent project links, security deep dive, and custom 404. The
home page states that guided onboarding is not yet published and contains no substitute command or
copy control. A later change will replace that notice with the canonical onboarding source after
that source is available on `main`.

## Local build and test

Python and Node.js are the only runtimes used. The build itself uses only the Python standard
library and has no package installation step.

Build the complete site at the custom-domain root and serve it locally:

```bash
python3 website/build.py \
  --repo-root . \
  --output /tmp/agentworks-site \
  --site-base /
python3 -m http.server --directory /tmp/agentworks-site 8000
```

Then open `http://localhost:8000/`, `http://localhost:8000/security/`, and
`http://localhost:8000/404.html`. A project-Pages build uses the same source and command with
`--site-base /agentworks/`.

The focused 404 seam remains available for game work:

```bash
python3 website/build.py \
  --only 404 \
  --repo-root . \
  --output /tmp/agentworks-404 \
  --site-base /
```

Run the automated suites and repository checks:

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py'
node --test website/tests/lander-model.test.mjs
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
```

Pull requests and pushes to `main` run these website contracts in the `Website` job of
`.github/workflows/ci.yml`; `ci-success` requires that job. The `Deploy website to Pages` workflow
in `.github/workflows/pages.yml` runs only for pushes to `main`. Its read-only build job repeats the
tests, normalizes the Pages-reported base path to `/` or the builder's slash-bounded project form,
proves a second full build is byte-identical, and uploads only the generated
`$RUNNER_TEMP/agentworks-site` directory. A separate `github-pages` deployment job alone receives
Pages write and OIDC permissions. The build job verifies exact event-commit checkout and a clean
tracked/untracked worktree after the tests and again immediately before artifact upload; deployment
is conditional on that verified source SHA still matching the event commit.

The package-free browser, responsive, motion, touch, and assistive-technology checks are in
[`tests/lander-browser-checklist.md`](tests/lander-browser-checklist.md). Complete its pending rows
before a public release.

## Artifact contract

The complete output contains exactly:

```text
404.html
index.html
assets/agw-rocket.svg
security/index.html
static/lander-game.js
static/lander-model.js
static/lander.css
static/site.css
```

The focused output omits `index.html` and `security/index.html`. The manifest is explicit in
`build.py`; the builder never recursively copies source directories.

All three CLI paths are required. The output must be outside the repository. The site base is an
ASCII, slash-bounded same-origin path such as `/` or `/agentworks/`; absolute URLs, dot segments,
encoding, and unbounded paths are rejected. The builder validates all content and templates, stages
an exact regular-file tree beside the destination, and then swaps it into place. Existing output is
replaced only when every entry belongs to the selected manifest.

Replacement uses a sibling backup. A failed installation or installed-manifest check restores the
previous output. Once the installed manifest is verified, the new artifact is committed. A failure
to remove the old backup after that point leaves the valid new artifact installed and prints a
`warning:` naming the retained backup. Remove that specifically named sibling only after confirming
the installed output remains correct.

The same inputs and arguments produce byte-identical files. Build output includes no timestamp,
commit identifier, environment-derived prose, or inherited file. Because output beneath the
repository is rejected, a successful build creates no Git status residue.

## Content ownership

The builder reads three permanent repository inputs:

- `README.md` owns the short product identity and design principles.
- `docs/why-agentworks.md` owns the problem statement, threat model, isolation boundaries,
  limitations, operator posture, and credential/secret explanation.
- `SECURITY.md` owns private vulnerability reporting and the reporting URL.

Passages are selected by complete heading keypaths and exact normalized blocks, then escaped and
rendered through a closed Markdown subset. Missing or duplicate headings, content drift, unsupported
markup, invalid links, and reporting-link drift fail the build before output changes. This is
intentional. Update the permanent source and its website contract together when a selected claim
changes; do not paste a second version into a template.

Templates own only headings, navigation, destination labels, presentation-neutral connective text,
and the bounded interim availability notice. `website/` does not own product behavior, security
claims, vulnerability contact details, or installation instructions. The selected SVG and lander
implementation are permanent assets and must not be regenerated from design-history files.

## Release stages

The interim release and completed onboarding release use the same URLs, templates, visual system,
builder, and publishing path. There is no runtime release mode.

The interim artifact must contain the ordinary-text availability notice exactly once and must not
contain a bootstrap region, installation command, copy control, copy script, dormant onboarding
token, disabled control, or empty placeholder. Home and security have no JavaScript. The custom 404
alone loads its same-origin game module, and its error content and home link work without scripts.

Once the canonical onboarding contract lands on `main`, a separately reviewed integration will
delete the notice, add the canonical content as a required input, and prove byte identity with the
README. Do not anticipate that interface or parse a branch-only wrapper.

## GitHub Pages setup and deployment

These one-time repository and organization settings require an operator with the relevant GitHub
permissions. Record non-secret evidence when each action is completed.

1. Set GitHub Pages to use GitHub Actions as its publishing source.
2. Protect the `github-pages` environment so only the default branch can deploy.
3. Merge the complete publishing workflow and verify the expected commit at the default Pages URL
   before attaching the custom domain.
4. Verify `agentworks.build` for the WayfarerLabs organization and retain the generated GitHub TXT
   record.
5. Set `agentworks.build` as this repository's custom domain.
6. Re-inventory DNS, obtain explicit operator approval for the exact cutover, and then change only
   the identified parking records.
7. Verify apex content, the `www` redirect, certificate hostname, and HTTPS enforcement.

The publishing workflow must build and test from a clean checkout on every push to `main`, without
path filters. Authoritative inputs live outside `website/`, so a website-only trigger could publish
stale content. The build job uploads exactly the generated directory. The deploy job uses the
`github-pages` environment with only Pages write and OIDC token permissions. Pull requests build and
test but do not deploy or receive those permissions.

Attaching the custom domain changes only the builder's site-base input from `/agentworks/` to `/`.
It does not select different content or require a second artifact path. The artifact contains no
`CNAME`; the repository setting is the custom-domain authority.

## DNS cutover and verification

The last recorded public inventory, dated 2026-08-07, found GoDaddy nameservers
`ns11.domaincontrol.com` and `ns12.domaincontrol.com`, apex parking addresses `76.223.105.230` and
`13.248.243.5`, and `www` as a CNAME to the apex. Queries returned no AAAA, MX, apex TXT, CAA, or
GitHub-verification TXT answers. This is historical context, not authorization to mutate DNS.

Immediately before cutover, inventory `A`, `AAAA`, `CNAME`, `MX`, `TXT`, and `CAA` again and save
the exact non-secret before-state and rollback values. Stop if any record's purpose is unclear. If a
restrictive CAA record now exists, confirm it permits GitHub Pages' current certificate issuer.

After explicit approval, replace only confirmed apex parking records with the then-current apex
records in GitHub's custom-domain documentation, and point `www` by CNAME to
`wayfarerlabs.github.io`. Do not create a wildcard record. DNS values must be copied from current
GitHub documentation during the change, not from this runbook's historical snapshot.

Verify all record types against the saved inventory plus approved delta. Then verify both hostnames
over HTTPS, that `www` redirects to the apex without a certificate warning, that canonical metadata
uses `https://agentworks.build`, and that the deployed content matches the expected source commit.

## Rollback and recovery

For a bad site merge, prefer a reviewed fix forward. Use GitHub's
[deployment history](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/view-deployment-history)
to identify the affected deployment and retain its logs as evidence; deployment history is not a
general-purpose artifact rollback mechanism. When the known-good Pages workflow run is still within
GitHub's current rerun window, use the supported
[workflow rerun](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
for that historical run, then verify its commit, exact manifest, and public result. Outside that
window, land a reviewed revert or fix commit on `main` and let the ordinary publishing workflow
deploy it. A fix or revert on `main` is the durable recovery path in either case.

For a DNS or certificate problem, keep the last verified Pages deployment available at its default
URL and compare live records with the saved before-state. Do not disable HTTPS or add forwarding
machinery to work around propagation. Restore only the explicitly changed parking records if the
approved cutover must be reversed.

If Pages is disabled, the repository moves, or the custom domain is detached, promptly remove or
repoint the public apex and `www` records so they cannot target an unclaimed Pages site. Restore
organization domain verification before reconnecting the domain.

For a hosting migration, deploy the exact generated directory to the replacement static host,
validate it at a temporary hostname, and then move DNS using the same inventory, approval, and
rollback discipline. Content sources, templates, URLs, and build contracts do not depend on GitHub
Pages.
