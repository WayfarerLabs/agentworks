# Using Amazon EC2 with Agentworks

Agentworks provisions Debian VMs on Amazon EC2 through the opt-in `aws` system plugin. Each
`aws-ec2` VM site targets one region and may name a subnet. If no subnet is configured, Agentworks
uses a default subnet in the region's default VPC.

## Enable and authenticate the platform

Enable the plugin in `~/.config/agentworks/config.toml`:

```toml
[plugins]
system = ["aws"]
```

Ambient authentication uses the normal boto3 credential chain. An explicit access-key site names the
secret containing its secret access key and never falls back to ambient credentials:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: ec2-dev
spec:
  platform:
    name: aws-ec2
    region: us-east-1
    auth:
      mode: access-key
      access_key_id: AKIAEXAMPLE
      access_key_secret: aws-secret-access-key
```

Add `subnet_id` when the site should use a particular subnet. Add `assume_role_arn` under `auth`
when the access key should assume a role. That source identity then needs `sts:AssumeRole`, and the
destination role trust policy must admit it.

## IAM permissions

The selected identity needs these actions for the complete shipped lifecycle:

```text
ec2:AuthorizeSecurityGroupIngress
ec2:CreateSecurityGroup
ec2:CreateTags
ec2:DeleteSecurityGroup
ec2:DescribeImages
ec2:DescribeInstances
ec2:DescribeInstanceTypes
ec2:DescribeSubnets
ec2:RevokeSecurityGroupIngress
ec2:RunInstances
ec2:StartInstances
ec2:StopInstances
ec2:TerminateInstances
ssm:GetParameter
```

`ssm:GetParameter` is used only to resolve Debian's public AMI parameter. Describe actions normally
need `Resource: "*"`; scope them with region conditions where appropriate. `RunInstances` must be
allowed for every resource type in the actual request, including the Debian AMI, subnet, security
group, instance, network interface, and EBS volume. The request also tags the instance, volume, and
security group with `agentworks:vm`, which is why `ec2:CreateTags` is required.

Agentworks does not use Elastic IPs, instance profiles, snapshots, or explicit volume deletion. It
therefore needs no Elastic IP actions, `iam:PassRole`, snapshot actions, or `ec2:DeleteVolume` for
the shipped request. An account whose selected AMI or EBS encryption defaults use a customer-managed
KMS key may also require the KMS actions that key policy and EBS encryption require.

## What Agentworks verifies

`vm create` authenticates with STS and performs its resource reads before creating anything. AWS
does not provide a reliable general answer for whether an identity can operate future resources,
especially when policies use resource ARNs, request tags, resource tags, VPCs, or region conditions.
Agentworks therefore does not use IAM policy simulation or claim that the runup check certifies the
whole lifecycle.

EC2 does provide exact-request `DryRun` authorization. Agentworks uses it where a later missing
permission would make an earlier mutation unsafe:

- Before opening an SSH ingress tuple, it verifies that the same tuple can be revoked. A definitive
  positive result is required before any route opens.
- Before terminating a VM, it verifies that the VM's security group can be deleted. A definitive
  positive result, or proof that the group is already absent, is required before termination.

Only EC2's documented `DryRunOperation` response is positive permission evidence. An already-absent
security group also needs no cleanup. Permission denials, credential failures, transport failures,
validation errors, and unexpected responses stop these composite operations before the guarded
mutation. A failed explicit delete keeps the VM row so the operator can correct IAM and retry
`agw vm delete`.

## Network and cleanup model

Each VM receives one Agentworks-owned security group. Its empty ingress set is the deny baseline.
Agentworks opens TCP/22 only to the detected operator IPv4 address plus `operator.ssh_allow_cidrs`,
first for bootstrap and later for a native platform shell. It revokes only the tuples that operation
opened.

`agw vm delete <name>` terminates the instance, waits for termination, and then deletes the security
group after its network interface detaches. If any required step fails, retain the VM row and retry
after correcting the provider failure. When investigating residue, match both the stored instance ID
and the `agentworks:vm` tag before deleting anything manually.
