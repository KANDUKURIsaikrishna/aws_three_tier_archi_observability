#!/usr/bin/env python3
"""
force_delete_flow_log_group.py — Destroy-time cleanup for the VPC Flow Logs
CloudWatch log group. Best-effort only, mirroring the original bash's `|| true`:
requires the aws CLI on whatever machine runs `terraform destroy`, and swallows
any failure rather than blocking the destroy on it.

Invoked by terraform/modules/network/main.tf's
null_resource.force_delete_flow_log_group (destroy-time only) via
`interpreter = ["python3"]` (not a shell) so this runs identically on
Windows/macOS/Linux.

Required env vars: LOG_GROUP_NAME, REGION
"""
import os
import subprocess
import time

LOG_GROUP_NAME = os.environ["LOG_GROUP_NAME"]
REGION = os.environ["REGION"]


def main():
    # Grace period for any in-flight flow-log delivery API calls to finish
    # before the log group is deleted -- not the fix itself, see the
    # resource's own comment in main.tf for why ordering (not this sleep)
    # is what actually prevents AWS's self-heal from recreating it.
    time.sleep(15)
    subprocess.run(
        ["aws", "logs", "delete-log-group", "--log-group-name", LOG_GROUP_NAME, "--region", REGION],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    main()
