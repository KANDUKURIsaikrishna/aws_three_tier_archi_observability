#!/usr/bin/env python3
"""
init_backend.py -- Terraform S3 backend bootstrap. DEPLOYMENT.md Step 2.

Run ONCE before the first `terraform apply` in a fresh checkout, or whenever
terraform/versions.tf has an empty bucket string.

What it does:
  1. Reads your AWS Account ID (no hardcoding needed)
  2. Creates the S3 state bucket if it doesn't exist yet (state locking is
     native S3 conditional-write locking via `use_lockfile` -- no DynamoDB)
  3. Writes the correct bucket name AND region into terraform/versions.tf
  4. Runs `terraform init` (or `terraform init -reconfigure` if already init'd)

Region resolution, in priority order:
  1. An explicit CLI arg: python3 scripts/init_backend.py us-west-2
  2. AWS_REGION from config.env, if config.env exists (same file
     scripts/configure.py reads -- run this AFTER filling in config.env so
     the backend's region and terraform.tfvars' region can't drift apart)
  3. us-west-1, if neither of the above is set

Pure Python 3 stdlib + the `aws` and `terraform` CLIs on PATH -- identical
behaviour on Windows, macOS and Linux (no shell, no sed, no mktemp).

Usage:
  python3 scripts/init_backend.py [region]
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV = REPO_ROOT / "config.env"
VERSIONS_TF = REPO_ROOT / "terraform" / "versions.tf"
TF_DIR = REPO_ROOT / "terraform"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def tool(name: str) -> str:
    """Resolve a CLI on PATH (name or name.exe), or exit with a clear error."""
    found = shutil.which(name)
    if not found:
        die(f"`{name}` not found on PATH -- install it and re-run.")
    return found


def run(argv, **kw):
    """subprocess.run with check=True, list args, never shell=True."""
    return subprocess.run(argv, check=True, **kw)


def capture(argv) -> str:
    return subprocess.run(
        argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ).stdout.strip()


def read_config_region() -> str:
    if not CONFIG_ENV.is_file():
        return ""
    region = ""
    for raw in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("AWS_REGION="):
            region = line.partition("=")[2].strip().strip('"').strip("'")
    return region


def bucket_exists(aws: str, bucket: str, region: str) -> bool:
    proc = subprocess.run(
        [aws, "s3api", "head-bucket", "--bucket", bucket, "--region", region],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def create_bucket(aws: str, bucket: str, region: str) -> None:
    print("[create] S3 bucket...")
    argv = [aws, "s3api", "create-bucket", "--bucket", bucket, "--region", region]
    if region != "us-east-1":
        argv += ["--create-bucket-configuration", f"LocationConstraint={region}"]
    run(argv, stdout=subprocess.DEVNULL)

    run(
        [aws, "s3api", "put-bucket-versioning", "--bucket", bucket,
         "--versioning-configuration", "Status=Enabled"],
        stdout=subprocess.DEVNULL,
    )
    run(
        [aws, "s3api", "put-bucket-encryption", "--bucket", bucket,
         "--server-side-encryption-configuration",
         json.dumps({"Rules": [{
             "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
             "BucketKeyEnabled": True,
         }]})],
        stdout=subprocess.DEVNULL,
    )
    run(
        [aws, "s3api", "put-public-access-block", "--bucket", bucket,
         "--public-access-block-configuration",
         "BlockPublicAcls=true,IgnorePublicAcls=true,"
         "BlockPublicPolicy=true,RestrictPublicBuckets=true"],
        stdout=subprocess.DEVNULL,
    )
    print("[ok] S3 bucket created.")


def patch_versions_tf(bucket: str, region: str) -> None:
    """Replace whatever sits between the quotes on the backend block's
    `bucket` and `region` fields -- including an empty string. Terraform
    resolves backend config before any variable, so these two fields can
    never be `var.aws_region` references; patching the literal text in is
    the only thing that keeps them correct.

    Read and write with newline='' so the file's existing line endings
    (CRLF on a Windows checkout with autocrlf, LF elsewhere) are preserved
    byte-for-byte -- otherwise every line shows as changed in `git diff`."""
    if not VERSIONS_TF.is_file():
        die(f"{VERSIONS_TF} not found.")
    with open(VERSIONS_TF, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    text = re.sub(
        r'bucket(\s*)=(\s*)"[^"]*"',
        lambda m: f'bucket{m.group(1)}={m.group(2)}"{bucket}"',
        text,
        count=1,
    )
    text = re.sub(
        r'region(\s*)=(\s*)"[^"]*"',
        lambda m: f'region{m.group(1)}={m.group(2)}"{region}"',
        text,
        count=1,
    )
    with open(VERSIONS_TF, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print("[ok] versions.tf updated.\n")
    # Echo the backend block back (same as the old script's `grep -A 8`).
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if 'backend "s3"' in line:
            print("\n".join(lines[i:i + 9]))
            break


def main() -> None:
    aws = tool("aws")
    terraform = tool("terraform")

    region = (sys.argv[1] if len(sys.argv) > 1 else "") or read_config_region() or "us-west-1"
    account_id = capture(
        [aws, "sts", "get-caller-identity", "--query", "Account", "--output", "text"]
    )
    bucket = f"bookstore-terraform-state-{account_id}"

    print(f"\nAccount : {account_id}")
    print(f"Region  : {region}")
    print(f"Bucket  : {bucket}\n")

    if bucket_exists(aws, bucket, region):
        print("[skip] S3 bucket already exists.")
    else:
        create_bucket(aws, bucket, region)

    print("\n[patch] Writing bucket name and region into versions.tf...")
    patch_versions_tf(bucket, region)

    print("\n[init] Running terraform init...")
    if (TF_DIR / ".terraform").is_dir():
        run([terraform, "init", "-reconfigure"], cwd=TF_DIR)
    else:
        run([terraform, "init"], cwd=TF_DIR)

    bar = "━" * 65
    print(f"\n{bar}")
    print("Backend ready. Run (from terraform/, or 'make plan'/'make apply' from repo root):")
    print("  cd terraform && terraform plan")
    print("  cd terraform && terraform apply")
    print(bar)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"command failed ({e.returncode}): {' '.join(str(a) for a in e.cmd)}")
