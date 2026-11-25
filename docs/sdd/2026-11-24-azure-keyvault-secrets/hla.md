# Azure Key Vault backend architecture

## Integration

Contribute `azure-keyvault` as a version-3 `SecretBackend` from `agentworks.plugins.azure`. Reuse
the existing source registry, model validation, source-first resolver, closed results, and command
surfaces. Do not change the generic backend contract or other providers.

A source binds one HTTPS vault URL and one authentication configuration. Its mapping model carries a
secret name and optional version, with a scalar name shorthand if the existing schema machinery
supports it without another parser. Azure public, US Government, and China vault endpoints must
select the corresponding authentication authority if supported; unsupported endpoints fail locally.
Keep authentication-challenge verification enabled.

Use the official Azure Key Vault Secrets SDK and existing Azure Identity dependency. Construct and
close credentials and SDK clients within the selected operation method. Factory construction and
context entry remain resource-free. The VM helper is not reusable as-is because it eagerly probes an
ARM token and permits browser authentication.

## Authentication and preview

Ambient mode uses the Azure default credential chain. No-impact preview excludes credential paths
that may request operator action. When a permitted lookup cannot proceed without broader impact,
preview reports indeterminate rather than launching interactive authentication. Maximum-impact
preview and actual resolution may use browser authentication, consistently with the existing Azure
VM ambient behavior. Explicit service principals use only their selected credential.

File-backed ambient credentials must read regular files through checked descriptors, not reopen a
path after checking it. Devices, pipes, and stdin aliases must not consume terminal or redirected
input. Feed checked certificate bytes and a checked workload assertion supplier into public SDK
credentials, preserving native environment precedence, selectors, identity, authority, and the
remaining default credential chain. Each workload refresh repeats the checked read. A failed
deployed credential must not become an unavailable credential that silently selects another
unattended identity. Keep the existing explicit ambient browser policy separate from that chain.
Give every constructed credential immediate operation-owned cleanup, including partial construction.

The service-principal mode reads its client secret from the named environment variable during the
selected operation. Tenant and client identifiers stay in source config. Do not add recursive secret
resolution or a generic credential framework to solve it.

Use explicit finite provider network and credential-subprocess timeouts. A connection or read
timeout is not a source-wide deadline. Do not expose a source-wide timeout setting without enforcing
it across authentication, retries, and every lookup in the turn.

## Failures and value handling

Project structured Azure error codes and exception classes into the existing closed backend results.
Only conclusive secret/version absence is ordinary missing. Denied access, unknown 404s,
authentication errors, malformed data, and transport failures remain failures. Suppress provider
diagnostic text, including under debug logging; never forward or chain a value-bearing exception.

Use the same read operation for preview and resolution. Preview validates and discards the value
inside the backend; resolution returns it only through `BackendResolved`. Preserve empty strings and
all line endings; reject NUL. Close operation-owned resources on success, failure, and cancellation
without exposing cleanup exceptions.

## Documentation

Add concise source and mapping examples to secret documentation and update Azure plugin metadata and
sample-config comments. First-party plugin packages can own static concept shells in a local
`guide-content/` directory, but there is no public plugin topic registration API. A small Azure
topic may point to current resource commands and Microsoft's online documentation without copying
schema-owned fields or provider manuals.
