# HLA: The agentworks.build Website

- Status: Interim implementation complete; release acceptance in progress
- Date: 2026-08-07
- Last revised: 2026-08-09
- FRD: `frd.md`
- Research: `prior-art-research.md`
- Brand direction: `brand-direction.md`

## Architectural summary

Build three semantic static pages from repository-owned inputs and deploy their artifact to GitHub
Pages on every push to `main`. Site sources live in `website/`; generated output does not. A small
standard-library Python builder performs explicit substitutions, escapes shared text for HTML, emits
the finished artifact, and fails when a content contract required by the current release is
unavailable or ambiguous.

Delivery has two honest stages over the same URLs and architecture. The interim release publishes
the complete home and security shells, repository-derived product/security passages, stable links,
selected brand, custom 404, pipeline, and domain while canonical onboarding is unavailable. It
contains a small semantic availability notice and no bootstrap-shaped substitute. After onboarding
Phase 3 lands on `main`, a second release replaces that notice with the canonical bootstrap and its
copy enhancement. This is a delivery sequence, not a runtime mode or parallel site.

The content pages use HTML and CSS for their full experience. A small local script adds the
nonessential custom-404 lander game. The later onboarding release adds focused copy-button behavior,
while its bootstrap and all error content remain useful when scripts are absent. The visual system
includes local SVG logo assets. There are no remote fonts, scripts, images, runtime APIs, analytics,
cookies, accounts, forms, or backend.

GitHub Actions builds and checks the same artifact in pull requests and on `main`. A dedicated Pages
workflow uploads that artifact and deploys it through the protected `github-pages` environment.
GitHub Pages serves `agentworks.build` over HTTPS; `www.agentworks.build` redirects to the apex.

## Decisions

### D1. One compact landing page plus optional security depth

The completed home page has this information order:

1. the selected AGW rocket as a prominent hero with a compact product identity;
2. the agent-addressed bootstrap as the dominant action;
3. one direct link each to GitHub, PyPI, deeper rationale, and security.

The interim release preserves that structure but replaces item 2 with a concise, ordinary-text
notice that guided onboarding is not yet published. It does not render an empty bootstrap region,
disabled copy button, speculative command, wait-list form, countdown, or generic "coming soon"
marketing panel. The notice is removed when the real bootstrap replaces it.

The landing page does not render the longer problem statement or principles. Those passages render
on the generated Manifesto page from their permanent repository source. GitHub and PyPI appear once
in the shared header; Manifesto and Security appear once in the shared footer. The body does not
repeat those destinations under different labels.

The home page gives the security posture one calm, visually secondary link labeled
`We take security seriously.` That link opens a dedicated static security page with this order:

1. a plain statement of the threat model and why isolation matters;
2. the actual VM, Linux-user, workspace, and operator-control boundaries;
3. candid current limitations and credential/secret considerations;
4. practical operator posture and the private vulnerability-reporting path.

The Manifesto and security pages are optional depth, not modals, warning gates, prerequisites, or
long pitches on the home page. They and the host-required 404 are the only separate pages in the
first slice; the primary product experience stays on the compact landing page. Pages have in-page
navigation only if their final length makes it useful. A custom `404.html` is an error surface, not
a content page or client-side route. There is no blog, documentation hierarchy, release feed,
search, or client-side routing. Growth-path content gets its own design when its authoritative
contracts have landed.

Home, Manifesto, Security, and 404 share one landmark shape. A breadcrumb sits at the upper left:
one linked `Agentworks` home crumb, a visual separator hidden from the accessibility tree, and a
non-linked current item marked with `aria-current="page"`. The current item is `Home`, `Manifesto`,
`Security`, or `404` as appropriate. Every page except Home places the small selected rocket
immediately before the breadcrumb. The 404's linked `Agentworks` crumb replaces the body-level
return-home action. One GitHub and one PyPI call to action sit at the upper right with visible
labels and local decorative icons hidden from the accessibility tree. The home hero remains the only
logo on Home.

The shared footer contains `Product of Wayfarer Labs, LLC` at the left and the only manifesto and
security links at the right, labeled `Agentworks Manifesto` and `We take security seriously`. The
header and footer wrap in source order rather than collapsing behind a menu. This is consistent
navigation across a tiny static site, not a new navigation system.

### D1A. The Manifesto is a generated site page

`/manifesto/` renders the long-form introduction, complete problem space, and complete key
principles from `docs/why-agentworks.md`. The builder selects the reviewed canonical source
structure by heading path and passes it through the same closed Markdown transform used by other
repository-derived content. Source-relative links are mapped by an explicit allowlist to permanent
repository URLs; no generic relative-URL rewriting or second prose copy is introduced. The page owns
only its `Agentworks Manifesto` presentation title, metadata, breadcrumb label, and connective
shell. If the permanent document adopts that title, the generated page follows it without a second
rename mechanism.

### D2. Plain web technologies with a narrow build step

The checked-in source consists of home, Manifesto, security, and 404 HTML templates, local CSS,
focused progressive-enhancement JavaScript, SVG assets, and a standard-library Python builder under
`website/`. The 404 template references stable same-origin groups in the selected SVG rather than
duplicating its paths. The builder substitutes a validated site base into the 404's home and local
asset URLs, allowing the same source to run at the local/custom-domain root and at the pre-DNS
GitHub Pages project path. There is no separate build-metadata abstraction. The builder's explicit
input list is the build manifest. It uses the supported Python runtime already present in this
repository and only the standard library. It does not introduce Node package metadata, a JavaScript
framework, Jekyll, or a general template language.

The builder accepts an explicit repository root and output directory, writes only beneath the output
directory, and produces deterministic bytes for the same inputs. It starts from an empty
caller-provided output directory in CI. Generated artifacts are ignored by Git and never committed.

The template vocabulary is closed to named placeholders owned by the builder. Shared text is HTML
escaped before insertion. There is no evaluation of source content as a template, Markdown, Python,
or JavaScript.

### D3. Repository content is a checked contract

The site has three content classes:

- **Canonical shared content.** After onboarding Phase 3 lands on `main`, the build reads its
  canonical bootstrap body directly. The built code element's decoded text, the canonical source,
  and the README's generated fenced block must be byte-identical. The build fails closed on missing,
  duplicate, malformed, or drifted sources. The website does not own or rewrite this text.
- **Repository-derived product and security content.** The problem, principle, threat-model,
  boundary, limitation, and operator-posture passages are selected by unique Markdown headings and
  exact expected text from permanent repository docs, never by paragraph position. The matching
  passages are normalized by a closed transform, HTML escaped, and generated into the relevant page.
  `SECURITY.md` remains the authority for private vulnerability reporting. A template cannot supply
  alternate product or security claims. Missing or duplicate headings, absent or duplicate expected
  text, unsupported Markdown, or reporting-link drift fail the build. Unrelated insertion or
  reordering does not. Links point to permanent repository docs, never to this SDD.
- **Site-owned connective content.** The website owns only presentation-neutral labels and
  instructions such as navigation, link introductions, the operator-approved security-link label,
  the interim onboarding-availability notice and, after integration, "Copy" and copy-status
  feedback. It does not make claims about Agentworks behavior, guarantees, principles, installation,
  security properties, or requirements.

The shell LLD pins the permanent paragraph text and its extraction contract against current `main`.
The later onboarding-integration LLD pins the exact upstream bootstrap path, extraction contract,
and README fence semantics after onboarding Phase 3 is merged. README fence-body byte equality is an
assumption until that pickup proves the merged onboarding contract. A branch-only path is not an
architecture input. Shell implementation and publication do not wait for onboarding; bootstrap
integration does.

That pickup also verifies the canonical disclosure's meaning before the website publishes it: the
agent must run on the intended workstation and needs full file inspection and command execution
access with the permissions of the workstation account running the harness. Root is not implicit;
privilege elevation remains separate and explicit. The recommended strict posture governs approval
and visibility without preventing the access onboarding needs. These are upstream onboarding
requirements, not prose for the website to reconstruct. If the merged canonical source does not
establish them cleanly, integration stops and coordinates the gap with the onboarding owner.

### D4. HTML is the agent surface too

The same documents serve humans, agents, assistive technology, text browsers, and indexing tools.
The built pages provide:

- one descriptive `title`, meta description, canonical URL, and ordinary indexable markup;
- semantic landmarks and one correctly nested heading outline;
- real anchor and button elements with accessible names;
- in the interim release, an ordinary-text onboarding-availability notice and no bootstrap or copy
  control;
- after onboarding integration, the complete bootstrap in a `pre`/`code` region, without image-baked
  or CSS-generated text;
- useful link text that identifies GitHub, PyPI, rationale, and security destinations;
- no essential state hidden behind interaction, animation, canvas, or client rendering.

No `llms.txt` or second agent-only representation is introduced. It would be another content copy
without a settled standard or first-slice need.

### D5. Visual system is local, restrained, and terminal-aware

The first slice uses system fonts, a small local color/token layer, and the selected custom AGW
rocket mark. The mark stacks custom symmetric A, G, and W geometry into a rocket silhouette; the G
has four equal corner radii, with only its opening and inward stroke breaking O-like symmetry. The
ordinary mark is neutral graphite. Its original twin-flame treatment uses compact pale-yellow hot
cores within orange and deeper orange-red plumes. On the landing page the mark is a dominant hero
element at two to three times the original small-header presentation, while the Manifesto, security,
and 404 contexts retain the shared compact size. The final checked-in SVG is self-contained,
font-independent, semantic where displayed as content, and reusable without this SDD.

The presentation should feel like a capable workbench rather than a generic SaaS landing page:
simple but powerful, with strong typography, restrained color, visible structure, and efficient
density. Terminal and TUI paradigms appear through monospaced accents, crisp panel boundaries,
compact status-like labels, and deliberate alignment. They do not appear as a fake window frame,
wall of command prompts, green-on-black theme, CRT effect, decorative ASCII text, or keyboard-only
interaction. The interim notice occupies the future bootstrap region without mimicking a code block;
after integration, the bootstrap becomes the visual center without requiring a layout redesign. No
remote font, icon library, or existing architecture diagram is introduced.

The shell LLD pins final tokens and layouts across the home, Manifesto, security, and 404 surfaces
with these invariants:

- useful at 320 CSS pixels and at 400 percent zoom without page-level horizontal scrolling;
- WCAG 2.2 AA text, component, focus, and interaction contrast;
- visible keyboard focus and a logical tab order;
- no required motion, and any decorative motion disabled under `prefers-reduced-motion`;
- light and dark presentation only if both can be tested to the same bar. A single excellent theme
  is preferred to two partially verified ones.

### D6. Copy is progressive enhancement

The interim release ships no copy script or dormant copy control. After onboarding integration, the
bootstrap text is selectable and readable before JavaScript runs. The copy button reads the code
element's `textContent`, invokes the Clipboard API only from the user's activation, and reports
success or failure in an `aria-live` status region without moving focus. If the API is unavailable,
the button is absent or explains that manual selection remains available. No clipboard content is
read.

### D7. The custom 404 hides a bounded deployment game

The built artifact includes a semantic `404.html` that identifies the missing page and exposes a
normal link home without CSS or JavaScript. The selected twin-plume mark hovers over a minimal lunar
surface. No visual instructions, score, or game chrome appear initially. On arrival, the plumes run
a subtle cue for less than five seconds and settle; `prefers-reduced-motion: reduce` suppresses that
cue entirely. This bounded cue preserves the surprise without requiring a pre-game pause control.

An unmodified, non-repeated Space key starts the game from the initial 404 state when its event
target is the document body or lander scene, never when focus is on the home link or another
interactive/editable element. The lander is also an operable, accessibly named start control without
visible instruction text. Activating it provides the pointer and assistive-technology path. The
accepted preflight Space event is consumed so it cannot also scroll the page. Starting moves focus
to the game scene and reveals concise controls and status.

While active, Space or Up commands equal thrust; Left or `h` increases the right engine to turn
left; Right or `l` increases the left engine to turn right. Apart from the accepted preflight Space
event, only active game controls prevent their ordinary browser behavior. A first pointer activation
starts without also applying thrust. Thereafter, pointer down captures that pointer and starts
collective thrust; movement applies a bounded horizontal differential after a dead zone; pointer up,
cancel, lost capture, exit, or page hide cuts both engines and releases capture. A short press with
little travel receives a pinned minimum impulse so a tap is useful; holding sustains thrust.
Dragging left biases the right engine, and dragging right biases the left. `touch-action` and scroll
suppression apply only inside the active game scene. Escape exits to the settled initial state, and
`r` restarts a completed or failed mission.

The game is a small DOM/SVG state machine, not canvas and not a general engine. A timestamp-driven
animation loop integrates a fixed-step two-dimensional model with bounded catch-up: gravity,
position, velocity, attitude, angular velocity, collective thrust, differential torque, fuel, and
landing contact. The loop pauses when the document is hidden and resumes without accumulating hidden
time. Within the pinned catch-up bound, the same timestamped input timeline produces equivalent
fixed-step simulation across representative 30, 60, and 120 Hz frame schedules. A larger stall
discards accumulated wall time and resumes from the last state rather than pretending to simulate
unseen play. Each plume group scales independently from the commanded engine thrust.

A valid landing requires contact within the marked zone, left of a small dark NOC cluster, under
pinned speed and attitude limits. After touchdown, the G opening acts as a deployment bay: a small
terminal-inspired agent reaches the surface and enters the NOC. Its windows and server-status lights
illuminate in sequence, followed by a restrained antenna signal. The powered appearance remains for
the rest of the in-memory run while the lander lifts away. The accessible status then becomes
exactly `Agent deployed. Mission continues.` Reduced motion skips the decorative travel and
departure and presents the powered NOC and status directly. An unsafe contact produces a restrained
failure state without flashing, sound, debris, or page damage. The home link remains available in
every state.

Game constants, DOM states, collision rules, agent-deployment geometry, and test vectors belong in
`brand-and-lander-lld.md`. The game makes no product claim and carries no analytics, storage,
network request, randomness that affects acceptance, or critical content. Restart or reload returns
the NOC to its initial dark state; there is no cross-run persistence or level system.

### D8. GitHub Pages is the replaceable delivery adapter

The existing CI workflow builds and tests the site on pull requests and pushes to `main`, without
Pages permissions. Before the implementation PR merges, the operator enables GitHub Actions as this
repository's Pages source and restricts the `github-pages` environment to the default branch. The
publishing workflow then runs only after a push to `main` and has two jobs:

1. **Build:** check out the repository, run the website contract tests, configure Pages metadata,
   run the builder with the normalized Pages base, verify the artifact, and upload exactly the
   generated directory.
2. **Deploy:** on `main` only, deploy the uploaded artifact to the `github-pages` environment with
   the minimum `pages: write` and `id-token: write` permissions.

The publishing build repeats the deterministic checks before it uploads, so deployment never trusts
an artifact from a different event. Deployment concurrency allows one active publish and cancels
superseded queued work without cancelling an in-progress production deployment.

The publishing workflow is not path-filtered. The site's authoritative inputs include files outside
`website/`, so every push to `main` must rebuild and verify the artifact rather than risk serving a
stale bootstrap or product passage.

The workflow pins current stable major versions of official GitHub actions at implementation time,
consistent with repository conventions. No third-party deploy action or long-lived cloud credential
is introduced.

The artifact boundary is portable: another static host can accept the generated directory without
changing content or build contracts. Hosting-specific behavior stays in the workflow and operator
runbook.

The workflow first goes live with the interim release. The onboarding integration later uses the
same merge-to-`main` path; it neither adds a second Pages project nor changes domain configuration.
This exercises the delivery system and custom 404 before the upstream content dependency is ready,
while keeping every deployed artifact reproducible from its source commit.

### D9. DNS and domain setup are explicit one-time operations

The permanent `website/README.md` runbook records external setup, current DNS inventory, approval,
rollback, and recovery. Setup and go-live are ordered so deployment exists before DNS cutover:

1. enable GitHub Actions as this repository's Pages source and protect the `github-pages`
   environment so only `main` can deploy;
2. merge the implementation and verify its automatic deployment at the default Pages URL;
3. verify `agentworks.build` in the WayfarerLabs GitHub organization and retain GitHub's TXT record;
4. set `agentworks.build` as this repository's custom domain;
5. after explicit operator approval, replace GoDaddy's apex parking record with GitHub's documented
   `A` records and point `www` by `CNAME` to `wayfarerlabs.github.io`;
6. verify DNS answers, apex content, `www` redirect, certificate, and HTTPS enforcement;
7. remove or repoint the DNS records promptly if Pages is ever disabled.

DNS values are copied from current GitHub documentation during go-live and recorded in acceptance
evidence. They are not hidden in application code. No wildcard record is created.

The DNS cutover belongs to the interim release, after that artifact passes acceptance at the default
Pages URL. The onboarding release is then an ordinary site-source deployment. Production closeout
waits until the canonical bootstrap is live and AC3/AC4 are accepted; the existence of a healthy
interim domain does not lock the effort early.

### D10. The interim-to-complete transition is explicit and disposable

The checked-in main-page template initially contains one bounded interim notice at the future
bootstrap position. The builder requires and emits that notice in the interim release, but has no
`preview` flag, environment-dependent content, hidden complete template, or optional branch that
pretends to consume onboarding. Tests assert that no bootstrap code region, copy affordance,
installation instruction, or bootstrap JavaScript is present.

Once the canonical onboarding contract is merged, the onboarding integration deletes the notice,
adds the real source as a required builder input, emits the verified `pre`/`code` region, and adds
the focused copy enhancement. Tests then invert the contract: the interim notice is forbidden and
bootstrap identity is mandatory. This keeps temporary behavior obvious and removable instead of
turning a two-step delivery need into permanent configuration machinery.

## Component topology

```text
README + docs/why-agentworks.md selectors --+--> home page
interim availability notice ---------------+

docs/why-agentworks.md security selectors --+--> security page
SECURITY.md reporting contract -------------+

404 template + logo/game assets ----------------> 404 page

home + security + 404 --> deterministic builder --> PR/CI
                                              |
                                              +--> GitHub Pages --> agentworks.build

After onboarding Phase 3 merges:

canonical bootstrap + README fenced block
                  --> identity and disclosure checks
                  --> generated bootstrap region (replaces interim notice)
```

## Source layout

The detailed filenames belong in the shell and onboarding-integration LLDs, but responsibilities are
fixed here:

- `website/`: home, Manifesto, security, and 404 source; final SVG assets; focused CSS/JavaScript;
  builder; tests; and permanent operator/developer runbook.
- `.github/workflows/`: Pages build/deploy workflow and the existing CI integration.
- `.gitignore`: generated site artifact exclusion.
- repository README and onboarding canonical source: inputs only after onboarding integration, not
  website-owned copies.
- `docs/why-agentworks.md`: permanent product/security rationale linked and checked as a claim
  source.
- `SECURITY.md`: permanent private vulnerability-reporting authority linked and contract-checked by
  the security page.
- this feature directory: temporary design, plan, research, and acceptance evidence only.

## Verification strategy

### Build and content contracts

- deterministic clean build in a temporary directory;
- no unexpanded placeholders or writes outside the requested output;
- unique source headings and paragraph selectors, closed normalization, and required product links;
- required security sections, actual-boundary/limitation content, stable security URL, and private
  vulnerability-reporting link;
- valid internal paths, canonical URL, metadata, and no external runtime assets;
- generated output absent from Git status.

Interim tests additionally reject bootstrap markup, copy controls/scripts, installation text, and
missing availability notice. After onboarding integration, tests reject the interim notice and
require exact bootstrap equality across canonical source, README fenced block, and decoded built
HTML, and prove that the canonical disclosure covers intended-workstation placement, full
workstation-account file/command access without implicit root, and the strict-posture
recommendation.

### Document behavior

- semantic landmarks, heading order, language, named controls, and the release-appropriate
  onboarding region asserted;
- the home security link remains visually secondary but programmatically clear, and the security
  page remains useful without script or terminal familiarity;
- after onboarding integration, copy behavior exercised for success, unavailable API, and rejected
  write without changing the source text;
- custom 404 fallback, initial hidden controls, bounded idle cue, keyboard/vi/pointer mappings,
  deterministic physics vectors, plume-to-thrust mapping, success/failure/restart/exit states,
  background pause, and agent-deployment completion asserted;
- keyboard-only traversal, visible focus, narrow-width reflow, zoom, reduced motion, and screen
  reader landmarks checked in acceptance;
- color tokens verified with computed contrast evidence for normal text, large text, components, and
  focus indicators.
- terminal/TUI cues remain present across all surfaces without fake-terminal chrome, inaccessible
  density, or loss of recognizable links and controls.

### Deployment and production

- pull request proves build without deploy permissions;
- a merge-to-`main` source change produces a successful Pages deployment with the expected commit;
- production returns the built content over HTTPS at the apex;
- `www` redirects to the apex without a certificate warning;
- DNS A, AAAA, CNAME, MX, TXT, and CAA answers match the recorded before-state plus approved cutover
  delta;
- GitHub and PyPI links resolve. Interim production contains the availability notice and no
  bootstrap affordance; complete production contains the byte-identical bootstrap and no interim
  notice.

## Security and privacy

- The site adds no first-party telemetry, user storage, analytics, cookies, forms, third-party
  script, remote font, or runtime network request. GitHub retains its ordinary hosting and
  operational processing.
- Shared text is escaped before HTML insertion. The builder never evaluates it.
- Game state is ephemeral in memory and accepts no text, URL, storage, or network input. Pointer and
  keyboard handlers are scoped to the active scene and released on exit or page lifecycle changes.
- A restrictive document policy is expressed where GitHub Pages allows it without breaking the page;
  lack of configurable response headers is documented as a Pages constraint, not simulated as a
  guarantee.
- The deployment workflow grants read-only contents plus job-local Pages/OIDC permissions and uses
  no repository or cloud secret.
- Organization domain verification and no-wildcard DNS reduce custom-domain takeover risk.
- External links do not receive opener authority if a new browsing context is used.

## Failure and recovery

- **Upstream content unavailable:** the interim release remains publishable without inspecting an
  onboarding branch. Bootstrap integration waits for the canonical contract on `main`; it never
  guesses.
- **Upstream content drift after integration:** build fails before artifact upload and names the
  missing or mismatched contract. The owner updates the website integration against the merged
  source; it never substitutes local copy.
- **JavaScript unavailable or game failure:** the semantic 404 message and ordinary home link remain
  available. The lander is nonessential and never owns navigation or recovery.
- **Bad site merge:** the Pages environment exposes deployment history. Fix forward or redeploy the
  last known-good artifact according to the runbook.
- **DNS or certificate delay:** the Pages deployment history, status, and captured pre-attachment
  default-URL acceptance remain the diagnostics. Do not weaken HTTPS or add alternate forwarding
  machinery while propagation is incomplete.
- **Pages disabled or repository moved:** remove the public DNS records immediately if the domain is
  no longer attached, then follow the runbook to restore verified ownership before repointing.
- **Hosting migration:** deploy the same generated artifact elsewhere, validate it at a temporary
  hostname, then move DNS. Site sources and content contracts stay unchanged.

## Deliberately deferred growth paths

- Web rendering of `agw guide` topics.
- Schema-derived reference pages.
- Release notes, changelog presentation, search, and multiple-page navigation.
- Analytics, feedback capture, accounts, forms, APIs, and dynamic backends.
- A general component system, design system package, or static-site framework.

Each requires a new requirement or a stable upstream contract. None is scaffolded in this slice.
