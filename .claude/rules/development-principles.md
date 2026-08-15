# Development Principles

The task, plan, or SDD you are working from says _what_ to build; the other always-on rules cover
the mechanics (style, commit format, linting). This rule covers the layer between: _how_ to develop.
It applies to everyone producing changes here, lead or subagent, human or agent; the
`agentworks-dev` persona embodies it for delegated implementation, and the `agentworks-reviewer`
enforces it, but we all must follow it.

One frame sits behind all of them: **main is a pattern book**. Everything that lands on main will be
read by someone, human or agent, trying to infer how we do things here, and they will copy what they
find. Write every change as if it will be the example someone learns from, because it will be.

The principles fall into three buckets: how the code is shaped (Design), what you leave behind on
main (What lands), and how you conduct the work (How you work). The numbering runs straight through
so a principle can be cited by number alone.

## Design

### 1. Don't accept bad complexity

Complexity is the number one cost of software over time. It is what makes things hard to understand,
hard to change, and hard to maintain. Any increase in complexity should be viewed with suspicion. Is
it really necessary? Is there a simpler way to achieve the same goal? Can we adjust the requirements
or the design to avoid or reduce it?

But of course, some complexity is unavoidable. Complexity is what makes software useful. The key is
to differentiate between good, necessary complexity and bad, unnecessary complexity. Like most
things that matter, the difference is in the details.

Good complexity is complexity that is fundamentally necessary to meet the requirements of the
system. It has a number of characteristics:

1. It represents reality. The first principle of good software design is that the software should
   model the real world as closely as possible. Not only does this get you the best system now, it
   is also the best possible hedge against future changes.
2. It is as general as possible while still meeting the requirements. Don't solve a more specific
   problem when a more general solution is possible. This is about the shape of the design, not
   about building ahead of need: the right general shape is usually simpler than the special case it
   replaces, while mechanism nothing consumes yet is speculative generality and principle 4's
   territory.
3. It is still simple on the micro scale. The individual components are fundamentally simple, with
   good data models, clear responsibilities, well-defined APIs that match their responsibilities,
   and a clearly-defined lifecycle. The complexity (and usefulness) emerges from the way these
   simple components are composed together.

Bad complexity, on the other hand, is complexity that is either unnecessary or ill-suited to the
problem at hand. In addition to being the opposite of the characteristics above, there are specific
things to watch out for:

1. There are a lot of exceptions. A properly-designed system rarely needs them. If you find yourself
   writing a lot of special-case code, it's a sign that the design is wrong. More than likely,
   you're not modeling the underlying reality correctly, and you should take a step back and
   re-evaluate your design.
2. It is brittle. If a small change in one part of the system causes a cascade of changes in other
   parts, it's a sign that the design is wrong. The components should be as independent as possible,
   and changes should be localized to the smallest possible area.

We simply don't accept bad complexity into our codebase. If you find it, fix it. If you can't fix
it, escalate it to whoever is driving your work rather than living with it silently.

### 2. Names tell the truth

If a method only does bookkeeping, call it `mark_realized`, not `realize`. If two APIs are used
differently, make them look different. A name that over-promises, or blurs a distinction the design
depends on, is a bug you ship to every future reader. Getting a name right is cheap at write time
and expensive forever after, so spend the thought now, and when you find an existing name that lies,
fix it (see principle 9).

### 3. Enforce invariants; don't just document them

If the design promises "these fields always match the level," then the object enforces it (in
`__post_init__`, a validator, a DB constraint) or a test proves it. A promise that lives only in
prose is not a promise; it is a hope. Prose explains _why_ the invariant exists; code enforces
_that_ it holds. Comments do not count as enforcement.

The invariant is the behavior, never the sentence that describes it. Enforcing "the disclosure
explains that elevation is separate" by asserting that wording appears in the output tests our
writing, not our system; enforce the boundary the sentence describes instead, and let review own the
words. See the `no-prose-policing-tests` rule.

"Or a test proves it" is not a license to test everything. A test earns its place by guarding an
invariant that can actually regress; a test that can only fail when someone edits the thing it
restates is cost, not coverage. Assert behavior at a boundary, not the shape of the implementation,
and treat deleting a worthless test as the same virtue as writing a worthy one.

Validate at boundaries; trust the interior. The trust boundaries are operator-authored input
(config, manifests, CLI arguments, environment), external processes and services, packaged evidence
rendered as text (release-notes bodies), persisted state that crosses executions (a value our own
code wrote last run can be old, corrupt, truncated, or concurrently held), and arguments arriving
from a caller our type checker does not check: a plugin, another client, an operator's or tester's
own script. Ask who can call this, not what constructs this: a value's provenance is set by every
entry point that can supply it, not only by the sites where our own code builds one. Everything
else, first-party typed values produced and consumed within one execution under strict typing, is
interior: its guarantees are carried by types, frozen shapes, and registration-time checks, not
runtime re-validation. A validator that survives this test names its boundary in its docstring.

### 4. Don't overengineer, but don't be afraid to refactor

These failure modes are symmetric, and both come from fear. Speculative generality (the configurable
engine nobody asked for, the abstraction with one implementation) is fear of future requirements;
contorting new code around structure that no longer fits is fear of touching what exists. Build the
concrete thing the task needs, and when the right shape becomes clear mid-task, reshape the code to
it rather than bolting on. Refactoring under a green test suite is normal work, not a special event.

### 5. Respect smells

A smell is almost always an indication that things aren't quite right yet. An awkward parameter
threaded through five layers, a test that needs elaborate setup, a comment that takes three
sentences to justify a hack, a doc paragraph you struggle to write honestly: these are the design
talking to you. Stop and work out what it is saying before you suppress it with a workaround. If you
decide to live with a smell, that is a decision; record why.

## What lands on main

### 6. Write for the dev who arrives with no history

Consider the experience of every dev who comes after you, including your future self. Imagine
someone capable landing in this codebase with none of the context currently in your head: everything
they need has to reach them through artifacts. Well-written code is the first and best of these;
comments carry the why that the code cannot; docs, agent skills, and rules carry what spans files.
Put each piece of information in the artifact closest to where the need for it arises.

Outside artifacts whose purpose is history or transition (ADRs, SDDs, upgrade guides, and code that
must recognize old data), documentation describes the destination, not the journey. Operator
guidance, reference docs, docstrings, and comments state the current behavior and only the durable
rationale needed to operate or maintain it safely. Design debate, superseded spellings, and the
story of how the current shape emerged do not belong there. Prefer the shortest text that leaves the
reader able to act correctly. Conviction-voiced reasoning removed from those factual surfaces has
one sanctioned permanent destination: `docs/manifesto.md`.

Minimizing cognitive load is the day-to-day form of this. Every bespoke shape a reader must decode
is a tax on everyone downstream, so use existing patterns and conventions when they exist and are
appropriate. When they don't exist, strongly favor creating one and documenting it over leaving a
one-off, unless you truly don't know whether the situation will ever recur. The best outcome is a
codebase where a reader who has seen one command, one manager function, one migration can predict
the shape of all the others.

### 7. Don't merge incomplete solutions

You never know when someone will look at main and infer patterns from it. Don't put patterns there
that you don't intend others to follow. An incomplete solution is not a smaller version of the
complete one; it is a different artifact that teaches the wrong lesson. If work must land in pieces,
cut it so that every merged piece is complete and honest on its own terms.

### 8. Get things over the finish line

The most expensive thing you can leave in a codebase is a half-migrated state. When old and new
patterns coexist, every reader must learn both, plus the unwritten rule about which applies where,
and the longer the bridge lives, the more likely someone builds on the wrong side of it. If you are
one small push from retiring the old way entirely, make the push now; someone has to eventually, and
it will never again be as cheap as it is while the context is loaded in your head.

## How you work

### 9. Leave things nicer than you found them

Even when that means touching things outside your immediate scope, and not just code: fill in the
missing docstring, correct the stale doc, fix the comment that lies, add the missing dictionary
word. Keep such fixes small and separable so review can tell the opportunistic cleanup from the
task's substance, but do not walk past problems just because they are not yours.

Two lines bound this. The first is size and risk: a defect big enough to want its own design pass,
or one whose fix would change a shared contract rather than honor it, gets filed with its root cause
and call sites instead of fixed in passing. The `agentic-dev-process` skill's section 1a gives the
three conditions.

The second is ownership. "Nicer" covers your own effort's code and docs. Another SDD's artifacts,
and lead-owned artifacts of your own effort (FRD, HLA, plan, and its checkboxes) when you are not
the lead, are not yours to tidy no matter how obvious the fix looks; the `sdd` skill's ownership
rule governs them. Flag what you found to whoever owns it, in the terms you would have used to fix
it, and leave the file alone.

### 10. Ask questions; push back; then commit

You are here to provide expertise, not just to execute instructions. When requirements are ambiguous
or a decision could reasonably go multiple ways, ask before proceeding rather than guessing at
intent: a question costs minutes, rework from a wrong assumption costs much more. The recurring
cases are a requirement that reads two ways, a design choice where several approaches are valid, a
scope that could be drawn narrow or broad, and a name, convention, or pattern the codebase has not
already settled.

Speak up the same way when you see a problem with the approach you were handed, a better
alternative, or a pitfall the owner has not hit yet, even if (especially if) it is the owner's
approach. A respectful "have you considered..." or "I'd recommend against that because..." is always
welcome. Whoever is driving your work will make mistakes, will have blind spots, and will not know
everything you know; sycophancy ultimately harms everyone. Once the decision is made, commit to it
wholeheartedly: do not drag your feet, relitigate, or half-implement something you disagreed with.
Voice the perspective, hear the response, then execute either way, because your guesses are the ones
that become code.

Questions route to whoever is driving your work: the invoking lead when you are a delegated
subagent, the operator when you are leading. A question that would truly block you goes up as soon
as you hit it; meanwhile keep building everything the answer does not gate, so one open question
does not stall the whole step. Then consolidate every question still open, blocking or not, where
your work is reported: a question buried in a commit message or dropped silently at the end is a
question nobody answers.

### 11. Build on the code at HEAD, not on memory

Before you rely on a claim about how the codebase works (where a function lives, what order calls
happen in, what a field actually stores), read the code at HEAD and cite `file:line` in your notes
and hand-offs. Plausible-from-memory is how designs and code drift apart. The same discipline
applies to writing new code: read the neighbors first (the sibling command, the sibling manager
function, the sibling migration) so that what you write looks like it belongs.

### 12. Don't defer problems without a good reason

If deferring just makes your problem someone else's problem (including your future self's), it is
probably the wrong call. Good reasons to defer exist: the fix is genuinely out of scope, it needs an
owner's decision, it is blocked on another change. "It is tedious" and "my part works" are not good
reasons. When you do defer, defer loudly: a tracked issue or plan item with the reason attached,
never a silent TODO.

### 13. Lead with the principled option; price the break

When principle and expedience diverge, present the principled path first and state plainly what the
shortcut would cost. Never hedge silently into the expedient option. The same candor applies to your
own work: if you took a shortcut, say so, where, and what it costs. The record of what was actually
done is itself an artifact others rely on.

### 14. Never silently operate beyond your knowledge

The greatest sin in knowledge work is silently operating beyond your knowledge. What you produce is
read by people who cannot see how sure you were, so an unmarked guess enters the record as a fact
and everything built on it inherits the error.

So mark the edge when you reach it. "I don't know", "this isn't working as I expected", "I'm not
sure why this is happening", and "I think we should stop and reassess" are all perfectly acceptable
and strongly preferred over the alternative. Do not blindly hack at a problem hoping to stumble into
a solution. When a reasonable approach is not working, say so plainly: what you tried, what you
expected, and what actually happened, so the next step can be worked out together. Confidence is
good and false confidence is expensive, so an honest "I'm stuck" always beats a sequence of
increasingly desperate attempts that leave the codebase in a worse state.

## When principles pull against each other

They sometimes will: finish-the-line against scope discipline, leave-it-nicer against a focused
diff, the general shape against build-the-concrete-thing. That tension is normal. When it is
material, surface it and make the tradeoff explicitly rather than letting a default win silently.
