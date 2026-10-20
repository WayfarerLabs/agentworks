# VM SSH binding

The private `ssh_execution_binding` helper in `_ssh_native_binding.py` composes selected literal
IPv4 endpoints with loaded `operator.ssh` settings. Native cloud routes supply a provider-observed
public endpoint; canonical routes supply the pinned database `tailscale_host`. It creates an
immutable connection, an ordinary carrier and a factory for fresh managed delivery carriers using
that same connection. Construction does not inspect local identity or trust files, start a process,
enroll trust or activate a route.

Each provider's `resolve_native_execution_binding` first requires explicit SSH settings, then reads
and verifies the persisted provider identity under the caller's deadline. AWS reads the exact EC2
instance and account. GCP also verifies the persisted network, subnet and named external access
configuration. Azure reads the exact VM, then its linked NIC and public IP identities under the same
deadline, with service retries disabled. Linked Azure resources may use other resource groups within
the persisted subscription. SDK credential setup and authentication retries remain subject to the
SDK's behavior; successful late results are rejected without replaying the read.

Trust uses the configured managed directory and a stable VM marker-derived alias. Identity, agent,
client and keepalive selections come from `SSHSettings`. The early guest facts probe enters as root,
directly for root delivery, otherwise through passwordless sudo to root, and resolves the named
delivery account's actual identity. AWS, Azure and GCP explicitly supply the no-idle-stop
independent-job availability fact in both passive resolution and owned preparation. The shared SSH
helper defaults to no guarantee; SSH connectivity alone says nothing about platform availability.
This fact excludes explicit VM stop, reboot, host loss and external policy. Route activation,
delivery custody and native lifecycle ownership belong to the caller.

## Owned availability and native preparation

The private native operation retains one platform access before any effect. An optional pure native
configuration check rejects missing route inputs before provider reads, followed by bounded power
observation and the core operator-stopped decision. `ensure_available` owns activation or the real
platform hold and selects a provider locator without requiring native delivery. Native route
preparation follows explicitly, including public endpoint and ingress work where needed.

Core retains each route's guest, account and namespace preparation before constructing its
file/execution views. Common assembly accepts the retained platform access, locator and binding; it
neither activates again nor acquires another claim. A platform can check the observed guest against
its availability evidence; WSL2 uses its actual hold READY. Aggregate teardown retains all route
records and the original access. It settles exact delivery and completed bookkeeping across routes
before resolving any execution lifetime or starting fresh artifact cleanup. Retiring a closed call's
local record does not settle another route's outstanding attempt. Only the outer workflow finalizes
ownership after every component settles. Creation/rollback integration and the production
native-to-canonical handoff remain unfinished; existing consumers are unchanged.

`vm_execution_context(..., route=...)` selects native or canonical delivery explicitly under one
shared `VMOperation` and cleanup fact. Canonical mode validates its stored literal endpoint/account
and SSH policy before provider work, skips native-only configuration and route preparation, and
performs the common guest/account preparation through SSH. It checks retained provider power,
locator and independent availability plus the pinned database selection before exposing views and on
later canonical calls. Native mode checks its selected route around initial guest and account
preparation too. Native and canonical views report their actual semantic route; neither silently
switches delivery. Full platform/native acceptance, creation/rollback and public surface completion
remain separate gates.

VM SSH connections use `agw-vm-<instance-marker>` as their host-key lookup identity across endpoints
and accounts. The persisted core marker is required; ordinary connection construction never creates
one. Creation enrolls `vm:<instance-marker>` once and retains its original endpoint as evidence. A
platform's explicit successful-publication fact skips canonical enrollment, not verification:
canonical preparation still admits current managed policy and connects strictly. Missing, blocked,
revoked or mismatching trust never triggers another first contact. Existing address-only, hashed or
certificate policy is not rewritten to match the alias automatically; explicit trust maintenance and
applicable certificate principals are required before adoption.

When a managed agent is selected, the private operation retains explicit UID-namespace preparation
after numeric account identity and root bootstrap verification, before constructing its file view.
Its original VM owner covers root-gate setup, exact binding publication and the separate fixed
namespace helper. Aggregate cleanup retains that preparation through uncertainty. This does not
coordinate legacy account creation, deletion or numeric UID reuse; those lifecycle and native boot
gates remain required before production agent adoption.

Local Lima's owned access selects its own opaque instance metadata and pins the client, home and
directory identity before native delivery. Availability and native route selection remain separate;
the latter pins the configured delivery user and driver. Startup is one-shot, with durable possible
effect recorded before dispatch and exact running evidence required alongside successful client
completion. Cleanup settles that request and local resources, not Lima's continuing VM runtime.
Directory replacement checks do not promise identity that cannot be copied or automatic restore
adoption. WSL power observation uses only host registration and power listings before availability;
native delivery-user resolution follows it. Registration identity includes the selected Windows
machine, account SID, distribution GUID and installation directory. First import can adopt its new
GUID only after the one successful start and matching fresh running evidence. Later native checks
pin the four configuration inputs used for selection. Unsupported or unconfirmed driver selection
refuses before invoking its inspector. Windows directory-handle coverage and this non-waking path
still need native evidence. Lima WSL retains the existing physical WSL hold for the operation after
activation and registration adoption, before native delivery-user resolution or canonical guest
preparation. Availability requires durable READY and live native ownership; the guest must match its
boot/init identity. Cleanup retains that hold and directory custody through uncertainty and releases
only after unrelated work settles. Native Windows/Lima proof remains open. This does not provide an
independent-job hold; running observations alone do not prevent idle shutdown. Remote SSH-accessed
platform-host composition remains unimplemented here.
