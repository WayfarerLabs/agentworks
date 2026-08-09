# LLD: Static Website Shell and Manifesto

<!-- cspell:ignore canonicalization keypath keypaths nonblank sdds TUI -->

- Status: Phase 4B implemented; release acceptance remains pending
- Date: 2026-08-09
- FRD: `frd.md`, specifically R10, R11, and R13-R17
- HLA: `hla.md`, specifically D1-D5, D8, and D10
- Source baseline: `1a52d4250bc0c7ff7edf29beeb1ba8067beeb2e5`

## 1. Scope and release invariant

The checked-in `website/` tree produces the complete static shell for Home, Manifesto, Security,
and 404. The phase changes navigation and adds the generated Manifesto without changing deployment,
DNS, onboarding, or the bounded 404 game. Home and Security remain script-free. Manifesto is also
script-free. The 404 remains useful without JavaScript and progressively enhances only its game.

The release still has no guided onboarding implementation. Home contains the one reviewed ordinary
text availability notice, with no command, copy control, empty placeholder, or runtime release mode.
A later onboarding phase replaces that notice through its own canonical contract.

## 2. Permanent files, routes, and output

| Source                             | Responsibility                                                              |
| ---------------------------------- | --------------------------------------------------------------------------- |
| `website/templates/index.html`     | Compact repository-sourced identity and interim onboarding notice           |
| `website/templates/manifesto.html` | Presentation shell for the generated long-form argument                     |
| `website/templates/security.html`  | Repository-sourced security depth and reporting routes                      |
| `website/templates/404.html`       | Useful error surface and progressively enhanced lander                      |
| `website/assets/agw-rocket.svg`    | Selected self-contained brand mark                                          |
| `website/static/site.css`          | Shared tokens, shell, document presentation, focus, and reflow              |
| `website/static/lander.css`        | 404 scene and mission presentation                                          |
| `website/static/lander-model.js`   | Pure deterministic lander model                                             |
| `website/static/lander-game.js`    | 404-only game controller                                                    |
| `website/build.py`                 | Closed inputs, rendering, validation, manifest, and atomic installation     |
| `website/tests/`                   | Source, template, generated-document, builder, workflow, and game contracts |
| `website/README.md`                | Permanent build, content-ownership, publishing, and recovery runbook        |

The complete generated artifact is exactly:

```text
404.html
index.html
assets/agw-rocket.svg
manifesto/index.html
security/index.html
static/lander-game.js
static/lander-model.js
static/lander.css
static/site.css
```

The supported public paths are `/`, `/manifesto/`, `/security/`, and `/404.html`. At the GitHub
Pages project base, the same paths are rooted beneath `/agentworks/`. Canonical metadata always uses
the custom-domain URLs at `https://agentworks.build`. Game development and demos serve `/404.html`
from this same complete linked artifact.

## 3. Shared header contract

Every page has one `header` after the skip link. Its first region is the page identity:

1. Manifesto, Security, and 404 have exactly one decorative small rocket with empty alternative
   text. It is the first child of `.header-identity`, immediately followed by the breadcrumb.
2. Home has no `.header-mark`; its large semantic hero mark follows in `main`.
3. The breadcrumb is a `nav` named `Breadcrumb`. It has a linked `Agentworks` home crumb, one `/`
   separator hidden with `aria-hidden="true"`, and one non-anchor current item with
   `aria-current="page"`.
4. Current-item text is exactly `Home`, `Manifesto`, `Security`, or `404`.

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
- `We take security seriously` to `{{SITE_BASE}}security/`.

The Manifesto and Security destinations occur nowhere else as anchors on a page. The repository and
package destinations occur nowhere else as anchors on a page. The linked home crumb is also unique.
The header and footer use wrapping flex layouts in source order, with no menu or hidden navigation.

## 5. Manifesto source contract

`docs/why-agentworks.md` remains the only long-form prose source and retains its repository title.
The presentation title, document title, `h1`, and canonical route are `Agentworks Manifesto`. The
template owns only that presentation shell and connective labels.

The builder reads the complete normalized UTF-8 source. Its contract pins:

- a reviewed SHA-256 of the complete source;
- the exact ordered heading tree from `# Why Agentworks` through all Problem Space and Key
  Principles subsections;
- one top-level source heading, which the presentation `h1` replaces;
- only the existing closed Markdown blocks: ATX headings, paragraphs, unordered lists, strong and
  emphasized text, code spans, and links.

The generated article includes the complete introduction, the complete Problem Space and every
subsection, and the complete Key Principles and every subsection. The source hash makes a prose,
link, or whitespace change fail closed until the reviewed contract is updated. Heading changes,
missing or additional sections, unsupported Markdown, invalid links, invalid UTF-8, and unclosed
fences also fail before any output replacement. Rendered prose is never maintained in a template or
Python string.

Source-relative links use this exact allowlist:

| Source destination                                   | Generated destination                                                                                  |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `../README.md`                                       | `https://github.com/WayfarerLabs/agentworks/blob/main/README.md`                                       |
| `guides/idempotency.md`                              | `https://github.com/WayfarerLabs/agentworks/blob/main/docs/guides/idempotency.md`                      |
| `../cli/README.md#environment-variables-and-secrets` | `https://github.com/WayfarerLabs/agentworks/blob/main/cli/README.md#environment-variables-and-secrets` |

The existing absolute issue link is preserved. Any other relative link or unapproved generated
absolute URL fails. Tests prove all three mappings and prove no source-relative `href` survives.

## 6. Existing repository content contracts

The builder still reads exactly three permanent content inputs: `README.md`,
`docs/why-agentworks.md`, and `SECURITY.md`. README owns the concise Home identity. The Why document
owns the complete Manifesto plus selected security passages. SECURITY owns private reporting prose
and its reference URL.

Home and Security selections continue to use complete heading keypaths plus exact normalized block
sequences. Their output is escaped and rendered through the same inline and block renderer used by
the Manifesto. Templates may not move content tokens outside their reviewed metadata or sourced
containers. All templates use a closed token vocabulary and reject unknown, missing, duplicated, or
brace-like tokens.

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
computed styles.

The same inputs and arguments produce byte-identical output. Artifacts contain no timestamps,
environment prose, or generated `CNAME`, and successful builds leave the repository clean.

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
Manifesto and Security begin with their `h1` and no repository-provenance eyebrow. Their shared
detail-main inset is `clamp(0.75rem, 2vw, 1.25rem)` below the header; the page heading adds no
second top inset. Canonical-source provenance remains a build contract rather than visitor-facing
chrome.

## 9. Verification matrix

| Contract                      | Automated evidence                                                                                                             |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Source completeness and drift | Hash, heading-tree, UTF-8, fence, block, and link-map failure tests                                                            |
| Template closure              | Token vocabulary, exact shell tree, hidden CTA, icon, breadcrumb, image, route-duplicate, and ownership mutation tests         |
| Generated semantics           | Four-page metadata, canonicals, landmarks, headings, skip links, shell, no-duplicate links, scripts, and local-reference tests |
| Exact artifacts               | The complete nine-file manifest at `/` and `/agentworks/`; no partial API or CLI option                                        |
| Determinism and safety        | Repeated byte snapshots, hostile output trees, rollback injection, path and symlink tests                                      |
| 404 preservation              | Python source/build tests plus Node model/controller contracts                                                                 |
| Browser acceptance            | `website/tests/lander-browser-checklist.md` pending four-page manual run                                                       |

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

The manual browser checklist remains a release gate because unit tests do not prove physical touch
feel, spoken screen-reader quality, or engine-specific reflow.
