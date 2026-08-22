#!/usr/bin/env python3
"""
monitoring_credentials.py — Print every observability URL + password in one shot.

    python3 scripts/monitoring_credentials.py

Reads the monitoring URLs from `terraform output` and the passwords from
Secrets Manager. Written in Python (not bash) specifically to sidestep a
real Git Bash gotcha on Windows: bash auto-converts any argument starting
with "/" into a Windows path before handing it to a native .exe, which
silently turns `--secret-id /bookstore/grafana-admin` into a mangled path
and makes `aws secretsmanager get-secret-value` fail with a confusing
"Invalid name" error. subprocess.run() here calls `aws.exe` directly,
bypassing bash entirely, so that conversion never happens.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TERRAFORM_DIR = REPO_ROOT / "terraform"


def load_region() -> str:
    config_path = REPO_ROOT / "config.env"
    if config_path.exists():
        for raw in config_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("AWS_REGION="):
                return line.partition("=")[2].strip().strip('"').strip("'")
    return "us-west-1"


def terraform_output(name: str) -> str:
    result = subprocess.run(
        ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-raw", name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"<unavailable: {result.stderr.strip().splitlines()[-1] if result.stderr else 'unknown error'}>"
    return result.stdout.strip()


def secret_value(secret_id: str, region: str) -> str:
    result = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value",
         "--secret-id", secret_id, "--region", region,
         "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"<unavailable: {result.stderr.strip().splitlines()[-1] if result.stderr else 'unknown error'}>"
    return result.stdout.strip()


def main():
    region = load_region()

    grafana_url = terraform_output("grafana_url")
    prometheus_url = terraform_output("prometheus_url")
    alertmanager_url = terraform_output("alertmanager_url")

    grafana_password = secret_value("/bookstore/grafana-admin", region)
    monitoring_password = secret_value("/bookstore/monitoring-basic-auth", region)

    rows = [
        ("Grafana", grafana_url, "admin", grafana_password),
        ("Prometheus", prometheus_url, "admin", monitoring_password),
        ("Alertmanager", alertmanager_url, "admin", monitoring_password),
    ]

    name_w = max(len(r[0]) for r in rows)
    url_w = max(len(r[1]) for r in rows)
    print(f"{'Service':<{name_w}}  {'URL':<{url_w}}  {'User':<6}  Password")
    print(f"{'-'*name_w}  {'-'*url_w}  {'-'*6}  {'-'*24}")
    for name, url, user, password in rows:
        print(f"{name:<{name_w}}  {url:<{url_w}}  {user:<6}  {password}")


if __name__ == "__main__":
    sys.exit(main())
