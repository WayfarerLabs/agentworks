# Native SSH binding

The private `_ssh_native_binding.py` helper composes cloud-selected public IPv4 endpoints with
loaded `operator.ssh` settings. It creates an immutable connection, an ordinary carrier and a
factory for fresh managed delivery carriers using that same connection. Construction does not
inspect local identity or trust files, start a process, enroll trust or activate a route.

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
transfer or finalize that ownership. Canonical-context and creation/rollback integration remain
separate unfinished work; existing consumers are unchanged.
