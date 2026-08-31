#!/usr/bin/env python3
"""
build_and_push.py -- build every service image and push to ECR.

Builds the React frontend (client/) plus all 5 backend microservices
(services/*) and pushes them to their `bookstore-<name>` ECR repos. This is
the manual equivalent of what CI does on every push -- useful for a
first-ever deploy before any CI run has landed (see docs/DEPLOYMENT.md
Step 8). There is no "bookstore-backend" monolith image anymore.

Everything except the image tag comes from config.env (AWS_ACCOUNT_ID,
AWS_REGION, DOMAIN) -- nothing hardcoded. Each value can still be
overridden with a flag for a one-off.

Pure Python 3 stdlib + the `aws` and `docker` CLIs on PATH -- identical on
Windows, macOS and Linux.

Usage:
  python3 scripts/build_and_push.py <IMAGE_TAG>
  python3 scripts/build_and_push.py v1.0.0 --account-id 123456789012 --region us-west-1
  python3 scripts/build_and_push.py v1.0.0 --api-url https://api.bookstore.example.com
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV = REPO_ROOT / "config.env"

# (ECR repo suffix, build context dir relative to repo root)
FRONTEND = ("bookstore-frontend", "client")
SERVICES = [
    ("bookstore-catalog-service", "services/catalog-service"),
    ("bookstore-user-service", "services/user-service"),
    ("bookstore-order-service", "services/order-service"),
    ("bookstore-notification-service", "services/notification-service"),
    ("bookstore-api-gateway", "services/api-gateway"),
]


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        die(f"`{name}` not found on PATH -- install it and re-run.")
    return found


def run(argv, **kw):
    return subprocess.run(argv, check=True, **kw)


def read_config() -> dict:
    """Parse config.env into a dict -- same shape scripts/configure.py reads."""
    cfg = {}
    if CONFIG_ENV.is_file():
        for raw in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build + push all bookstore images to ECR.")
    ap.add_argument("tag", help="image tag (e.g. a git SHA or v1.2.0)")
    ap.add_argument("--account-id", help="override AWS_ACCOUNT_ID from config.env")
    ap.add_argument("--region", help="override AWS_REGION from config.env")
    ap.add_argument("--api-url",
                    help="override REACT_APP_API_URL "
                         "(default: https://api.bookstore.<DOMAIN>)")
    args = ap.parse_args()

    aws = tool("aws")
    docker = tool("docker")
    cfg = read_config()

    account_id = args.account_id or cfg.get("AWS_ACCOUNT_ID")
    region = args.region or cfg.get("AWS_REGION")
    if not account_id:
        die("AWS_ACCOUNT_ID not in config.env and --account-id not given.")
    if not region:
        die("AWS_REGION not in config.env and --region not given.")

    api_url = args.api_url
    if not api_url:
        domain = cfg.get("DOMAIN")
        if not domain:
            die("DOMAIN not in config.env and --api-url not given.")
        api_url = f"https://api.bookstore.{domain}"

    registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

    print(f"Registry : {registry}")
    print(f"Tag      : {args.tag}")
    print(f"API URL  : {api_url}\n")

    # -- Authenticate to ECR (pipe get-login-password -> docker login --password-stdin) --
    print("==> Authenticating to ECR...")
    pw = subprocess.run(
        [aws, "ecr", "get-login-password", "--region", region],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout
    run([docker, "login", "--username", "AWS", "--password-stdin", registry],
        input=pw, text=True)

    # -- Build --
    frontend_image = f"{registry}/{FRONTEND[0]}:{args.tag}"
    print(f"\n==> Building frontend ({frontend_image})...")
    run([docker, "build",
         "--build-arg", f"REACT_APP_API_URL={api_url}",
         "-t", frontend_image,
         str(REPO_ROOT / FRONTEND[1])])

    service_images = []
    for repo, ctx in SERVICES:
        image = f"{registry}/{repo}:{args.tag}"
        service_images.append(image)
        print(f"\n==> Building {repo} ({image})...")
        run([docker, "build", "-t", image, str(REPO_ROOT / ctx)])

    # -- Push --
    print("\n==> Pushing frontend...")
    run([docker, "push", frontend_image])
    for image in service_images:
        print(f"==> Pushing {image}...")
        run([docker, "push", image])

    print("\nDone.")
    for image in [frontend_image, *service_images]:
        print(f"  {image}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"command failed ({e.returncode}): {' '.join(str(a) for a in e.cmd)}")
