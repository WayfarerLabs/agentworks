# LLD: Interim Website Shell

<!-- cspell:ignore autolinks blockquotes canonicalization keypath keypaths -->
<!-- cspell:ignore navs nonblank scroller TUI -->

- Status: Approved for implementation; Phase 4 in progress
- Date: 2026-08-09
- FRD: `frd.md`, specifically R10, R11, and R13
- HLA: `hla.md`, specifically D1-D5, D8, and D10
- Source baseline: `origin/main` at `7f54658cf4d33c54016f05e4903bdc0726cb945f`; the three permanent
  content inputs below are byte-identical at this worktree's HEAD

## 1. Scope and release invariant

This LLD pins the home and security shells that extend the accepted Phase 2 404 artifact into the
useful interim release. It covers only merged repository content, page structure, shared visual
language, deterministic building, and interim verification. Deployment workflow and DNS mechanics
remain Phase 5 and Phase 6 work.

The release has no onboarding implementation. The home page contains one populated, ordinary-text
availability region at the location the canonical bootstrap will later occupy. There is no bootstrap
text, installation command, `pre` region, copy control, copy script, empty placeholder, feature
flag, preview mode, or branch-only input. Phase 7 must design the later replacement from the
then-current contract on `main`; this LLD does not predict it.

## 2. Permanent files, URLs, and ownership

Phase 4 extends the existing `website/` tree without renaming the accepted lander files.

| File                                        | Responsibility                                                    |
| ------------------------------------------- | ----------------------------------------------------------------- |
| `website/templates/index.html`              | Semantic home template and populated interim onboarding region    |
| `website/templates/security.html`           | Semantic security deep dive                                       |
| `website/templates/404.html`                | Existing useful error and lander template, plus shared site CSS   |
| `website/assets/agw-rocket.svg`             | Existing selected, font-independent brand asset                   |
| `website/static/site.css`                   | Shared tokens, typography, landmarks, panels, links, and reflow   |
| `website/static/lander.css`                 | Lander scene and mission states that are not shared page styling  |
| `website/static/lander-model.js`            | Existing pure lander model                                        |
| `website/static/lander-game.js`             | Existing 404-only progressive enhancement                         |
| `website/build.py`                          | Explicit manifest, source extraction, rendering, and safe staging |
| `website/tests/test_site_build.py`          | Shell content, builder, DOM, URL, and interim-release contracts   |
| `website/tests/test_lander_404.py`          | Existing focused 404 source and build contracts                   |
| `website/tests/lander-model.test.mjs`       | Existing deterministic game coverage                              |
| `website/tests/lander-browser-checklist.md` | Existing browser acceptance, extended with shared-shell rows      |
| `website/README.md`                         | Permanent local, publishing, DNS, rollback, and ownership runbook |

The public mappings are exact:

| Artifact path           | Custom-domain URL                                | Project-Pages path       |
| ----------------------- | ------------------------------------------------ | ------------------------ |
| `index.html`            | `https://agentworks.build/`                      | `/agentworks/`           |
| `security/index.html`   | `https://agentworks.build/security/`             | `/agentworks/security/`  |
| `404.html`              | `https://agentworks.build/404.html`              | `/agentworks/404.html`   |
| `assets/agw-rocket.svg` | `https://agentworks.build/assets/agw-rocket.svg` | `/agentworks/assets/...` |
| `static/*`              | `https://agentworks.build/static/*`              | `/agentworks/static/*`   |

The home and security canonical links are always `https://agentworks.build/` and
`https://agentworks.build/security/`. Local navigation and assets use the validated site base.
External destinations are fixed to:

- repository: `https://github.com/WayfarerLabs/agentworks`;
- package: `https://pypi.org/project/agentworks-cli/`;
- rationale: `https://github.com/WayfarerLabs/agentworks/blob/main/docs/why-agentworks.md`;
- security policy: `https://github.com/WayfarerLabs/agentworks/security/policy`.

No generated file is checked in. The artifact contains no `CNAME`; the Pages repository setting is
the custom-domain authority.

## 3. Content classes and exact site-owned text

Product and security claims come only from section 4's permanent inputs. Templates may own
navigation, headings, and presentation-neutral connective labels. These two interim strings are
exact and are each present once on the home page:

```text
Guided onboarding is not yet published. You can still explore the repository, PyPI package, rationale, and security model.
```

```text
We take security seriously.
```

The first string is the complete text of `#onboarding-availability`. The second is the only home
link text for the security deep dive and links to `{{SITE_BASE}}security/`. It is a normal secondary
link, not a banner, warning, modal, prerequisite, or dominant action.

Other exact destination labels are `View the GitHub repository`, `View the PyPI package`,
`Read why Agentworks is built this way`, `Read the repository security policy`,
`GitHub private vulnerability reporting`, and `Return to agentworks.build`. Labels and section
headings make no new behavior, security, or installation claim.

## 4. Current-main content contracts

The builder's manifest names exactly `README.md`, `docs/why-agentworks.md`, and `SECURITY.md` as
content inputs. A selector is a full ATX heading keypath plus an exact expected normalized block or
contiguous block sequence. Heading levels and text are part of the keypath; matching by paragraph
position is forbidden.

### 4.1 Home passages

`HOME_IDENTITY` selects this exact two-paragraph sequence under `README.md` keypath `# Agentworks`:

```text
A comprehensive toolkit for managing agentic workloads: VMs, workspaces, agents, sessions, harnesses, secrets/config, and the supporting systems that glue them together. Built around the conviction that autonomy, security, and control are not mutually exclusive: a good platform makes it possible and straightforward to have it all.

Create and manage an agentic fleet from your own workstation. **Durable agents** run as separate Linux users in **VMs** on infrastructure you choose and control. They retain their own tools, git credentials, and accumulated application state (a coding assistant's context and memory, interactive logins). **Disposable sessions** spin up against them for a single piece of work and are thrown away when done. One `agw` CLI drives all of it declaratively via an SSH-over-Tailscale control plane.
```

`HOME_PROBLEM` selects the complete two-paragraph body under `docs/why-agentworks.md` keypath
`# Why Agentworks > ## The Problem Space > ### Workload Management`:

```text
Anyone who has had more than a few parallel agentic sessions has likely run into the problem of keeping track of which agents are doing what, which sessions are active, what tools and credentials are available in each session, how to coordinate work across multiple agents (possibly working in the same repository or worktree), how to keep them all running reliably (e.g. even when you close your laptop or lose your network connection), etc.

These are real challenges that impose real limits on how many agentic workloads a single operator can reasonably manage at once. Most devs who have leaned into this space have developed some amount of custom tooling to help with this problem. Solving for this at the platform layer lets devs and their agents focus on shipping code instead of fiddling with infrastructure.
```

`HOME_PRINCIPLES` selects the introductory paragraph and four-item list under `README.md` keypath
`# Agentworks > ## Why It's Built This Way`:

```text
A few convictions shape the whole design. The short version:

- **Autonomy and control are not a tradeoff.** Much of the ecosystem treats loss of control as the price of agentic autonomy; Agentworks is built on the opposite bet, that the right platform lets you have both.
- **Composable, Linux-native isolation.** The hard boundary is the VM; agents are Linux users and workspaces are Linux groups. Use the full model or any subset, and because it is all ordinary users, groups, and filesystem permissions, graduated privilege between cooperating agents (a low-privilege researcher handing artifacts to a privileged actor) is an everyday pattern, not a special case.
- **Support for differing levels of ephemerality.** Different operators have different needs for how long-lived their workloads and related resources are. Robust, declarative templates facilitate rapid setup and scale, while idempotent reinitialization and reuse of resources across workloads allow durable resources such as agents to accumulate state and context.
- **Declarative and idempotent.** Every layer is templated and declared, and the long-lived resources (VMs and agents) can be reinitialized to pick up changes, so environments stay consistent and evolve predictably rather than drifting.
```

### 4.2 Security passages

`SECURITY_THREATS` selects the opening paragraph and four-item list under `docs/why-agentworks.md`
keypath `# Why Agentworks > ## The Problem Space > ### Security`:

```text
Agentic engineering is inherently risky. These risks come from multiple directions, including:

- **Honest mistakes** - An agent can simply make a mistake that results in data loss, corruption, or unintended side effects. It's very easy to find stories of Claude wiping out entire directories or otherwise causing havoc.
- **Prompt injection** - Agents that are exposed to the outside world (e.g. by downloading untrusted web content) can potentially be manipulated into doing things outside of their operator's intent or control.
- **Supply chain attacks** - Agents may download and run compromised software or dependencies from external sources, which could introduce malicious code into the environment, at build time, runtime, or both.
- **Rogue agents** - The agent itself could behave maliciously due to a compromise of the model, the provider, or emergent behavior.
```

`SECURITY_BOUNDARIES` selects these two adjacent paragraphs later in that same section:

```text
All of these suggest similar solutions, though. You need strong guardrails (isolation, permissions, etc.) to ensure that _when_ things go sideways, the blast radius is contained and the operator retains control.

Being precise about what those guardrails do is as important as having them. Agentworks builds its isolation from VM boundaries plus standard Linux users, groups, and filesystem permissions. That separates agents' credentials and state from one another and bounds what a mistaken or compromised agent can reach. Two things it deliberately does not do: it is not a kernel-level sandbox (agents on one VM share a kernel, so a local privilege escalation is a path between them), and it does not yet constrain outbound network access, so an agent that reads untrusted content can still reach the network with whatever it can read (tracked in [#224](https://github.com/WayfarerLabs/agentworks/issues/224)).
```

`SECURITY_POSTURE` selects the complete three-paragraph body under `docs/why-agentworks.md` keypath
`# Why Agentworks > ## Key Principles > ### Composable Isolation`:

```text
This model provides several isolation mechanisms, which operators can compose to achieve their desired security posture. While the system is optimized around the full isolation model (VMs, agents, and workspaces), this is by no means required. Operators are free to use any subset that makes sense for their security and operational requirements.

Composition runs the other way too. Because agents are Linux users and workspaces are Linux groups, granting _partial_ access costs no more than withholding it, which makes graduated privilege between cooperating agents a practical everyday pattern rather than a special case. A research agent can be created with workspace access and nothing else, gather material, and leave artifacts behind for a more privileged agent to act on, so the privileged agent never crawls untrusted content itself. Models built on container-per-agent isolation can express the separation, but pay for the sharing in volumes, networking, or an orchestrator; here both halves are ordinary filesystem permissions.

A handoff like that narrows exposure rather than eliminating it. Whatever the low-privilege agent writes is still attacker-influenced input to whoever reads it next, so those artifacts are best treated as data to be evaluated, not as instructions to be followed.
```

`SECURITY_SECRETS` selects the second paragraph under `docs/why-agentworks.md` keypath
`# Why Agentworks > ## Key Principles > ### Declarative Configuration and Templates`:

```text
Environment variables and secrets are first-class in the configuration: env tables can be declared at vm, workspace, admin, agent, or session scope and merge in a defined precedence order. Secret references (`{ secret: name }`) resolve through a configurable backend chain (`env-var` reads from an `AW_SECRET_*` env var; `prompt` asks interactively at run time). Use `agw env show` to inspect the merged result for any context. See [cli/README.md](../cli/README.md#environment-variables-and-secrets) for the shape, and `agw resource describe-kind secret` for the full reference.
```

The renderer rewrites that one repository-relative link to
`https://github.com/WayfarerLabs/agentworks/blob/main/cli/README.md#environment-variables-and-secrets`.
No other relative link is accepted in extracted content.

`SECURITY_REPORTING` selects the first two paragraphs and following flat list under `SECURITY.md`
keypath `# Security Policy > ## Reporting a Vulnerability`:

```text
If you believe you have found a security vulnerability in Agentworks, please report it privately rather than opening a public issue.

Use GitHub's [private vulnerability reporting][gh-private] on this repository, or email the maintainer directly. Please include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce (or a proof-of-concept, if applicable).
- The version, commit, or branch you observed it on.
- Any relevant configuration (sanitized of secrets).
```

The generated page follows the selected source with the exact external link label
`GitHub private vulnerability reporting` and the exact permanent-policy link from section 2.

### 4.3 Reporting-link contract

`SECURITY.md` must contain exactly one reference definition named `gh-private`, and after whitespace
canonicalization its URL must be exactly:

```text
https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability
```

The selected reporting paragraph must contain exactly one
`[private vulnerability reporting][gh-private]` reference. The generated inline anchor and the
explicit `GitHub private vulnerability reporting` anchor both use the parsed reference URL. Missing,
duplicate, renamed, or changed definitions and label/definition drift are build errors. The builder
does not synthesize an email address or a repository advisory URL that the source does not provide.

## 5. Extraction, normalization, rendering, and errors

Inputs are decoded as strict UTF-8 after rejecting a byte-order mark. Convert CRLF and bare CR to LF
for matching. Heading matching strips only leading/trailing ASCII whitespace around the ATX heading
text; it does not case-fold or discard inline markup. A section ends at the next heading of equal or
higher level. Duplicate matching keypaths are errors.

ATX headings inside fenced code do not participate in keypaths. The scanner recognizes a fence
opened by up to three leading spaces followed by at least three matching backticks or tildes,
ignores every line through a closing fence that uses the same character and at least the opening
length, and fails on an unclosed fence. A heading-shaped shell comment inside a valid fence is
therefore content, not structure.

Within a selected section, the closed block grammar is:

- paragraph: consecutive nonblank, non-heading, non-list lines joined with one ASCII space;
- flat unordered list: hyphen-plus-space items whose indented continuation lines join with one ASCII
  space;
- blank line: exactly one separator between normalized blocks.

Strip trailing ASCII spaces per line and surrounding blank lines. Do not normalize punctuation,
Unicode, inline-code contents, or internal word spacing. Expected text in section 4 is already in
this normalized form. A selected sequence must occur exactly once and match whole normalized blocks,
not a substring. Text is rendered from the matched source blocks, never from the expected constant.

The closed inline grammar accepts plain text, `**strong**`, `_emphasis_`, backtick code spans,
inline HTTPS links, and the one reporting reference link. HTML, images, nested lists, blockquotes,
ordered lists, fenced code, autolinks, malformed or nested inline constructs, non-HTTPS extracted
links, and source reference definitions inside selected blocks are unsupported. Escape all plain
text, code text, attributes, and link labels with the standard library before emitting the fixed
`p`, `ul`, `li`, `strong`, `em`, `code`, and `a` elements. Extracted HTML is never evaluated.

Every contract failure occurs before staging or replacing output. The error is a single line that
starts `error:`, names the contract ID, source relative path, and heading keypath, and identifies
one of: missing/unreadable input, invalid UTF-8 or byte-order mark, missing/duplicate heading,
missing or duplicate expected block sequence, unsupported block or inline Markdown, invalid link,
missing or duplicate reference definition, reporting-link drift, or rendering invariant failure.
Template, base, manifest, staging, and output-ownership errors retain the same fail-closed behavior.
Every failure before the installed-manifest commit point leaves an existing output tree
byte-for-byte unchanged. Backup cleanup occurs after that point and follows section 9's warning
contract.

## 6. Closed template vocabulary

Tokens use the existing `{{UPPER_SNAKE_CASE}}` spelling. Each content token occurs exactly once in
its owning template; `SITE_BASE` occurs only in URL attributes and at least once per template. The
builder rejects a missing required use, a duplicate content-token use, a token in an unapproved
template, brace-like unknown text, or any token left after rendering.

| Template        | Complete allowed vocabulary                                            |
| --------------- | ---------------------------------------------------------------------- |
| `index.html`    | `SITE_BASE`, `HOME_META_DESCRIPTION`, `HOME_IDENTITY`, `HOME_PROBLEM`, |
|                 | `HOME_PRINCIPLES`                                                      |
| `security.html` | `SITE_BASE`, `SECURITY_META_DESCRIPTION`, `SECURITY_THREATS`,          |
|                 | `SECURITY_BOUNDARIES`, `SECURITY_POSTURE`, `SECURITY_SECRETS`,         |
|                 | `SECURITY_REPORTING`                                                   |
| `404.html`      | `SITE_BASE`                                                            |

The interim notice, headings, metadata, and destination labels are literal reviewed template text,
not general-purpose substitutions. There is deliberately no `BOOTSTRAP`, `ONBOARDING`, `COPY`,
conditional, include, loop, or arbitrary-key token.

## 7. Semantic documents and future insertion point

All pages have `lang="en"`, UTF-8 and viewport metadata, one descriptive title, one meta
description, one canonical link, a visible skip link, `header`, `main`, and `footer`, and exactly
one `h1`. Home uses title `Agentworks`, its canonical URL from section 2, and a plain-text,
attribute-escaped `HOME_META_DESCRIPTION` derived from only the first paragraph of `HOME_PROBLEM`.
Security uses title `Security | Agentworks`, its canonical URL from section 2, and
`SECURITY_META_DESCRIPTION` derived from the opening paragraph of `SECURITY_THREATS`. Metadata
transformation removes accepted inline Markdown delimiters while preserving their decoded text,
joins source lines with the section 5 paragraph rule, and performs no truncation or rewriting. These
metadata tokens are alternate renderings of the same selected source blocks, not independently owned
claims.

Each document also carries this exact meta-delivered content security policy:

```text
default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'
```

Inline style permission is limited to CSS because the accepted 404 controller writes dynamic lander
custom properties; scripts remain same-origin and connections remain disabled. The shared header
uses the linked AGW image with the accessible name `Agentworks home`; its small nav contains
ordinary anchors and no menu control.

The 404 keeps title `Page not found | Agentworks`, uses meta description
`The requested Agentworks page was not found.`, and has canonical URL
`https://agentworks.build/404.html`.

The home outline and source placement are exact:

```text
header: linked brand; GitHub and PyPI navigation
main
  h1: Agentworks
  identity: HOME_IDENTITY
  section#onboarding[aria-labelledby=onboarding-heading]
    h2#onboarding-heading: Guided onboarding
    p#onboarding-availability: exact interim notice
  section#problem
    h2: The problem space
    HOME_PROBLEM
  section#principles
    h2: Why it is built this way
    HOME_PRINCIPLES
  nav[aria-label="Explore Agentworks"]
    repository, package, rationale, and secondary exact security link
footer: repository identity and security link are ordinary anchors
```

`section#onboarding` is the future bootstrap insertion point. It is useful and nonempty now. The
later integration keeps the section and heading relationship, deletes `p#onboarding-availability`,
and supplies the reviewed canonical subtree. No hidden node, comment, token, CSS selector named for
bootstrap, fixed height, or reserved copy-control location exists in the interim artifact.

The security outline is exact:

```text
header: linked brand; Home, GitHub, and PyPI navigation
main
  h1: Security at Agentworks
  section#threat-model > h2: Threat model; SECURITY_THREATS
  section#boundaries > h2: Boundaries and current limitations; SECURITY_BOUNDARIES
  section#operator-posture > h2: Operator posture; SECURITY_POSTURE
  section#credentials > h2: Credentials and secrets; SECURITY_SECRETS
  section#reporting > h2: Report a vulnerability; SECURITY_REPORTING and reporting links
footer: Return to agentworks.build
```

The 404 retains `brand-and-lander-lld.md` section 4's outline, hooks, fallback, names, and focus
contract. Adding shared CSS and metadata must not reorder or nest its error and game landmarks.

## 8. Responsive workbench visual system

`site.css` owns the shared single light theme. Exact color tokens are:

| Token           | Value     | Use                                                         |
| --------------- | --------- | ----------------------------------------------------------- |
| `--canvas`      | `#f5f2e8` | Page and scene sky                                          |
| `--panel`       | `#ebe7dc` | Bounded content regions                                     |
| `--ink`         | `#292b30` | Body text and recognizable underlined links                 |
| `--ink-muted`   | `#4b4e55` | Secondary text, borders on canvas                           |
| `--line-subtle` | `#8a867c` | Non-text dividers and decorative details only               |
| `--accent`      | `#d94a1e` | Focus, rules, and non-text emphasis, never normal-size text |
| `--hot`         | `#ffe09a` | Existing plume core on graphite only                        |
| `--status`      | `#7de2c5` | Existing powered NOC indicators on `#20232a` only           |

Pinned computed contrasts include ink/canvas `12.646:1`, muted/canvas `7.440:1`, subtle/canvas
`3.243:1`, accent/canvas `3.789:1`, ink/panel `11.464:1`, hot/ink `11.049:1`, and status/NOC
`10.153:1`. Necessary boundaries and the three-pixel focus outline meet 3:1; all normal text meets
4.5:1. Links remain ink-colored, underlined, and distinguishable without color.

Body copy uses `system-ui, sans-serif`. Status labels, eyebrow text, compact nav details, and inline
code use `ui-monospace, "Cascadia Code", "Liberation Mono", Menlo, monospace`. The layout has no
window controls, prompt glyphs, CRT treatment, green-on-black palette, decorative command text, or
keyboard-only affordance. Terminal influence comes only from crisp one-pixel panels, short uppercase
labels with `0.08em` tracking, aligned metadata, compact density, and monospace accents.

The content frame is `width: min(100%, 60rem)` with `margin-inline: auto` and page padding
`clamp(1rem, 4vw, 3rem)`. Paragraph measure is at most `68ch`. At `min-width: 48rem`, the identity
and exploration regions may use a two-column grid with a minimum track of zero; every other content
flow remains readable in source order. Below 48rem all regions are one column. Panels use fluid
padding `clamp(1rem, 3vw, 2rem)` and spacing, never fixed heights.

Global `box-sizing: border-box`, `min-width: 0` on grid/flex children, wrapping nav,
`overflow-wrap: anywhere` for literal URLs, responsive SVGs, and no fixed content width prevent
page-level horizontal scrolling at 320 CSS pixels and 400 percent zoom. The lander keeps its pinned
25:16 scene and 44 CSS pixel start target. No horizontal content scroller is introduced. Motion
remains limited to the accepted 404 and is suppressed per its reduced-motion contract; home and
security are motion-free.

## 9. Builder CLI, output, and site-base transition

The full build command is:

```text
python3 website/build.py --repo-root ROOT --output OUT --site-base BASE
```

The accepted focused seam remains:

```text
python3 website/build.py --only 404 --repo-root ROOT --output OUT --site-base BASE
```

All three paths are required. `--only` has the sole allowed value `404`; omitting it means the full
site. The existing slash-bounded ASCII site-base grammar remains unchanged. `/` is used for local
and custom-domain builds. `/agentworks/` is used at the default project Pages URL. In the Pages
workflow, the current `configure-pages` base-path output is normalized to this grammar and passed to
the same CLI, so attaching the custom domain changes the deployment input to `/` without a source
template, release flag, or alternate artifact path. An empty, absolute, URL-like, or unbounded base
fails.

The full output tree is exactly:

```text
OUT/
  404.html
  index.html
  assets/agw-rocket.svg
  security/index.html
  static/lander-game.js
  static/lander-model.js
  static/lander.css
  static/site.css
```

The focused build extends Phase 2's artifact with the shared stylesheet and has this exact six-file
tree: `404.html`, `assets/agw-rocket.svg`, `static/lander-game.js`, `static/lander-model.js`,
`static/lander.css`, and `static/site.css`. The full manifest is explicit in `build.py`; recursive
source copying is forbidden. Render into a fresh sibling temporary directory, verify exact
regular-file paths, and reject symlinks and special entries before touching the destination. Replace
only an absent output or an existing real directory whose entries are a subset of the selected
manifest. Continue rejecting output at or beneath the repository root.

Replacement uses a sibling backup as a commit protocol. When output exists, rename it to a fresh
backup, rename the verified staging directory to output, verify the installed manifest, and only
then delete the backup. If staging installation or installed-manifest verification fails, remove an
incomplete new output if present and rename the untouched backup back to the original path before
reporting the error. Failures before the first rename leave output untouched. Tests inject failure
at each rename and verification boundary and assert exact restoration. Installed-manifest
verification is the commit point. Backup cleanup failure after that point leaves the installed
output valid, exits successfully, and emits one `warning:` line naming the retained sibling backup;
it is not recast as a failed build.

No source write, output escape, inherited unknown file, timestamp, commit ID, environment-dependent
prose, or nondeterministic ordering enters output bytes. For the same input bytes and arguments,
every generated and copied file is byte-identical.

Every local URL in HTML is `SITE_BASE` plus a root-relative artifact path without a leading slash.
Root navigation is exactly `SITE_BASE`; security navigation is `SITE_BASE + "security/"`. Canonical
and approved external anchors are absolute HTTPS URLs and are not base-prefixed. Full build
verification resolves every local reference against the output manifest for both `/` and
`/agentworks/`.

## 10. Interim verification matrix

| Layer                              | Required automated or manual evidence                                                                                                                                                                                                                                                                                                                                     |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Source selection                   | Every section 4 happy path; missing/duplicate/reordered headings; heading-shaped fenced-code canary and unclosed fence; expected passage missing/duplicated/drifted; CRLF normalization; invalid UTF-8 and byte-order mark; unsupported Markdown; escaping of `<`, `>`, `&`, quotes, and code; source reordering outside a selected section does not fail.                |
| Reporting                          | Exact `gh-private` definition and selected reference; missing, duplicate, renamed, non-HTTPS, or changed URL fails; both generated reporting anchors use the parsed URL; permanent policy link is exact.                                                                                                                                                                  |
| Template contract                  | Per-template closed token sets and counts; unknown, missing, duplicate, wrong-template, brace-like, and unexpanded tokens fail; extracted fragments are escaped and cannot create script, style, event-handler, or template markup.                                                                                                                                       |
| Builder and manifest               | Full and focused CLI shapes; root and `/agentworks/` bases; invalid bases; clean deterministic builds; exact trees; safe replacement; unknown file, symlink, and special-entry refusal; output and staging remain outside the repository; injected rename/verification failures restore exact existing output; no Git status residue.                                     |
| Interim guards                     | Exact availability notice exists once in ordinary markup; `#onboarding` is nonempty; no `pre` in home, copy/clipboard selector or script, bootstrap token/comment, disabled control, `uv tool install`, `pipx install`, `git clone`, `agw config init`, or alternative release mode; home and security contain no script, while 404 contains only its same-origin module. |
| DOM and links                      | HTML language, titles, descriptions, canonicals, skip links, landmarks, one `h1`, nested heading order, named navs, duplicate IDs, useful labels, exact URLs, secondary security link, reporting links, 404 fallback, and all local references at both bases. Run assertions against parsed generated HTML, not regex alone.                                              |
| Runtime and privacy                | No remote CSS/font/image/script, analytics, form, cookie, storage, service worker, fetch, XHR, WebSocket, EventSource, beacon, or client routing. External anchors opened in a new context, if any, use `rel="noopener noreferrer"`; same-context links need no `target`.                                                                                                 |
| CSS and accessibility              | Token values and computed contrast; three-pixel visible focus; underline independent of color; source/logical tab order; 44 CSS pixel lander target; no required motion; reduced-motion 404 behavior; terminal/TUI cues present without fake-terminal signatures.                                                                                                         |
| Responsive manual                  | Chromium, Firefox, and WebKit at 320 CSS pixels, 400 percent zoom, touch landscape, and wide desktop: no page overflow, clipped text/nav, overlap, fixed-height loss, covered home link, or terminal-familiarity dependency.                                                                                                                                              |
| Assistive and clean-context manual | Screen-reader landmarks, headings, links, 404 status/focus, and hidden game controls. A newcomer identifies the product and interim availability and chooses repository, package, rationale, or security without explanation; record timing and intervention.                                                                                                             |

Run existing Node and Python lander suites unchanged, the new standard-library shell suite, and
`./scripts/lint-files.sh --fix`. The final check builds to a fresh temporary directory and confirms
the repository status contains no generated artifact.

## 11. Traceability and handoff

| Requirement or decision                                | Pinned by                                       |
| ------------------------------------------------------ | ----------------------------------------------- |
| R10, C5, D10: honest removable interim state           | Sections 1, 3, 7, 9, and 10                     |
| R11, D1/D3: sourced security depth and reporting       | Sections 2-7 and 10                             |
| R13, D5: restrained terminal/TUI-derived language      | Section 8 and the CSS/manual rows in section 10 |
| AC9/AC14: no substitute onboarding and useful choices  | Sections 3, 7, and 10                           |
| AC11: stable optional security path and sourced claims | Sections 2-7 and 10                             |
| AC13: semantic, responsive, recognizable shared shell  | Sections 7, 8, and 10                           |

Phase 4 implements this interim contract only. Phase 7 must re-read merged onboarding sources and
write its own integration LLD before removing the notice. Permanent source, tests, and runbook must
stand on their own and must not link back to this temporary SDD.
