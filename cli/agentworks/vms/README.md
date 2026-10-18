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

Trust uses the configured managed directory and endpoint lookup with no host key alias. Identity,
agent, client and keepalive selections come from `SSHSettings`. The early guest facts probe enters
as root, directly for root delivery, otherwise through passwordless sudo to root, and resolves the
named delivery account's actual identity. AWS, Azure and GCP explicitly supply the no-idle-stop
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

Core then retains the shared guest preparation and its escaping control facts before constructing
account/file/execution views. A platform can check the observed guest against its availability
evidence; WSL2 uses its actual hold READY. Aggregate teardown retains the original access and shared
preparation until their effects and local custody settle. Neither a route switch nor an accessor can
transfer or finalize that ownership. Creation/rollback integration remains unfinished; existing
consumers are unchanged.

`vm_execution_context(..., route=...)` selects native or canonical delivery explicitly under one
shared `VMOperation` and cleanup fact. Canonical mode validates its stored literal endpoint/account
and SSH policy before provider work, skips native-only configuration and route preparation, and
performs the common guest/account preparation through SSH. It checks retained provider power,
locator and independent availability plus the pinned database selection before exposing views and on
later canonical calls. Native mode checks its selected route around initial guest and account
preparation too. Native and canonical views report their actual semantic route; neither silently
switches delivery. Full platform/native acceptance, creation/rollback and public surface completion
remain separate gates.

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
