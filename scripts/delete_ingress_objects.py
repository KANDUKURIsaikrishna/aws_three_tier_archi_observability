#!/usr/bin/env python3
"""
delete_ingress_objects.py — Destroy-time cleanup for the AWS Load Balancer
Controller: delete Ingress objects (releasing their finalizers so the real
ALB gets torn down) before `terraform destroy` reaches the VPC, then wait
for the controller's shared backend security group to clear before the VPC
delete can succeed.

Ported faithfully from a heavily incident-hardened bash script (see the
comments in terraform/modules/eks-addons/aws-load-balancer-controller.tf
for the exact incidents each step below was written to prevent) --
preserving its exact fail-loudly-on-kubectl-delete vs.
best-effort-on-security-group-poll semantics. Invoked via
`interpreter = ["python3"]` (not a shell) so this runs identically on
Windows/macOS/Linux -- no bash for-loop, no $(...) subshell, no cmd.exe
quoting.

Required env vars: CLUSTER_NAME, REGION
"""
import os
import subprocess
import time

CLUSTER_NAME = os.environ["CLUSTER_NAME"]
REGION = os.environ["REGION"]


def main():
    # Best-effort -- if the cluster is already gone or kubeconfig can't be
    # updated, the kubectl deletes below will simply fail loudly on their
    # own, which is the correct behavior either way.
    subprocess.run(
        ["aws", "eks", "update-kubeconfig", "--name", CLUSTER_NAME, "--region", REGION],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Deliberately NOT best-effort on these two -- a real ALB deletion (the
    # controller's own AWS API call, triggered by this finalizer-gated
    # delete) can genuinely take several minutes. Failing loudly on a real
    # timeout here means a genuine problem surfaces as a clear
    # `terraform destroy` error instead of a silently orphaned ALB
    # discovered much later as an unrelated-looking subnet-destroy hang.
    for namespace in ("bookstore", "gateway"):
        subprocess.run(
            [
                "kubectl", "delete", "ingress", "--all", "-n", namespace,
                "--wait", "--timeout=600s", "--ignore-not-found",
            ],
            check=True,
        )

    # The two kubectl deletes above only confirm the Ingress OBJECTS (and
    # their own per-target-group SGs) are gone -- they say nothing about the
    # controller's separate SHARED "backend" security group
    # (k8s-traffic-<cluster>-<hash>, tagged elbv2.k8s.aws/cluster, one per
    # cluster, reused across every Ingress group), which the controller only
    # deletes once it notices zero Ingress groups reference it left -- a
    # distinct, slightly-later reconcile than clearing the Ingress finalizer
    # itself.
    #
    # This is the ONLY real lever available: aws_vpc has no `timeouts` block
    # at all, so DeleteVpc is a single, immediate API call with no
    # Terraform-side retry/backoff to extend. If this SG still exists by the
    # time the VPC's destroy runs, the whole apply hard-errors on
    # DependencyViolation -- there's no second safety net downstream. So this
    # waits up to 20 minutes (120 x 10s), matching the longest this has
    # actually taken on a real cluster, specifically so destroy doesn't
    # reach that unrecoverable step until the SG has had a real chance to
    # clear. Best-effort at the very end -- if even 20 minutes isn't enough,
    # letting destroy proceed and hard-error on the VPC (with a clear
    # DependencyViolation message pointing at exactly what to clean up
    # manually) beats hanging here forever with no error at all.
    for attempt in range(1, 121):
        try:
            result = subprocess.run(
                [
                    "aws", "ec2", "describe-security-groups",
                    "--filters", f"Name=tag:elbv2.k8s.aws/cluster,Values={CLUSTER_NAME}",
                    "--region", REGION,
                    "--query", "SecurityGroups[0].GroupId", "--output", "text",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            sg_id = result.stdout.strip()
        except subprocess.CalledProcessError:
            sg_id = ""

        if not sg_id or sg_id == "None":
            print("Controller-managed backend security group already gone.")
            break

        print(f"Waiting for controller to clean up its shared backend security group ({sg_id}, attempt {attempt}/120)...")
        time.sleep(10)


if __name__ == "__main__":
    main()
