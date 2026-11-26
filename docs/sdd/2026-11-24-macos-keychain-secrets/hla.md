# macOS Keychain backend architecture

## Integration

Proposed response to the draft requirements, pending acceptance in Agention thread 6.

Ship `macos-keychain` as an opt-in system plugin and version-3 secret backend, in the same
distribution as other first-party plugins. Imports and models remain portable. Offline readiness
reports native availability from the host platform without loading Security or accessing a keychain.
Reuse the current source registry, mapping validation, precedence, and closed results.

Each source binds one keychain path or the native default keychain. Require explicit service and
account mappings and a finite timeout using the existing source timeout convention. Do not search
the entire native search list implicitly. No core contract or cross-platform keyring framework is
needed.

## Native worker isolation

The file-keychain API's UI control is process-local. Run native operations in a dedicated Python
worker process so zero-impact preview can disable that control without changing the CLI's own
setting. This is isolation for a concrete native global, not a new plugin execution framework.

Start the worker only inside the selected preview or resolution method, using the current Python
executable and the packaged module. Send a validated batch over private pipes; do not place values
in arguments, files, or logs. The worker does not inherit usable terminal input or a controlling
terminal. A parent-enforced shrinking source-turn deadline covers startup, all lookups, and cleanup.
Termination must reap the worker and close pipes. Never forward stderr or traceback text.

Use a small lazy `ctypes` binding to the macOS Security and CoreFoundation frameworks. Declare exact
ABI signatures and ownership. Each child owns one intent and batch: set its native interaction
policy once and retain it through native cleanup. Process exit discards the setting; no getter or
restoration path is needed, and the CLI's own setting never changes.

Obtain a concrete keychain reference with `SecKeychainOpen` or `SecKeychainCopyDefault`, then check
status. Opening a reference alone does not prove that the file exists. At zero impact, locked state
is indeterminate; under allowed impact, explicitly unlock when necessary so cancellation/errors
surface before item search. Do not relock during cleanup: authentication state can be shared with
other applications.

Call `SecKeychainFindGenericPassword` with that concrete keychain and nonempty service/account
buffers plus explicit byte lengths. Copy returned password data with its explicit length, never NUL
termination. Release native password memory and retained references in `finally`. There is no
keychain close API to invent.

## Results and containment

Preview fetches and validates the data inside the child, then returns only a closed availability,
missing, failure, or indeterminate status. Resolution may return the validated string through the
private result pipe and then the framework's redacted `BackendResolved` boundary. Validate worker
output as external process input, including complete result keys, tags, types, framing, intent
legality, and exit status. Reject partial or malformed output without displaying it.

Only a specific item-not-found status after a valid source preflight is ordinary missing. Source
absence, denial, cancellation, native interaction restrictions, timeout, and invalid text remain
distinct failures or legal preview indeterminate. Native status handling must be verified against a
real macOS fixture. Account for lock/removal races conservatively; do not retry with another
keychain identity or claim that a preflight makes native state immutable.

## Collateral and verification

Update the installed plugin index, plugin inventory/descriptions, sample config comments,
secret-system README and guide shell, and packaged topic prose. Explain that native approval can
unlock the keychain or grant access, and that cleanup does not reverse those operator choices.

Tests on Linux/Windows cover portability and parent protocol/lifecycle semantics. Independent
private project, complexity, and correctness/security reviews precede ready handoff. The standard
published integration tester must exercise this exact build on macOS; its native observations, not
simulated status codes, determine live coverage.
