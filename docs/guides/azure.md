# Using Azure Virtual Machines with Agentworks

Agentworks provisions Debian VMs through the opt-in `azure` system plugin. Each `azure-vm` site
targets one existing resource group and region. Agentworks creates per-VM compute and network
resources inside that group; it never creates or deletes the group itself.

## Enable and authenticate the platform

Enable the plugin in `~/.config/agentworks/config.toml`:

```toml
[plugins]
system = ["azure"]
```

Ambient authentication uses the Azure default credential chain. A service-principal site names the
secret containing its client secret and never falls back to ambient credentials:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: azure-dev
spec:
  platform:
    name: azure-vm
    subscription_id: 00000000-0000-0000-0000-000000000000
    resource_group: agentworks-vms
    region: eastus2
    auth:
      mode: service-principal
      tenant_id: 00000000-0000-0000-0000-000000000000
      client_id: 00000000-0000-0000-0000-000000000000
      secret: azure-client-secret
```

The selected identity needs create, read, update, delete, join, and operation-polling rights for
virtual machines, managed disks, public IP addresses, network security groups and rules, virtual
networks and subnets, and network interfaces in the resource group. Azure's built-in Contributor
role at the resource-group scope is the simplest supported starting point. A custom role must cover
the complete action families Agentworks uses:

```text
Microsoft.Compute/disks/read
Microsoft.Compute/disks/write
Microsoft.Compute/disks/delete
Microsoft.Compute/locations/operations/read
Microsoft.Compute/virtualMachines/read
Microsoft.Compute/virtualMachines/write
Microsoft.Compute/virtualMachines/delete
Microsoft.Compute/virtualMachines/start/action
Microsoft.Compute/virtualMachines/deallocate/action
Microsoft.Compute/virtualMachines/instanceView/read
Microsoft.Network/locations/operations/read
Microsoft.Network/locations/operationResults/read
Microsoft.Network/networkInterfaces/read
Microsoft.Network/networkInterfaces/write
Microsoft.Network/networkInterfaces/delete
Microsoft.Network/networkInterfaces/join/action
Microsoft.Network/networkSecurityGroups/read
Microsoft.Network/networkSecurityGroups/write
Microsoft.Network/networkSecurityGroups/delete
Microsoft.Network/networkSecurityGroups/join/action
Microsoft.Network/networkSecurityGroups/securityRules/read
Microsoft.Network/networkSecurityGroups/securityRules/write
Microsoft.Network/networkSecurityGroups/securityRules/delete
Microsoft.Network/publicIPAddresses/read
Microsoft.Network/publicIPAddresses/write
Microsoft.Network/publicIPAddresses/delete
Microsoft.Network/publicIPAddresses/join/action
Microsoft.Network/virtualNetworks/read
Microsoft.Network/virtualNetworks/write
Microsoft.Network/virtualNetworks/delete
Microsoft.Network/virtualNetworks/subnets/read
Microsoft.Network/virtualNetworks/subnets/write
Microsoft.Network/virtualNetworks/subnets/join/action
Microsoft.Resources/subscriptions/resourceGroups/read
```

No snapshot actions are required.

## What Agentworks verifies

Before `vm create` writes local state or Azure resources, Agentworks confirms that the configured
resource group exists. It then asks Azure for the caller's resource-group permissions and checks the
create, rollback, and bootstrap-route-closure actions. A complete response that definitively lacks
one of those grants fails before provisioning.

This is a negative diagnostic, not an authorization certificate. Azure can still refuse a request
because of deny assignments, conditions, policy, locks, propagation delay, linked-resource scope, or
a later RBAC change. If the permission listing itself is unavailable or incomplete, Agentworks warns
and lets the real operation remain authoritative. The permission query uses
`Microsoft.Authorization/permissions/read`; a role that cannot make that query is not assumed to
lack VM permissions.

Start, deallocate, delete, and native-route operations run against existing resources that may have
more specific role assignments than the resource group. Agentworks lets those exact operations
authorize themselves instead of rejecting them from a broader-scope approximation.

## Network and cleanup model

Each VM gets an Agentworks-owned public IP, NSG, VNet, NIC, and managed OS disk. The NSG has a
deny-all-inbound baseline and ephemeral scoped TCP/22 allows for bootstrap and native platform
shells. `agw vm delete <name>` deletes the VM first, then sweeps its auxiliary resources and
confirms that the VM is absent before dropping the database row. A failed delete keeps the row so
the operator can correct RBAC and retry.
