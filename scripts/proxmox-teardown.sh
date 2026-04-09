#!/bin/bash
set -euo pipefail

# Agentworks Proxmox Teardown
#
# Removes all agentworks resources from a Proxmox VE host:
#   1. Destroys all VMs in the agentworks pool
#   2. Removes the resource pool
#   3. Removes the API token, user, and custom roles
#   4. Destroys the VM template
#
# Run on the Proxmox host as root:
#   bash proxmox-teardown.sh [VMID]
#
# Arguments:
#   VMID - Template VM ID to remove (default: 9000)

VMID="${1:-9000}"
POOL="agentworks"
USER="agentworks@pam"
TOKEN_NAME="agentworks"
TOKEN_ID="${USER}!${TOKEN_NAME}"

echo "=== Agentworks Proxmox Teardown ==="
echo ""
echo "  Template VMID: $VMID"
echo "  Pool:          $POOL"
echo "  User:          $USER"
echo ""
echo "This will remove ALL agentworks resources including any VMs in the pool."
echo ""
read -rp "Continue? (y/N) " REPLY
if [ "$REPLY" != "y" ] && [ "$REPLY" != "Y" ]; then
    echo "Aborted."
    exit 0
fi

echo ""

# -------------------------------------------------------------------
# Step 1: Destroy VMs in the pool
# -------------------------------------------------------------------

echo "--- Step 1: Pool VMs ---"

POOL_VMS=$(pvesh get /pools/"$POOL" --output-format json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for member in data.get('members', []):
        if member.get('type') == 'qemu':
            print(member['vmid'])
except Exception:
    pass
" 2>/dev/null || true)

if [ -n "$POOL_VMS" ]; then
    for vm in $POOL_VMS; do
        echo "  Destroying VM $vm..."
        qm stop "$vm" 2>/dev/null || true
        qm destroy "$vm" --purge 2>/dev/null || true
    done
else
    echo "  No VMs in pool."
fi

echo ""

# -------------------------------------------------------------------
# Step 2: Remove resource pool
# -------------------------------------------------------------------

echo "--- Step 2: Resource Pool ---"

if pvesh get /pools/"$POOL" >/dev/null 2>&1; then
    echo "  Removing pool '$POOL'..."
    pvesh delete /pools/"$POOL"
else
    echo "  Pool '$POOL' does not exist, skipping."
fi

echo ""

# -------------------------------------------------------------------
# Step 3: Remove ACLs, token, and user
# -------------------------------------------------------------------

echo "--- Step 3: User & Token ---"

# Remove ACLs (best-effort, ignore errors for paths that don't exist)
echo "  Removing ACLs..."
pveum aclmod "/pool/$POOL" -user "$USER" -role AgentworksVM -delete 2>/dev/null || true
pveum aclmod "/vms/$VMID" -user "$USER" -role AgentworksTemplate -delete 2>/dev/null || true
# Try common storage names for ACL removal
for storage in local local-lvm data; do
    pveum aclmod "/storage/$storage" -user "$USER" -role AgentworksStorage -delete 2>/dev/null || true
done
pveum aclmod /sdn/zones/localnetwork -user "$USER" -role AgentworksSDN -delete 2>/dev/null || true

# Remove token
echo "  Removing API token..."
pveum user token remove "$USER" "$TOKEN_NAME" 2>/dev/null || true

# Remove user
echo "  Removing user '$USER'..."
pveum user delete "$USER" 2>/dev/null || true

echo ""

# -------------------------------------------------------------------
# Step 4: Remove custom roles
# -------------------------------------------------------------------

echo "--- Step 4: Custom Roles ---"

for role in AgentworksVM AgentworksTemplate AgentworksStorage AgentworksSDN; do
    if pveum role list --output-format json 2>/dev/null | grep -q "\"roleid\":\"$role\""; then
        echo "  Removing role '$role'..."
        pveum role delete "$role"
    else
        echo "  Role '$role' does not exist, skipping."
    fi
done

echo ""

# -------------------------------------------------------------------
# Step 5: Destroy template
# -------------------------------------------------------------------

echo "--- Step 5: VM Template ---"

if qm status "$VMID" >/dev/null 2>&1; then
    echo "  Destroying template $VMID..."
    qm destroy "$VMID" --purge 2>/dev/null || true
else
    echo "  Template $VMID does not exist, skipping."
fi

echo ""
echo "==========================================="
echo "  Agentworks Proxmox Teardown Complete"
echo "==========================================="
echo ""
echo "Remember to also remove the [proxmox] section from your"
echo "agentworks config.toml and unset PROXMOX_TOKEN_SECRET."
echo ""
