#!/usr/bin/env python3
"""
build_and_push.py -- build every service image and push to ECR.

Builds the React frontend (client/) plus all 5 backend microservices
(services/*) and pushes them to their `bookstore-<name>` ECR repos. This is
the manual equivalent of what CI does on every push -- useful for a
first-ever deploy before any CI run has landed (see docs/DEPLOYMENT.md
Step 8). There is no "bookstore-backend" monolith image anymore.

Pure Python 3 stdlib + the `aws` and `docker` CLIs on PATH -- identical on
Windows, macOS and Linux.

Usage:
  python3 scripts/build_and_push.py <AWS_ACCOUNT_ID> <AWS_REGION> <IMAGE_TAG> [REACT_APP_API_URL]

Example:
  python3 scripts/build_and_push.py 123456789012 us-west-1 v1.0.0 https://api.bookstore.example.com
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

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


def main() -> None:
    if len(sys.argv) < 4:
        die("usage: python3 scripts/build_and_push.py "
            "<AWS_ACCOUNT_ID> <AWS_REGION> <IMAGE_TAG> [REACT_APP_API_URL]")

    aws = tool("aws")
    docker = tool("docker")

    account_id = sys.argv[1]
    region = sys.argv[2]
    tag = sys.argv[3]
    api_url = sys.argv[4] if len(sys.argv) > 4 else "http://localhost:3000"

    registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

    print(f"Registry : {registry}")
    print(f"Tag      : {tag}")
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
    frontend_image = f"{registry}/{FRONTEND[0]}:{tag}"
    print(f"\n==> Building frontend ({frontend_image})...")
    run([docker, "build",
         "--build-arg", f"REACT_APP_API_URL={api_url}",
         "-t", frontend_image,
         str(REPO_ROOT / FRONTEND[1])])

    service_images = []
    for repo, ctx in SERVICES:
        image = f"{registry}/{repo}:{tag}"
        service_images.append(image)
        print(f"\n==> Building {repo} ({image})...")
        run([docker, "build", "-t", image, str(REPO_ROOT / ctx)])

    # -- Push --
    print(f"\n==> Pushing frontend...")
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
