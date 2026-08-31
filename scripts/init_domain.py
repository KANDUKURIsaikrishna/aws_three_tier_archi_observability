#!/usr/bin/env python3
"""
init_domain.py -- public Route53 hosted-zone bootstrap. DEPLOYMENT.md Step 3.

Run ONCE per domain, ever -- not once per apply/destroy cycle.

Terraform (modules/route53) reads the public hosted zone via a
`data "aws_route53_zone"` lookup; it does NOT create or destroy it. That's
deliberate: a Route53 zone gets 4 brand-new, randomly-assigned nameservers
every time it's created, so if Terraform owned it, every `terraform destroy`
+ fresh `apply` would silently break the registrar delegation until someone
noticed the domain stopped resolving (see docs/TROUBLESHOOTING.md OBS-058).

What it does:
  1. Reads DOMAIN from config.env (or an explicit CLI arg)
  2. Creates the public Route53 hosted zone if it doesn't already exist
     (idempotent -- safe to re-run any time, e.g. after a fresh checkout)
  3. Prints the zone's 4 NS values for you to set at your registrar

Pure Python 3 stdlib + the `aws` CLI on PATH -- identical on Windows,
macOS and Linux.

Usage:
  python3 scripts/init_domain.py [domain]
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV = REPO_ROOT / "config.env"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        die(f"`{name}` not found on PATH -- install it and re-run.")
    return found


def capture(argv) -> str:
    return subprocess.run(
        argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ).stdout.strip()


def read_config_domain() -> str:
    if not CONFIG_ENV.is_file():
        return ""
    domain = ""
    for raw in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("DOMAIN="):
            domain = line.partition("=")[2].strip().strip('"').strip("'")
    return domain


def main() -> None:
    aws = tool("aws")

    domain = sys.argv[1] if len(sys.argv) > 1 else read_config_domain()
    if not domain:
        die("Usage: python3 scripts/init_domain.py <domain>  (or set DOMAIN in config.env)")

    print(f"\nDomain : {domain}\n")

    # list-hosted-zones-by-name can also return zones for unrelated
    # parent/child domains that sort near this name; filter strictly on an
    # exact Name match ("<domain>." with Route53's trailing dot) and
    # PrivateZone == false -- the private RDS zone shares the account and
    # could otherwise collide by name.
    existing = capture([
        aws, "route53", "list-hosted-zones-by-name",
        "--dns-name", domain,
        "--query",
        f"HostedZones[?Name=='{domain}.' && Config.PrivateZone==`false`].Id | [0]",
        "--output", "text",
    ])

    if existing and existing != "None":
        print(f"[skip] Public hosted zone already exists: {existing}")
        zone_id = existing
    else:
        print("[create] Public hosted zone...")
        zone_id = capture([
            aws, "route53", "create-hosted-zone",
            "--name", domain,
            "--caller-reference", f"init-domain-{int(time.time())}",
            "--query", "HostedZone.Id",
            "--output", "text",
        ])
        print(f"[ok] Created {zone_id}")

    name_servers = capture([
        aws, "route53", "get-hosted-zone",
        "--id", zone_id,
        "--query", "DelegationSet.NameServers",
        "--output", "text",
    ]).split()

    bar = "━" * 65
    print(f"\n{bar}")
    print("One-time step: set these 4 nameservers at your domain registrar")
    print(f"(GoDaddy, Namecheap, etc. -- wherever {domain} is registered):")
    print(bar)
    for ns in name_servers:
        print(f"  {ns}")
    print()
    print("Propagation to public resolvers can take anywhere from a few minutes to")
    print("longer, depending on the registrar and previous record TTLs. Do this")
    print("BEFORE running 'terraform apply' for the ingress/ACM layer, not after --")
    print("otherwise the apply will sit on 'aws_acm_certificate_validation.ingress:")
    print("Still creating...' until it does.")
    print()
    print("Once set, this is a one-time step for this domain -- future")
    print("terraform apply / terraform destroy cycles never touch this zone.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"command failed ({e.returncode}): {' '.join(str(a) for a in e.cmd)}")
