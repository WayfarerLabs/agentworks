# Native npm support

New effort authorized by the operator on 2026-11-12. This document records the
requirements supplied in the authenticated session; implementation choices live in
the architecture and plan.

1. Admin and agent templates can declare npm CLI packages installed per user.
2. Native npm installs work independently of mise. Neither installation mechanism
   requires the other to be enabled.
3. An `npm_enabled` flag toggles the entire npm feature for the selected user.
4. A configurable minimum age in days refuses package releases newer than that age.
5. Both mise and npm install before custom user install commands.
6. Installed executables are available across that user's shells and sessions.
7. Node.js and npm version requirements are checked and documented.

The operator selected npm for the initial implementation. Alternative package
managers are outside this effort.
