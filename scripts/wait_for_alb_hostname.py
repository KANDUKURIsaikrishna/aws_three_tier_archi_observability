#!/usr/bin/env python3
"""
wait_for_alb_hostname.py — Wait for ArgoCD to sync the bookstore Ingress,
then wait for the AWS Load Balancer Controller to provision the real ALB
and populate the Ingress's hostname.

Invoked by terraform/argocd.tf's null_resource.wait_for_alb_hostname via
`interpreter = ["python3"]` (not a shell) so this runs identically on
Windows/macOS/Linux — no bash for-loop, no $(...) subshell, no cmd.exe
quoting.

Required env vars: CLUSTER_NAME, REGION
"""
import os
import subprocess
import sys
import time

CLUSTER_NAME = os.environ["CLUSTER_NAME"]
REGION = os.environ["REGION"]


def main():
    subprocess.run(
        ["aws", "eks", "update-kubeconfig", "--name", CLUSTER_NAME, "--region", REGION],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    # Stage 1: wait for ArgoCD to have actually synced the Ingress object
    # into existence -- up to 5 minutes, generous since automated sync on a
    # brand-new Application typically starts within seconds, not a full
    # 5-minute poll cycle, but this is a safety margin, not the expected path.
    for _ in range(60):
        result = subprocess.run(
            ["kubectl", "get", "ingress", "bookstore-ingress", "-n", "bookstore"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            break
        time.sleep(5)
    else:
        # Previously fell through silently into Stage 2's kubectl wait,
        # which would then fail on a nonexistent resource with a confusing
        # error instead of stating the real problem: ArgoCD never synced
        # the Ingress at all within 5 minutes.
        sys.exit("Ingress bookstore-ingress in namespace bookstore never appeared after 5 minutes")

    # Stage 2: wait for the AWS Load Balancer Controller to finish
    # provisioning the real ALB and populate the Ingress's status.
    subprocess.run(
        [
            "kubectl", "wait",
            "--for=jsonpath={.status.loadBalancer.ingress[0].hostname}",
            "ingress/bookstore-ingress", "-n", "bookstore", "--timeout=240s",
        ],
        check=True,
    )


if __name__ == "__main__":
    sys.exit(main())
