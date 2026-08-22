#!/usr/bin/env python3
"""
force_delete_flow_log_group.py — Destroy-time cleanup for the VPC Flow Logs
CloudWatch log group.

Invoked by terraform/modules/network/main.tf's
null_resource.force_delete_flow_log_group (destroy-time only) via
`interpreter = ["python3"]` (not a shell) so this runs identically on
Windows/macOS/Linux.

Required env vars: LOG_GROUP_NAME, REGION
"""
import json
import os
import subprocess
import time

LOG_GROUP_NAME = os.environ["LOG_GROUP_NAME"]
REGION = os.environ["REGION"]


def still_exists() -> bool:
    result = subprocess.run(
        [
            "aws", "logs", "describe-log-groups",
            "--log-group-name-prefix", LOG_GROUP_NAME,
            "--region", REGION,
            "--query", "logGroups[*].logGroupName",
            "--output", "json",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    try:
        names = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return LOG_GROUP_NAME in names


def main():
    # Grace period for any in-flight flow-log delivery API calls to finish
    # before the log group is deleted -- not the fix itself, see the
    # resource's own comment in main.tf for why ordering (not this sleep)
    # is what actually prevents AWS's self-heal from recreating it.
    time.sleep(15)

    # AWS's VPC Flow Logs service self-heals its destination log group --
    # even after this successfully deletes it, the service can recreate it
    # shortly after if any in-flight delivery was still queued at that exact
    # moment. One delete call isn't reliably enough to catch that (confirmed
    # live: the log group survived a real destroy cycle despite the delete
    # call succeeding). Poll and re-delete for a bit rather than trusting a
    # single attempt.
    for _ in range(6):  # ~90s on top of the initial 15s grace period above
        subprocess.run(
            ["aws", "logs", "delete-log-group", "--log-group-name", LOG_GROUP_NAME, "--region", REGION],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(15)
        if not still_exists():
            return


if __name__ == "__main__":
    main()
