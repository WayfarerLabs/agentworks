# LLD: Static Website Shell and Manifesto

<!-- cspell:ignore canonicalization keypath keypaths nonblank sdds TUI -->

- Status: Phase 4F implemented; release acceptance remains pending
- Date: 2026-08-10
- FRD: `frd.md`, specifically R7-R11 and R13-R20
- HLA: `hla.md`, specifically D1-D5, D7, D8, and D10
- Source baseline: `5598a12c`

## 1. Scope and release invariant

The checked-in `website/` tree produces the complete static shell for Home, Manifesto, Security,
Lander, and 404. Phase 4C adds a deliberate game route and a footer easter-egg link without changing
deployment, DNS, onboarding, or game mechanics. Home, Manifesto, and Security remain script-free.
Lander and 404 remain useful without JavaScript and progressively enhance only their one shared game
subtree.

The release still has no guided onboarding implementation. Home contains the one reviewed ordinary
text availability notice, with no command, copy control, empty placeholder, or runtime release mode.
A later onboarding phase replaces that notice through its own canonical contract.

## 2. Permanent files, routes, and output

| Source                               | Responsibility                                                              |
| ------------------------------------ | --------------------------------------------------------------------------- |
| `website/templates/index.html`       | Compact repository-sourced identity and interim onboarding notice           |
| `website/templates/manifesto.html`   | Presentation shell for the generated long-form argument                     |
| `website/templates/security.html`    | Repository-sourced security depth and GitHub reporting route                |
| `website/templates/lander.html`      | Dedicated semantic shell for the shared lunar deployment game               |
| `website/templates/404.html`         | Useful error surface and progressively enhanced lander                      |
| `website/templates/lander-game.html` | Sole template source for the reusable game subtree                          |
| `website/assets/agw-favicon.svg`     | Flame-free browser icon projection of the selected mark                     |
| `website/assets/agw-rocket.svg`      | Selected self-contained brand mark                                          |
| `website/static/site.css`            | Shared tokens, shell, document presentation, focus, and reflow              |
| `website/static/lander.css`          | Shared Lander/404 scene and mission presentation                            |
| `website/static/lander-model.js`     | Pure deterministic lander model                                             |
| `website/static/lander-game.js`      | Page-agnostic game controller                                               |
| `website/build.py`                   | Closed inputs, rendering, validation, manifest, and atomic installation     |
| `website/site_content.py`            | Complete Markdown projection and safe HTML rendering                        |
| `website/site_validation.py`         | Template, shell, CSS, and local-reference validation                        |
| `website/site_asset_validation.py`   | Exact canonical and favicon head-link contracts                             |
| `website/tests/`                     | Source, template, generated-document, builder, workflow, and game contracts |
| `website/README.md`                  | Permanent build, content-ownership, publishing, and recovery runbook        |

The complete generated artifact is exactly:

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
static/lander.css
static/site.css
```

The supported public paths are `/`, `/manifesto/`, `/security/`, `/lander/`, and `/404.html`. At the
GitHub Pages project base, the same paths are rooted beneath `/agentworks/`. Canonical metadata
always uses the custom-domain URLs at `https://agentworks.build`. Game development and demos serve
`/lander/` from this same complete linked artifact; fallback acceptance also exercises `/404.html`.

The Lander metadata contract is exact:

- document title: `Lunar deployment | Agentworks`;
- `h1`: `Lunar deployment`;
- description: `Fly the Agentworks lunar deployment mission and deliver an agent to the NOC.`;
- canonical URL: `https://agentworks.build/lander/`;
- Content Security Policy: byte-identical to 404's restrictive policy:
  `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self';`
  `connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'`.

Template and generated-document mutation tests pin every value and reject a missing, duplicated,
relaxed, reordered, or route-mismatched metadata field.

## 3. Shared header contract

Every page has one `header` after the skip link. Its first region is the page identity:

1. Manifesto, Security, Lander, and 404 have exactly one decorative small header rocket with empty
   alternative text. It is the first child of `.header-identity`, immediately followed by the
   breadcrumb.
2. Home has no `.header-mark`; its large semantic hero mark follows in `main`.
3. The breadcrumb is a `nav` named `Breadcrumb`. It has a linked `Agentworks` home crumb, one `/`
   separator hidden with `aria-hidden="true"`, and one non-anchor current item with
   `aria-current="page"`.
4. Current-item text is exactly `Home`, `Manifesto`, `Security`, `Lander`, or `404`.

The second region is one `nav` named `External`. It contains exactly one repository link labeled
`GitHub` and one package link labeled `PyPI`. Each anchor contains one inline local SVG followed by
visible direct label text. Each SVG has exactly one direct `path` with its reviewed service
geometry, `aria-hidden="true"`, and `focusable="false"`; the text remains the accessible name if CSS
or the icon is unavailable. No remote icon asset or package is used.

The 404 breadcrumb's linked `Agentworks` crumb is its sole visible route-home action. The error body
contains no second home link. The controller has no dependency on that removed element, so scene,
focus, keyboard, pointer, reduced-motion, no-JavaScript, and recovery behavior remain unchanged.

## 4. Shared footer contract

Every page has one `.site-footer` after `main`. Its left side is exactly:

```text
Product of Wayfarer Labs, LLC
```

Its right side is one `nav` named `Footer` containing exactly:

- `Agentworks Manifesto` to `{{SITE_BASE}}manifesto/`;
- `We take security seriously` to `{{SITE_BASE}}security/`;
- an icon-only `.footer-game-link` to `{{SITE_BASE}}lander/#lander-game`, with the accessible name
  `Play Lunar Lander` and one `.footer-game-mark` image using the selected rocket, empty alternative
  text, and no duplicate visible label.

The Manifesto, Security, and Lander destinations occur nowhere else as anchors on a page. The
repository and package destinations occur nowhere else as anchors on a page. The linked home crumb
is also unique. The footer rocket is its final right-side item and remains visibly at the lower
right in ordinary document flow; it is not fixed to the viewport. The header and footer use wrapping
flex layouts in source order, with no menu or hidden navigation.

The deliberately small visible footer mark sits inside an interactive area of at least 24 by 24 CSS
pixels at every viewport. Padding may provide that area without enlarging the mark. The link uses
the shared three-pixel focus outline and two-pixel offset without clipping. Automated CSS and
template assertions pin its minimum target dimensions and accessible name; manual narrow-width,
zoom, pointer, and keyboard acceptance verifies computed size, focus visibility, and no overlap.

## 5. Whole-document Markdown source contract

The builder renders two complete normalized UTF-8 Markdown documents:

| Route         | Current source           |
| ------------- | ------------------------ |
| `/manifesto/` | `docs/why-agentworks.md` |
| `/security/`  | `SECURITY.md`            |

Each source owns every body heading and paragraph on its page, including exactly one source `h1`.
Each template contains one sourced-content token in `main` and supplies no additional body title,
section heading, prose, selected passage, or reporting panel. Document titles, descriptions,
canonical URLs, breadcrumbs, header, and footer remain site-shell responsibilities.

The closed renderer supports ATX headings, paragraphs, unordered lists, strong and emphasized text,
code spans, and links. It escapes source text and rejects raw HTML, images, unsupported block or
inline syntax, invalid UTF-8, byte-order marks, malformed or unclosed fences, a missing or duplicate
`h1`, and unsafe or unexpected links before output replacement. It renders every supported source
block in order. It deliberately does not pin source hashes, heading inventories, expected passages,
or heading-path selections; ordinary supported document edits therefore appear on the site without a
website-code edit.

The renderer also derives one `On this page` navigation from the parsed document's `h2` and `h3`
blocks. The navigation is inserted immediately after the source `h1`, links to the same generated
heading identifiers, preserves source order, and nests each `h3` beneath its preceding `h2`. It is
omitted when a document has no `h2` or `h3`. This derived navigation is not a second content model:
it is computed from the same validated blocks during the same render pass, and no heading inventory
is stored in code or tests. Its default flow is inline. At the wide-screen breakpoint a derived
layout wrapper becomes a two-column grid. The source `h1` and one grouped post-title body occupy the
right column while the navigation spans both rows in the left column, so body copy begins beside the
rail instead of waiting for its height. DOM and keyboard order do not change.

The Manifesto source path is exactly `docs/why-agentworks.md` in this release. A later document
rename changes that one configuration value to `docs/manifesto.md` in the same reviewed rename.
There is no dual-path fallback, probing, or autodetection.

Source-relative links use this exact allowlist:

| Source destination                                   | Generated destination                                                                                  |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `../README.md`                                       | `https://github.com/WayfarerLabs/agentworks/blob/main/README.md`                                       |
| `guides/idempotency.md`                              | `https://github.com/WayfarerLabs/agentworks/blob/main/docs/guides/idempotency.md`                      |
| `../cli/README.md#environment-variables-and-secrets` | `https://github.com/WayfarerLabs/agentworks/blob/main/cli/README.md#environment-variables-and-secrets` |

The existing absolute issue link is preserved. Any other relative link or unapproved generated
absolute URL fails. Tests prove all three mappings and prove no source-relative `href` survives.

## 6. Other repository content contracts

The builder reads exactly three permanent content inputs: `README.md`, `docs/why-agentworks.md`, and
`SECURITY.md`. README continues to own the concise selected Home identity. The other two inputs each
own one complete long-form page. Only Home retains a heading-keypath plus exact-block selection;
long-form content has no duplicated prose contract in Python or templates.

Templates may not move content tokens outside their reviewed metadata or sourced containers. All
templates use a closed token vocabulary and reject unknown, missing, duplicated, or brace-like
tokens. The Security output retains the GitHub-only reporting invariant and rejects address-shaped
reporting paths.

## 7. Builder and replacement safety

The CLI requires `--repo-root`, `--output`, and `--site-base`. It has no partial-output option. Site
bases accept only ASCII slash-bounded same-origin paths such as `/` and `/agentworks/`. Output must
be outside the repository.

The builder validates sources, templates, shell destinations, labels, landmark locations, current
state, icons, complete image inventory, ownership text, normalized route uniqueness, and local
references in memory. It renders the complete explicit manifest to a sibling staging directory,
verifies exact regular files and directories, and atomically installs only after validation.
Existing output is accepted only when every entry is builder-owned. No validator exception permits a
generated local link outside the manifest. A failed install restores the prior output.

One local-reference resolver serves both route uniqueness and manifest validation. Directory routes
and their trailing `index.html` forms resolve to the same manifest path, including fragment-bearing
references; root and `/index.html` likewise resolve together. Every same- or cross-document fragment
must identify an actual element in its HTML or SVG target. Shared shell labels are visible leaf
text, not text inherited from hidden or structural descendants. In `static/site.css`, the builder
rejects backslash escapes, allows `display` only as `grid`, `flex`, or `inline-flex`, and rejects
every `opacity`, `visibility`, or `content-visibility` declaration; manual acceptance still verifies
computed styles. That declaration vocabulary is deliberately narrow, not a claim to detect every
possible concealment technique. Browser acceptance separately verifies each reviewed shell link's
computed visibility, in-viewport bounds, keyboard focus, and pointer reachability, including a
canary for off-screen absolute positioning. No home-grown general CSS parser is introduced.

`website/build.py` remains the executable CLI and artifact orchestrator. Content projection and
Markdown rendering move to `website/site_content.py`; template, CSS, shell, and local-reference
validation move to `website/site_validation.py`; exact canonical and favicon head-link validation
lives in the focused `website/site_asset_validation.py`. Each production module and each test module
remains below 1,000 lines. The split preserves one public build command and introduces no package or
runtime dependency.

Tests own a literal expected eleven-file manifest independent of production constants. They scan
every static JavaScript module import, resolve same-origin relative imports against its emitted
path, and require the target in that literal manifest; a missing `lander-model.js` mutation must
fail. A malicious reviewed-link canary containing quotes, ampersands, and an attempted attribute
boundary must serialize as one escaped `href` with no injected attribute. These witnesses pin
manifest and attribute safety without duplicating the whole builder implementation.

The same inputs and arguments produce byte-identical output. Artifacts contain no timestamps,
environment prose, or generated `CNAME`, and successful builds leave the repository clean.

### 7.1 Shared Lander fragment and detail headings

`website/templates/lander-game.html` owns the complete `<section id="lander-game">` subtree. It uses
only its closed `{{SITE_BASE}}` token. The builder validates and renders that trusted fragment, then
inserts its exact bytes through one `{{LANDER_GAME}}` placement in both `lander.html` and
`404.html`. The outer shells may differ; the game subtree may not. The fragment is a source input,
not an emitted route. Generated-document tests parse both pages and prove their `#lander-game`
subtrees are byte-equivalent, and mutation tests reject a missing, duplicate, moved, or
independently edited placement.

Both game shells load the same `site.css`, `lander.css`, and `lander-game.js`. Each document
contains only one game subtree, so the controller's stable IDs remain unique without route-specific
logic. Lander uses the document title `Lunar deployment | Agentworks` and the `h1`
`Lunar deployment`; 404 retains its established metadata and the `h1` `Page not found`. Each `main`
uses the shared `.detail-main` inset and a game-specific compact gap. Its `.page-heading` is the
first child and contains only the reviewed `h1`. The 404 explanatory paragraph follows the heading,
and neither shell includes an eyebrow, error code, provenance, or other pre-title label.

## 8. Accessibility, reflow, and presentation

All pages retain one visible-on-focus skip link, one `h1`, logical heading order, and `header`,
`main`, and `footer` landmarks. Native links preserve keyboard behavior and accessible names. Focus
outlines remain visible. Decorative header marks use empty alternative text; service icons and the
breadcrumb separator are explicitly hidden from the accessibility tree.

The local system-font visual language keeps the accepted neutral palette, mono accents, crisp
boundaries, and compact labels. It does not introduce a fake terminal, remote font, remote asset,
client routing, or essential motion. Header and footer regions wrap rather than overflow. Body and
content dimensions use `min-width: 0`, bounded widths, fluid spacing and type, and anywhere link
wrapping to preserve one-dimensional reflow at 320 CSS pixels and the 400-percent zoom equivalent.
Manifesto, Security, Lander, and 404 begin with their `h1` and no eyebrow, error-code, or
repository-provenance label. Their shared detail-main inset is `clamp(0.75rem, 2vw, 1.25rem)` below
the header; the page heading adds no second top inset. Canonical-source provenance remains a build
contract rather than visitor-facing chrome.

The Home hero's `3.2rem` to `4.8rem` width is measured against the accepted pre-refinement
`1.6rem`-wide header presentation, yielding two to three times that historical baseline. It is not
specified as two to three times the current compact `1.2rem` header mark; no CSS change is required.

## 9. Verification matrix

| Contract                       | Automated evidence                                                                                                             |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| Source completeness and safety | Complete-source projection, single-`h1`, UTF-8, fence, syntax, and link-map failure tests                                      |
| Template closure               | Token vocabulary, exact shell tree, HTML-hidden CTA, icon, breadcrumb, image, route-duplicate, and ownership mutation tests    |
| Generated semantics            | Five-page metadata, canonicals, landmarks, headings, skip links, shell, no-duplicate links, scripts, and local-reference tests |
| Exact artifacts                | The complete eleven-file manifest at `/` and `/agentworks/`; no partial API or CLI option                                      |
| Determinism and safety         | Repeated byte snapshots, hostile output trees, rollback injection, path and symlink tests                                      |
| Lander/404 preservation        | Shared-subtree identity, Python source/build tests, and Node model/controller contracts                                        |
| Browser acceptance             | `website/tests/lander-browser-checklist.md` pending five-page manual run                                                       |

Before handoff, run:

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py'
node --test website/tests/lander-model.test.mjs
python3 website/build.py --repo-root . --output /tmp/agentworks-site-root --site-base /
python3 website/build.py --repo-root . --output /tmp/agentworks-site-project --site-base /agentworks/
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
git diff --check
```

The operator's 2026-08-09 ruling makes Chrome and Edge the pre-merge manual browser gate on the
forwarded preview. Unit tests still do not prove physical touch feel, spoken screen-reader quality,
or independent-engine reflow, so Firefox/WebKit, spoken screen-reader, physical mobile/touch, and
broader device rows remain mandatory post-launch production acceptance before closeout and
`locked.md`; defects found there enter the next website work round.
