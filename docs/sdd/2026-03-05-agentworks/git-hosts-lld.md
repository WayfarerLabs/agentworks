# Agentworks -- Git Host Providers LLD

**Status:** Active **Parent:** [plan.md](plan.md) -- 1.7

---

## Overview

Git host providers are a pluggable abstraction for registering and removing SSH keys with git
hosting services. During VM initialization, Agentworks generates an SSH keypair on the VM and
registers the public key with the user's configured git hosts so the VM can clone repositories. On
VM deletion, the keys are removed.

---

## Provider Interface

```python
from abc import ABC, abstractmethod


class GitHostProvider(ABC):
    """Base interface for git host providers."""

    @abstractmethod
    def verify_auth(self) -> bool:
        """Check that the provider's authentication is valid.

        Called during pre-flight before VM provisioning begins.
        Should not produce side effects -- only check credentials.
        """
        ...

    @abstractmethod
    def auth_hint(self) -> str:
        """Return a human-readable hint for how to authenticate.

        Shown to the user when verify_auth() fails.
        Example: "Run 'az login' to authenticate with Azure and ensure you
          have access to the '{org}' AzDO organization."
        """
        ...

    @abstractmethod
    def register_key(self, vm_name: str, public_key: str) -> str:
        """Register an SSH public key with the provider.

        Returns the remote key ID (provider-specific) for later removal.
        The key title/description should include the vm_name for traceability.
        """
        ...

    @abstractmethod
    def test_key_present(self, remote_key_id: str) -> bool:
        """Test if a registered key is still present on the provider.

        Used for cleanup verification. Not strictly required, but can help
        identify if keys were manually removed by the user.
        """
        ...

    @abstractmethod
    def remove_key(self, remote_key_id: str) -> None:
        """Remove a previously registered SSH key.

        Called during vm delete. Should not fail if the key is already gone.
        """
        ...
```

---

## Provider Resolution

Providers are instantiated on demand only when needed by a command. That said, all providers must
initialize cleanly prior to the command executing, so any auth failures are atomic.

When a command needs git host providers, resolution works as follows:

```text
1. Determine selected provider names:
     a. --git-hosts flag (if specified) -- comma-separated list
     b. defaults.git_hosts from config (if set)
     c. All keys under [git_hosts.*] (fallback)
2. Validate selected names exist in [git_hosts.*] config -- unknown names are an error
3. Instantiate only the selected providers:
     "azdo"   -> AzDoProvider(org=config.org)
     "github" -> GitHubProvider()
```

This keeps startup fast and avoids instantiating providers that are not needed for the current
command.

---

## AzDO Provider

### Authentication

AzDO uses Azure AD tokens obtained via `az cli`. No PAT is required -- this assumes AzDO and Azure
share the same AAD tenant.

```text
az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798
```

The resource ID `499b84ac-1321-427f-aa17-267ca6975798` is the well-known AzDO resource identifier.

### verify_auth

```text
1. Run: az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798
2. If exit code != 0: return False
3. Parse JSON output, check that accessToken is present and not expired
4. Verify org access: GET https://vssps.dev.azure.com/{org}/_apis/connectiondata
   with Authorization: Bearer <token>
5. If 200: return True, else: return False
```

### auth_hint

```text
"Run 'az login' to authenticate with Azure, then verify access to the '{org}' AzDO organization."
```

### register_key

```text
1. Obtain token: az account get-access-token --resource 499b84ac-...
2. POST https://vssps.dev.azure.com/{org}/_apis/ssh/keys?api-version=7.1
   Headers:
     Authorization: Bearer <token>
     Content-Type: application/json
   Body:
     {
       "displayName": "agentworks-<vm_name>",
       "publicData": "<public_key>"
     }
3. Parse response, return the key ID from the response body
```

### test_key_present

```text
1. Obtain token: az account get-access-token --resource 499b84ac-...
2. GET https://vssps.dev.azure.com/{org}/_apis/ssh/keys?api-version=7.1
   Headers:
     Authorization: Bearer <token>
3. Search response array for an entry matching remote_key_id
4. Return True if found, False otherwise
```

### remove_key

```text
1. Obtain token: az account get-access-token --resource 499b84ac-...
2. DELETE https://vssps.dev.azure.com/{org}/_apis/ssh/keys/<remote_key_id>?api-version=7.1
   Headers:
     Authorization: Bearer <token>
3. Accept 204 (deleted) or 404 (already gone) as success
```

### Error Handling

- Token acquisition failure: raise with auth_hint
- HTTP errors (non-2xx, non-404 on delete): raise with status code and response body
- Network errors: raise with connection details

---

## GitHub Provider

### Authentication

GitHub uses a token obtained from `gh cli` or a configured PAT. The `gh cli` approach is preferred
as it avoids storing tokens in the Agentworks config.

Token resolution order:

1. `gh auth token` (if `gh` is available)
2. `GITHUB_TOKEN` environment variable
3. Error with auth_hint

### verify_auth

```text
1. Obtain token (resolution order above)
2. GET https://api.github.com/user
   Headers:
     Authorization: Bearer <token>
     Accept: application/vnd.github+json
3. If 200: return True, else: return False
```

### auth_hint

```text
"Run 'gh auth login' to authenticate with GitHub, or set the GITHUB_TOKEN environment variable."
```

### register_key

```text
1. Obtain token
2. POST https://api.github.com/user/keys
   Headers:
     Authorization: Bearer <token>
     Accept: application/vnd.github+json
     Content-Type: application/json
   Body:
     {
       "title": "agentworks-<vm_name>",
       "key": "<public_key>"
     }
3. Parse response, return the "id" field (integer, stored as string)
```

### test_key_present

```text
1. Obtain token
2. GET https://api.github.com/user/keys/<remote_key_id>
   Headers:
     Authorization: Bearer <token>
     Accept: application/vnd.github+json
3. If 200: return True
4. If 404: return False
5. Other status: raise
```

### remove_key

```text
1. Obtain token
2. DELETE https://api.github.com/user/keys/<remote_key_id>
   Headers:
     Authorization: Bearer <token>
     Accept: application/vnd.github+json
3. Accept 204 (deleted) or 404 (already gone) as success
```

### Error Handling

Same pattern as AzDO: token failure raises with auth_hint, HTTP errors raise with details, 404 on
delete is accepted.

---

## Key Lifecycle

### Registration (during vm create)

```text
1. Initializer generates ed25519 keypair on VM
2. Reads public key from VM
3. Stores public key in vms table: db.update_vm_ssh_public_key(vm_name, public_key)
4. For each selected provider:
     remote_key_id = provider.register_key(vm_name, public_key)
     db.insert_vm_git_host_key(vm_name, provider_name, remote_key_id)
```

### Removal (during vm delete)

```text
1. Query vm_git_host_keys for the VM
2. For each key record:
     provider = providers[key.git_host_name]
     if provider.test_key_present(key.remote_key_id):
       provider.remove_key(key.remote_key_id)
     else:
       log warning: key already removed from provider
     db.delete_vm_git_host_key(key.id)
3. If a provider is no longer configured (removed from config), log a warning
   and skip removal -- the user will need to remove the key manually
```

### Key Naming Convention

All registered keys use the title/description format `agentworks-<vm_name>`. This provides
traceability in the git host provider's UI (e.g. AzDO SSH keys page, GitHub SSH keys settings).

---

## HTTP Client

Both providers make REST API calls. Rather than pulling in a heavy HTTP library, use
`urllib.request` (stdlib) for these simple JSON requests. Each provider method:

1. Builds the request (URL, headers, JSON body)
2. Executes via `urllib.request.urlopen`
3. Parses the JSON response
4. Handles errors (HTTP status, network)

If the stdlib approach becomes unwieldy, `httpx` is a reasonable upgrade path -- but for the handful
of API calls involved, stdlib is sufficient.

---

## Adding New Providers

To add a new git host provider:

1. Create `git_hosts/<provider_name>.py` implementing `GitHostProvider`
2. Add the type string to the provider factory in `git_hosts/__init__.py`
3. Document any required config fields under `[git_hosts.<name>]`
4. Add the type to the config validation whitelist

No changes to the initializer, CLI, or database schema are needed.
