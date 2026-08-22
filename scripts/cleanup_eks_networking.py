#!/usr/bin/env python3
"""
cleanup_eks_networking.py — Destroy-time cleanup for two resource types EKS
and its own workloads create directly via the EC2 API, entirely outside
Terraform's resource graph: orphaned VPC CNI ENIs and the EKS "cluster
security group".

Invoked by terraform/main.tf's null_resource.cleanup_eks_networking
(destroy-time only) via `interpreter = ["python3"]` (not a shell) so this
runs identically on Windows/macOS/Linux.

Required env vars: VPC_ID, REGION
"""
import os
import subprocess
import time

VPC_ID = os.environ["VPC_ID"]
REGION = os.environ["REGION"]


# The VPC CNI plugin (the aws-node DaemonSet on every EKS worker node)
# attaches secondary ENIs to nodes for pod networking, by calling the EC2
# API directly at pod-scheduling time -- entirely outside Terraform's own
# resource graph, using the node's own IAM permissions. If a node
# terminates (or the whole cluster is torn down) before the CNI's own
# cleanup routine runs, the ENI is left behind: detached, but never
# deleted. Its subnet can't be deleted while it exists.
#
# By the time this runs, module.eks has already been destroyed (see the
# depends_on wiring in main.tf), so any node that owned these ENIs is
# already gone -- but AWS's own state can lag a few seconds behind the
# API calls that tore the node down, so this polls briefly rather than
# assuming the very first check is authoritative.
def cleanup_orphaned_enis():
    description_prefix = "aws-K8S-"
    for attempt in range(20):  # ~5 minutes at 15s each
        result = subprocess.run(
            [
                "aws", "ec2", "describe-network-interfaces",
                "--filters", f"Name=vpc-id,Values={VPC_ID}", "Name=status,Values=available",
                "--region", REGION,
                "--query", "NetworkInterfaces[*].[NetworkInterfaceId,Description]",
                "--output", "text",
            ],
            capture_output=True, text=True,
        )
        lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
        cni_enis = [line.split("\t")[0] for line in lines if description_prefix in line]

        if not cni_enis:
            print("No orphaned VPC CNI ENIs found.")
            return

        print(f"Found {len(cni_enis)} orphaned VPC CNI ENI(s) (attempt {attempt + 1}/20): {cni_enis}")
        for eni_id in cni_enis:
            delete_result = subprocess.run(
                ["aws", "ec2", "delete-network-interface", "--network-interface-id", eni_id, "--region", REGION],
                capture_output=True, text=True,
            )
            if delete_result.returncode == 0:
                print(f"Deleted {eni_id}")
            else:
                print(f"Could not delete {eni_id} yet: {delete_result.stderr.strip()}")
        time.sleep(15)

    print("Gave up waiting for orphaned ENIs to clear after 5 minutes -- "
          "if the VPC/subnet destroy fails next with DependencyViolation, "
          "check `aws ec2 describe-network-interfaces` by hand.")


# EKS auto-creates its own "cluster security group" as a side effect of
# cluster creation (not a Terraform-managed resource). AWS's own
# DeleteCluster is supposed to clean this up automatically, but that's a
# best-effort, one-shot attempt at cluster-deletion time -- if anything
# (typically the same orphaned CNI ENIs above) is still a member of it at
# that exact moment, the cleanup fails silently and the security group is
# stuck permanently, even after whatever was blocking it is later removed.
# Identified by the "aws:eks:cluster-name" tag EKS applies automatically,
# not by name/cluster-name matching, so this doesn't need to know this
# project's cluster name specifically.
def cleanup_eks_cluster_security_group():
    result = subprocess.run(
        [
            "aws", "ec2", "describe-security-groups",
            "--filters", f"Name=vpc-id,Values={VPC_ID}", "Name=tag-key,Values=aws:eks:cluster-name",
            "--region", REGION,
            "--query", "SecurityGroups[*].GroupId",
            "--output", "text",
        ],
        capture_output=True, text=True,
    )
    sg_ids = [sg for sg in result.stdout.strip().split() if sg]

    if not sg_ids:
        print("No leftover EKS cluster security group found.")
        return

    for sg_id in sg_ids:
        delete_result = subprocess.run(
            ["aws", "ec2", "delete-security-group", "--group-id", sg_id, "--region", REGION],
            capture_output=True, text=True,
        )
        if delete_result.returncode == 0:
            print(f"Deleted leftover EKS cluster security group {sg_id}")
        else:
            print(f"Could not delete {sg_id}: {delete_result.stderr.strip()}")


if __name__ == "__main__":
    cleanup_orphaned_enis()
    cleanup_eks_cluster_security_group()
