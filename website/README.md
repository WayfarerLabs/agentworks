# Agentworks Website

<!-- cspell:ignore sdds -->

This directory owns the static source for `agentworks.build`. A standard-library Python builder
combines semantic templates, local CSS and JavaScript, the AGW rocket asset, and selected passages
and complete documents from permanent repository sources. Generated output stays outside the
repository and can be published by any static host.

The compact Home page renders the repository-sourced identity and the canonical thin CLI bootstrap
from `packaging/agentworks/assistance.md`. The same bootstrap is projected into the top-level
README; the build fails unless those two source representations are byte-identical. A generated
Manifesto presents the complete long-form argument from `docs/manifesto.md`, while a separate
Security page provides practical depth and a GitHub reporting route. The shared header places the
GitHub and PyPI destinations once per page. A dedicated Lander page presents the same bounded
lunar-deployment game used as progressive enhancement on the useful 404 fallback. The shared footer
places the Manifesto, Security, and icon-only Lander destinations once per page.

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

Then open `http://localhost:8000/`, `http://localhost:8000/manifesto/`,
`http://localhost:8000/security/`, `http://localhost:8000/lander/`, and
`http://localhost:8000/404.html`. A project-Pages build uses the same source and command with
`--site-base /agentworks/`.

Game work normally uses `/lander/` from this complete build; fallback acceptance also exercises the
actual `/404.html`. Both shells receive one already-rendered `lander-game.html` fragment and load
the same CSS, controller, and model. The builder has no partial-output mode because every breadcrumb
and footer links to the other generated pages.

Run the automated suites and repository checks:

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py'
node --test website/tests/*.test.mjs
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
```

The Python website suite requires `chromium`, `chromium-browser`, or `google-chrome` on `PATH`. Its
responsive geometry test launches that browser in headless mode and fails, rather than skips, when
none is available. PR CI and the Pages build therefore prove the wide left-rail and narrow inline
layouts.

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

Generated output is not maintained or edited. The complete output contains exactly:

```text
404.html
index.html
assets/agw-favicon.svg
assets/agw-rocket.svg
manifesto/index.html
lander/index.html
security/index.html
static/lander-game.js
static/lander-model.js
static/onboarding-copy.js
static/lander.css
static/site.css
```

This is the builder's only output shape. The manifest is explicit in `build.py`; the builder never
recursively copies source directories or permits a generated local link outside the manifest.

All three CLI paths are required. The output must be outside the repository. The site base is an
ASCII, slash-bounded same-origin path such as `/` or `/agentworks/`; absolute URLs, dot segments,
encoding, and unbounded paths are rejected. The builder validates all content and templates, stages
an exact regular-file tree beside the destination, and then swaps it into place. Existing output is
replaced only when every entry belongs to the complete manifest.

Local directory URLs and trailing `index.html` aliases resolve to one manifest destination, and
every local fragment must exist in its actual HTML or SVG target. Shared shell labels must remain
visible direct text. `static/site.css` contains no backslash escapes; `display` accepts only `grid`,
`flex`, or `inline-flex`; and `opacity`, `visibility`, and `content-visibility` declarations are
forbidden. These are exact static checks, not a general CSS concealment parser. The manual checklist
still verifies browser-computed visibility, bounds, focus, and pointer reachability.

Replacement uses a sibling backup. A failed installation or installed-manifest check restores the
previous output. Once the installed manifest is verified, the new artifact is committed. A failure
to remove the old backup after that point leaves the valid new artifact installed and prints a
`warning:` naming the retained backup. Remove that specifically named sibling only after confirming
the installed output remains correct.

The same inputs and arguments produce byte-identical files. Build output includes no timestamp,
commit identifier, environment-derived prose, or inherited file. Because output beneath the
repository is rejected, a successful build creates no Git status residue.

## Content ownership

The builder reads four permanent repository inputs:

- `README.md` owns the concise product identity rendered on the landing page. Its short design
  summary remains repository documentation, not additional landing-page content. Its generated
  assistance fence is also checked as an exact projection, not treated as an authored second body.
- `packaging/agentworks/assistance.md` is the single authored source for the Home onboarding prompt.
  It remains a thin install-or-update, version-verification, and `agw guide --agent` handoff.
- `docs/manifesto.md` owns every body heading and block rendered on the Manifesto page.
- `SECURITY.md` owns every body heading and block rendered on the Security page, including the
  private vulnerability reporting channel and URL.

Home alone selects content by a complete heading path and exact normalized blocks. The Manifesto and
Security pages each render one complete Markdown document, including its single source `h1`, through
the same closed transform. Their templates supply only the shared shell and metadata placement.
Supported document edits appear on the site without synchronized website prose, a source hash, a
heading inventory, or selected-section configuration.

Each long-form page derives one `On this page` navigation from its source `h2` and `h3` blocks. It
appears immediately after the source `h1` in ordinary flow, with `h3` links nested beneath their
preceding `h2`. A wide-screen CSS layout presents that same navigation as a left rail; narrow and
zoomed layouts keep it inline. The links target the renderer's existing heading identifiers, and the
navigation is omitted when the source has no `h2` or `h3`. No JavaScript or separate heading
inventory is involved.

The closed document subset supports ATX headings, paragraphs, unordered lists using `-`, `*`, or
`+`, strong text, emphasis using `_` or `*`, inline code, and reviewed inline or reference links.
Reserved Markdown syntax outside that subset fails the build instead of rendering as literal text.

Repository content is escaped before rendering. Missing, unreadable, symlinked, byte-order-marked,
or invalid UTF-8 input; a missing or duplicate `h1`; malformed heading structure; unsupported
Markdown; duplicate identifiers in the complete rendered document; an invalid or unexpected link;
and a GitHub-only reporting violation all fail before output changes. The Manifesto source path is
exactly `docs/manifesto.md`. Do not add probing or a dual-path fallback.

The Manifesto maps only these source-relative links:

- `../README.md` to the stable GitHub README blob URL;
- `guides/idempotency.md` to the stable GitHub idempotency guide blob URL;
- `../cli/README.md#environment-variables-and-secrets` to the stable GitHub CLI README fragment.

Allowed absolute source links are preserved. Any other relative link or an unapproved generated URL
fails closed.

Templates own only navigation, destination labels, metadata placement, presentation-neutral
connective text, game-page headings, and the semantic container around the canonical onboarding
projection. `website/` does not own the prompt body, long-form body headings, product behavior,
security claims, vulnerability contact details, or installation instructions. The selected SVG and
lander implementation are permanent assets and must not be regenerated from design-history files.

Home, Manifesto, Security, Lander, and 404 use the same breadcrumb-led header and traditional
footer. The header has exactly one linked `Agentworks` home crumb, a hidden visual separator, a
non-linked current-page item, and one icon-and-text link each for GitHub and PyPI. Home omits the
small header mark because its large hero follows; every other page has exactly one decorative small
mark immediately before the breadcrumb. The footer has exact ownership text plus one Manifesto and
one Security text link, followed by a selected-rocket icon link named `Play Lunar Lander`. Each
service icon has one pinned direct path; its adjacent visible text supplies the accessible name.
Those local and external destinations are not repeated in the body. On 404, the linked home crumb is
the sole visible route-home action.

Every page head references `assets/agw-favicon.svg`, a transparent, flame-free projection of the
neutral A/G/W mark. Its geometry is checked against `assets/agw-rocket.svg`; maintain the selected
mark there and update the favicon projection in the same reviewed change.

Build artifacts are disposable projections of the templates and permanent sources; maintain the
sources, not generated HTML.

## Onboarding projection

The Home template contains one semantic `pre` and `code` container. The builder reads the canonical
prompt as exact NUL-free, LF-terminated UTF-8, proves that the collision-proof generated README
fence contains the same bytes, escapes it for HTML, and places it once. Missing, symlinked,
malformed, normalized, or divergent inputs fail before output changes. The generated code element's
decoded text is therefore byte-identical to the canonical source.

Without JavaScript, the prompt remains visible and manually selectable. The local
`static/onboarding-copy.js` module reveals the copy button only when clipboard writing is available,
writes the code element's exact `textContent` on user activation, and reports success or a manual
fallback through a polite status region without moving focus. It reads no clipboard data, makes no
network request, and provides no ongoing Agentworks guidance. Manifesto and Security have no
JavaScript. Lander and the custom 404 load only the same-origin game module.

## GitHub Pages setup and deployment

These one-time repository and organization settings require an operator with the relevant GitHub
permissions. Record non-secret evidence when each action is completed.

Pre-merge setting evidence, recorded 2026-08-09: repository Pages reports `build_type: workflow`,
the default URL `https://wayfarerlabs.github.io/agentworks/`, HTTPS enforcement enabled, and no
custom domain. The `github-pages` environment uses custom deployment-branch policies rather than all
protected branches and has exactly one policy: branch `main`. No site deployment is expected until
the publishing workflow first runs from a merged `main` commit.

1. Set GitHub Pages to use GitHub Actions as its publishing source.
2. Protect the `github-pages` environment so only the default branch can deploy.
3. Merge the complete publishing workflow and verify the expected commit at the default Pages URL
   before attaching the custom domain.
4. Verify `agentworks.build` for the WayfarerLabs organization and retain the generated GitHub TXT
   record.
5. Set `agentworks.build` as this repository's custom domain. Do not mutate DNS yet.
6. On the same already verified implementation merge-push workflow, use GitHub's **Re-run all
   jobs**. Prove the rerun checked out the same source SHA, normalized `site_base=/`, built and
   uploaded the exact root-base twelve-file artifact, and deployed that artifact successfully. If
   the deployment fails or cannot be verified, execute the activation rollback below.
7. Re-inventory DNS. Only after the same-SHA root deployment is proven, obtain explicit operator
   approval for the exact cutover and then change only the identified parking records.
8. Verify apex content, the `www` redirect, certificate hostname, and HTTPS enforcement.

The publishing workflow must build and test from a clean checkout on every push to `main`, without
path filters. Authoritative inputs live outside `website/`, so a website-only trigger could publish
stale content. The build job uploads exactly the generated directory. The deploy job uses the
`github-pages` environment with only Pages write and OIDC token permissions. Pull requests build and
test but do not deploy or receive those permissions.

Attaching the custom domain changes only the builder's site-base input from `/agentworks/` to `/`.
The required same-workflow rerun makes that root-base transition observable before DNS approval. It
does not select different content or require a second artifact path. The artifact contains no
`CNAME`; the repository setting is the custom-domain authority.

The default project URL may be degraded between attaching the repository custom-domain setting and
verifying the root-base rerun. The previous project-base artifact is not guaranteed to remain
available during this interval. If the same-SHA root-base rerun or deployment fails or cannot be
verified, leave all DNS records unchanged and detach the repository custom-domain setting. Preserve
the WayfarerLabs organization verification and its TXT record, along with every unrelated DNS
record. On the same latest verified `main` push workflow, use **Re-run all jobs** again. Verify that
`configure-pages` selected `/agentworks/`, that the rerun checked out the same source SHA, and that
the exact project-base twelve-file artifact deployed successfully. Verify that same SHA at
`https://wayfarerlabs.github.io/agentworks/`, then stop. Retry custom-domain activation only through
the full reviewed sequence above.

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

For a DNS or certificate problem, compare live records with the saved before-state. Do not assume
that the previous project-base artifact remains available at the default URL after changing the
custom-domain setting. Do not disable HTTPS or add forwarding machinery to work around propagation.
Restore only the explicitly changed parking records if the approved cutover must be reversed.

If Pages is disabled, the repository moves, or the custom domain is detached after public DNS points
to Pages, promptly remove or repoint the apex and `www` records so they cannot target an unclaimed
Pages site. Keep organization domain verification intact; if it was lost, restore it before
reconnecting the domain.

For a hosting migration, deploy the exact generated directory to the replacement static host,
validate it at a temporary hostname, and then move DNS using the same inventory, approval, and
rollback discipline. Content sources, templates, URLs, and build contracts do not depend on GitHub
Pages.
