# Evidence and design constraints

The primary evidence is [issue 877](https://github.com/WayfarerLabs/agentworks/issues/877) and its
[live-capture follow-up](https://github.com/WayfarerLabs/agentworks/issues/877#issuecomment-6787405894).
These are incident reports, not an upstream stability guarantee.

Observed continuation records span Claude 2.1.290 and 2.1.307. Parked records were observed across
2.1.311/314/321 and together with continuation records on 2.1.307 handing off to 2.1.321. Treat the
two signals independently. Versions do not predict which residue appears.

File mtime, assistant output and unfiltered user records cannot establish human activity. No recency
predicate is used. A tail-only scan misses continuation records buried beneath later work, so a
streaming full scan is necessary when cheaper evidence yields no candidate.

The existing code at base 2253a6819 performs an exact UUID filename probe, keeps conversation
addressing in harness namespaces, and validates effective config with declaring-owner provenance.
These existing boundaries support a plugin probe and a shared declaration marker respectively.

Open verification limit: versioned Claude PID records are internal data; detection must degrade
without changing transcript existence semantics when records are absent, malformed or unreadable.
