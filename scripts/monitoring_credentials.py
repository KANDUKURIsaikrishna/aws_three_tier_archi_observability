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
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TERRAFORM_DIR = REPO_ROOT / "terraform"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _first_useful_line(stderr: str) -> str:
    """Terraform/aws error output is wrapped in ANSI colour and box-drawing
    characters -- picking `.splitlines()[-1]` lands on a bare '╵'. Strip the
    decoration and return the first line that actually says something,
    preferring the one that starts with 'Error:'."""
    lines = []
    for raw in stderr.splitlines():
        clean = _ANSI.sub("", raw).strip().strip("│╷╵").strip()
        if clean:
            lines.append(clean)
    if not lines:
        return "unknown error"
    for line in lines:
        if line.lower().startswith("error"):
            return line[:120]
    return lines[0][:120]


def load_region() -> str:
    """AWS_REGION from config.env, else whatever the AWS CLI itself is set to
    (`aws configure get region` -- profile / AWS_REGION / AWS_DEFAULT_REGION).
    No hardcoded literal; errors if neither yields anything."""
    config_path = REPO_ROOT / "config.env"
    if config_path.exists():
        for raw in config_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("AWS_REGION="):
                val = line.partition("=")[2].strip().strip('"').strip("'")
                if val:
                    return val
    proc = subprocess.run(
        ["aws", "configure", "get", "region"],
        capture_output=True, text=True,
    )
    region = proc.stdout.strip()
    if region:
        return region
    print("error: no region -- set AWS_REGION in config.env or "
          "`aws configure set region <region>`.", file=sys.stderr)
    sys.exit(1)


def terraform_output(name: str) -> str:
    result = subprocess.run(
        ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-raw", name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        hint = _first_useful_line(result.stderr)
        if "Backend initialization" in result.stderr or "please run \"terraform init\"" in result.stderr:
            hint = "run: python3 scripts/init_backend.py"
        return f"<unavailable: {hint}>"
    return result.stdout.strip()


def secret_value(secret_id: str, region: str) -> str:
    result = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value",
         "--secret-id", secret_id, "--region", region,
         "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"<unavailable: {_first_useful_line(result.stderr)}>"
    return result.stdout.strip()


def main():
    region = load_region()

    grafana_url = terraform_output("grafana_url")
    prometheus_url = terraform_output("prometheus_url")
    alertmanager_url = terraform_output("alertmanager_url")
    loki_url = terraform_output("loki_url")

    grafana_password = secret_value("/bookstore/grafana-admin", region)
    monitoring_password = secret_value("/bookstore/monitoring-basic-auth", region)

    rows = [
        ("Grafana", grafana_url, "admin", grafana_password),
        ("Prometheus", prometheus_url, "admin", monitoring_password),
        ("Alertmanager", alertmanager_url, "admin", monitoring_password),
        ("Loki", loki_url, "-", "(no auth - same box)"),
    ]

    name_w = max(len(r[0]) for r in rows)
    url_w = max(len(r[1]) for r in rows)
    print(f"{'Service':<{name_w}}  {'URL':<{url_w}}  {'User':<6}  Password")
    print(f"{'-'*name_w}  {'-'*url_w}  {'-'*6}  {'-'*24}")
    for name, url, user, password in rows:
        print(f"{name:<{name_w}}  {url:<{url_w}}  {user:<6}  {password}")


if __name__ == "__main__":
    sys.exit(main())
