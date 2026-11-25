# GCP Secret Manager backend architecture

## Integration and addressing

Proposed response to the draft requirements, pending acceptance in Agention thread 7.

Contribute `gcp-secrets-manager` as a version-3 secret backend from the existing `gcp` plugin. Reuse
the source registry, mapping validation, closed results, preview and source precedence. Do not add a
provider abstraction or change the generic backend contract.

A source binds one explicit project and optional regional location. A mapping carries one secret
name and optional version/alias, with a scalar name shorthand for `latest`. No mapping means not
applicable. Construct the native version resource and endpoint deterministically from validated
config. Regional paths and endpoints must remain paired; global paths must not be silently sent to
regional endpoints or vice versa.

Use the official Secret Manager SDK and an explicit CRC32C dependency. Create credentials and an
operation-local native REST transport only inside the selected method. Factory and context entry are
resource-free. The transport uses the selected global/regional public service endpoint, provided
credentials, and no client-certificate callback. Avoid default certificate-command discovery and
custom endpoints. Close transport resources within the operation, including failure and cancellation
paths.

## Credential selection and supported forms

Keep native ADC precedence while avoiding unnecessary project discovery: the source already selects
a project. Generic ADC loading can refresh external credentials or invoke gcloud before returning.
Instead select the appropriate credential file, read it once as regular-file input, validate its
discriminator/source structure, and use public typed credential loaders on that same parsed mapping.
A selected invalid or unsupported credential does not permit identity fallback.

Support the proposed service-account, stored-user/workforce, impersonation, file/URL federation, AWS
federation and attached-metadata forms. Retain native scope and quota-project semantics. Reject
executable, certificate-sourced, GDCH and unknown credentials before their loader can run. Inspect
nested credential descriptions before constructing impersonated credentials. Do not enable
interactive reauthentication for stored-user credentials.

Token-file reads must also remain regular-file reads during refresh; a path stat followed by an
unguarded later SDK read is not sufficient. Use the public subject-token supplier seam where needed,
preserving native file-format semantics. Supported URL suppliers are unattended token sources; no
browser, login command, certificate helper, or terminal challenge is launched.

No supported authentication path requests operator interaction. Therefore preview at either impact
and actual resolution can use the same policy; unsupported providers fail authentication under every
intent rather than returning a false impact-limited indeterminate.

## Values, failures, and diagnostics

Use the native access operation for preview and resolution. Check payload presence, checksum
presence and value, then decode SDK bytes once with strict UTF-8 and reject NUL. A valid empty
payload remains an empty string; an absent checksum is distinct from a present checksum of zero.
Preview validates and discards the data before returning only availability.

Map structured Google exception classes and status evidence into the current closed results.
Confirmed not-found permits fallback; permission failures, disabled/destroyed state, invalid
requests, authentication errors, transport failures and unknown errors remain hard failures. Never
reinterpret `latest` as a search over enabled versions.

Contain provider logging throughout credentials, access, and cleanup, including existing child
handlers and debug response logging. Never forward or chain native exception text. Use finite
authentication and service HTTP timeouts and bounded retries. Multiple phases may consume several
intervals, so these bounds are not a source-wide wall deadline.

## Collateral and validation

Update GCP plugin metadata/inventories, sample config comments, secret-system documentation, the
existing GCP guide, and retained secret guide shell. Explain the supported ADC envelope and external
login setup without duplicating Google's manuals.

Behavioral tests must exercise real credential constructors and SDK request boundaries with
synthetic files/HTTP responses, not merely replace the outer client factory. Cover identity
precedence, forbidden providers, safe file refresh, mTLS-command suppression, exact bytes, checksum
presence/zero, endpoint routing, closed errors, and resolver fallback. Private project, complexity
and correctness/security reviews precede ready handoff. Published review and live integration
evidence independently determine merge readiness.
