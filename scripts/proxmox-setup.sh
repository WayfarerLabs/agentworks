#!/bin/bash
set -euo pipefail

# Agentworks Proxmox Setup
#
# One-time setup script that prepares a Proxmox VE host for agentworks:
#   1. Creates a Debian 12 cloud-init VM template with qemu-guest-agent
#   2. Creates a resource pool for agentworks VMs
#   3. Creates a least-privilege user, API token, custom roles, and ACLs
#
# Run on the Proxmox host as root:
#   bash proxmox-setup.sh [VMID] [STORAGE] [BRIDGE]
#
# Arguments (all optional with defaults):
#   VMID    - Template VM ID (default: 9000)
#   STORAGE - Storage volume for VM disks (default: local-lvm)
#   BRIDGE  - Network bridge (default: vmbr0)
#
# Idempotent: safe to re-run. Skips resources that already exist.
# To recreate the API token, answer 'y' when prompted.

VMID="${1:-9000}"
STORAGE="${2:-local-lvm}"
BRIDGE="${3:-vmbr0}"
POOL="agentworks"
USER="agentworks@pam"
TOKEN_NAME="agentworks"

IMAGE_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2"
IMAGE_FILE="/tmp/debian-12-generic-amd64.qcow2"
TEMPLATE_NAME="debian-12-template"

echo "=== Agentworks Proxmox Setup ==="
echo ""
echo "  Template VMID: $VMID"
echo "  Storage:       $STORAGE"
echo "  Bridge:        $BRIDGE"
echo "  Pool:          $POOL"
echo "  User:          $USER"
echo ""

# -------------------------------------------------------------------
# Step 1: Create the VM template
# -------------------------------------------------------------------

echo "--- Step 1: VM Template ---"

if qm status "$VMID" >/dev/null 2>&1; then
    echo "  Template VMID $VMID already exists, skipping."
else
    # Download cloud image
    if [ -f "$IMAGE_FILE" ]; then
        echo "  Using existing image: $IMAGE_FILE"
    else
        echo "  Downloading Debian 12 cloud image..."
        wget -q --show-progress -O "$IMAGE_FILE" "$IMAGE_URL"
    fi

    # Install qemu-guest-agent into the image
    echo "  Installing qemu-guest-agent into image..."
    if ! command -v virt-customize >/dev/null 2>&1; then
        echo "  Installing libguestfs-tools..."
        apt-get update -qq && apt-get install -y -qq libguestfs-tools
    fi
    virt-customize -a "$IMAGE_FILE" --install qemu-guest-agent

    # Create VM
    echo "  Creating VM $VMID..."
    qm create "$VMID" --name "$TEMPLATE_NAME" --memory 2048 --cores 2 \
        --net0 "virtio,bridge=$BRIDGE" \
        --scsihw virtio-scsi-pci \
        --serial0 socket --vga serial0 \
        --agent enabled=1

    # Import and attach disk
    echo "  Importing disk..."
    qm importdisk "$VMID" "$IMAGE_FILE" "$STORAGE"
    qm set "$VMID" --scsi0 "$STORAGE:vm-${VMID}-disk-0"
    qm set "$VMID" --boot order=scsi0

    # Cloud-init drive
    echo "  Adding cloud-init drive..."
    qm set "$VMID" --ide2 "$STORAGE:cloudinit"

    # Convert to template
    echo "  Converting to template..."
    qm template "$VMID"
    echo "  Template created."
fi

echo ""

# -------------------------------------------------------------------
# Step 2: Create resource pool
# -------------------------------------------------------------------

echo "--- Step 2: Resource Pool ---"

if pvesh get /pools/"$POOL" >/dev/null 2>&1; then
    echo "  Pool '$POOL' already exists, skipping."
else
    echo "  Creating pool '$POOL'..."
    pvesh create /pools --poolid "$POOL"
    echo "  Pool created."
fi

echo ""

# -------------------------------------------------------------------
# Step 3: Create custom roles
# -------------------------------------------------------------------

echo "--- Step 3: Custom Roles ---"

# Helper: create or update a role
ensure_role() {
    local roleid="$1"
    local privs="$2"
    if pveum role list --output-format json 2>/dev/null | grep -q "\"roleid\":\"$roleid\""; then
        echo "  Updating role '$roleid'..."
        pveum role modify "$roleid" --privs "$privs"
    else
        echo "  Creating role '$roleid'..."
        pveum role add "$roleid" --privs "$privs"
    fi
}

ensure_role AgentworksVM \
    "VM.Allocate VM.Clone VM.Config.CPU VM.Config.Memory VM.Config.Cloudinit VM.Config.Disk VM.Config.HWType VM.Config.Options VM.Config.Network VM.PowerMgmt VM.Audit VM.Monitor"
ensure_role AgentworksTemplate "VM.Clone VM.Audit"
ensure_role AgentworksStorage "Datastore.AllocateSpace Datastore.Audit"
ensure_role AgentworksSDN "SDN.Use"

echo ""

# -------------------------------------------------------------------
# Step 4: Create user and API token
# -------------------------------------------------------------------

echo "--- Step 4: User & API Token ---"

# Create user (idempotent -- ignore error if exists)
if pveum user list --output-format json 2>/dev/null | grep -q "\"userid\":\"$USER\""; then
    echo "  User '$USER' already exists, skipping."
else
    echo "  Creating user '$USER'..."
    pveum user add "$USER"
fi

# Create API token (privsep=0: token inherits user permissions)
echo "  Creating API token..."
TOKEN_OUTPUT=$(pveum user token add "$USER" "$TOKEN_NAME" --privsep=0 --output-format json 2>/dev/null || true)

if [ -z "$TOKEN_OUTPUT" ]; then
    echo "  Token '$TOKEN_NAME' may already exist. Delete and recreate? (y/N)"
    read -r REPLY
    if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
        pveum user token remove "$USER" "$TOKEN_NAME"
        TOKEN_OUTPUT=$(pveum user token add "$USER" "$TOKEN_NAME" --privsep=0 --output-format json)
    else
        echo ""
        echo "  Skipping token creation. You'll need to use your existing token secret."
        TOKEN_OUTPUT=""
    fi
fi

# Extract token secret
TOKEN_SECRET=""
TOKEN_ID=""
if [ -n "$TOKEN_OUTPUT" ]; then
    TOKEN_SECRET=$(echo "$TOKEN_OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('value',''))" 2>/dev/null || true)
    TOKEN_ID=$(echo "$TOKEN_OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('full-tokenid',''))" 2>/dev/null || true)
fi

if [ -z "$TOKEN_ID" ]; then
    TOKEN_ID="${USER}!${TOKEN_NAME}"
fi

echo ""

# -------------------------------------------------------------------
# Step 5: Assign ACLs (on the user, inherited by the token via privsep=0)
# -------------------------------------------------------------------

echo "--- Step 5: ACL Permissions ---"

echo "  Granting AgentworksVM on /pool/$POOL..."
pveum aclmod "/pool/$POOL" -user "$USER" -role AgentworksVM

echo "  Granting AgentworksTemplate on /vms/$VMID..."
pveum aclmod "/vms/$VMID" -user "$USER" -role AgentworksTemplate

echo "  Granting AgentworksStorage on /storage/$STORAGE..."
pveum aclmod "/storage/$STORAGE" -user "$USER" -role AgentworksStorage

echo "  Granting AgentworksSDN on /sdn/zones/localnetwork..."
pveum aclmod /sdn/zones/localnetwork -user "$USER" -role AgentworksSDN

echo ""

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------

NODE=$(hostname)
API_HOST=$(hostname -f 2>/dev/null || hostname)

echo "==========================================="
echo "  Agentworks Proxmox Setup Complete"
echo "==========================================="
echo ""
echo "Add this to ~/.config/agentworks/config.toml:"
echo ""
echo "  [proxmox]"
echo "  api_url = \"https://${API_HOST}:8006\""
echo "  node = \"${NODE}\""
echo "  token_id = \"${TOKEN_ID}\""
echo "  template_vmid = ${VMID}"
echo "  storage = \"${STORAGE}\""
echo "  pool = \"${POOL}\""
echo "  verify_ssl = false"
echo ""

if [ -n "$TOKEN_SECRET" ]; then
    echo "Set this environment variable (save it -- it cannot be retrieved again):"
    echo ""
    echo "  export PROXMOX_TOKEN_SECRET=\"${TOKEN_SECRET}\""
else
    echo "Use your existing PROXMOX_TOKEN_SECRET environment variable."
fi

echo ""
echo "Then run:"
echo ""
echo "  agw vm create my-vm --platform proxmox"
echo ""
