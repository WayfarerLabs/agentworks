# Architecture

## Declarations

Admin-template and agent-template expose `npm_enabled` (default true), `npm_packages` (default
empty), and `npm_min_release_age_days` (default seven). Agent declarations preserve the existing
omitted-value inheritance semantics. Registry package names, scopes, versions, tags, and ranges are
supported. File, Git, URL, alias, and option inputs are rejected at the declaration boundary so
package age filtering applies to the registry install path.

## Execution

A shared native npm runner operates through the target user's existing transport and login shell. It
installs into a stable directory under that user's home, without sudo, using npm's native minimum
release age and strict engine checks. Unsupported npm versions fail closed before any package
install. Errors use the existing nonfatal initialization warning convention and never retry without
the age filter.

Runtime provisioning remains explicit through existing apt or system install mechanisms, as selected
by the operator. npm does not invoke mise or bootstrap its runtime automatically.

## Ordering and profiles

Both user flows run dotfiles setup, mise setup, npm setup, and then custom user install commands.
Managed PATH state makes the installed tools available before the custom commands run and preserves
those additions in the final profile. Dotfiles and source-line repair remain in the established
lifecycle.

Mise becomes optional at VM scope through `mise_enabled`, default true. Disabling it skips its apt
repository, package, admin setup, and agent setup, without uninstalling existing software.
User-level activation retains its current setting.

## Delivery

One feature branch and one complete change include schema, runners, ordering, behavioral coverage,
operator guidance, and live npm fixture evidence. No pnpm/Bun abstraction or new resource kind is
needed.
