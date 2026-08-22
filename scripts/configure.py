#!/usr/bin/env python3
"""
configure.py — Stamp real values into all project files that need them.

Run once after cloning, or any time you change config.env:
    python3 scripts/configure.py

Substitution is regex-anchored on each value's *shape* (domain host,
12-digit ECR account, GitHub clone URL, AWS region), not a literal
placeholder string. That matters on a re-run: once a placeholder is
replaced with a real value, a literal-string replace finds no placeholder
left to match on the next run, so a later config.env change (new domain,
new account, new repo) would silently leave the file serving the OLD real
value forever, no error, no warning. Matching by shape means the current
value gets found and replaced regardless of whether it's still the
original placeholder or a real value a previous run already wrote.

In CI the values come from GitHub Secrets automatically — this script is for
local development / first-time setup only.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    cfg = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


def substitute(rel_path: str, pattern: str, replacement: str):
    p = REPO_ROOT / rel_path
    if not p.exists():
        print(f"  SKIP  {rel_path} (file not found)")
        return
    text = p.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, replacement, text)
    if count:
        p.write_text(new_text, encoding="utf-8")
        plural = "es" if count != 1 else ""
        print(f"  [ok]  {rel_path}  ({count} match{plural})")
    else:
        print(f"  --{rel_path}  (already configured)")


def main():
    config_path = REPO_ROOT / "config.env"
    if not config_path.exists():
        sys.exit(
            "\nERROR: config.env not found.\n"
            "  1. cp config.env.example config.env\n"
            "  2. Fill in your real values\n"
            "  3. Re-run this script\n"
        )

    cfg = load_config(config_path)

    required = ["AWS_ACCOUNT_ID", "AWS_REGION", "DOMAIN", "GITHUB_REPO", "ALERT_EMAIL"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"\nERROR: Missing values in config.env: {', '.join(missing)}\n")

    account_id  = cfg["AWS_ACCOUNT_ID"]
    region      = cfg["AWS_REGION"]
    domain      = cfg["DOMAIN"]
    github_repo = cfg["GITHUB_REPO"]
    alert_email = cfg["ALERT_EMAIL"]

    print(f"\nConfiguring project with:")
    print(f"  Account : {account_id}")
    print(f"  Region  : {region}")
    print(f"  Domain  : {domain}")
    print(f"  Repo    : {github_repo}")
    print(f"  Alerts  : {alert_email}")
    print()

    # ── 1. terraform/terraform.tfvars ────────────────────────────────────────
    tfvars = REPO_ROOT / "terraform" / "terraform.tfvars"
    tfvars.write_text(
        f'aws_region  = "{region}"\n'
        f'domain      = "{domain}"\n'
        f'github_repo = "{github_repo}"\n'
        f'alert_email = "{alert_email}"\n',
        encoding="utf-8",
    )
    print(f"  [ok]  terraform/terraform.tfvars  (generated)")

    # ── 2. Domain — bookstore.<domain> / api.bookstore.<domain> convention ──
    # Explicit file list, NOT a repo-wide scan: k8s/base/monitoring/
    # prometheus-rules.yaml has PrometheusRule group names like
    # "bookstore.pods" and "bookstore.http" that would false-positive-match
    # a looser search across every k8s/**/*.yaml file.
    domain_pattern = r"bookstore\.[A-Za-z0-9.-]+"
    for rel_path in [
        "k8s/base/ingress/ingress.yaml",
        "k8s/services/api-gateway/base/ingress.yaml",
        "k8s/services/api-gateway/base/configmap.yaml",
    ]:
        substitute(rel_path, domain_pattern, f"bookstore.{domain}")

    # ── 3. ECR account ID — every overlays/prod/kustomization.yaml ──────────
    # CI (deploy stage) overwrites the `newName` image field from
    # secrets.AWS_ACCOUNT_ID on every push anyway — this only matters for a
    # manual local apply before CI has run once.
    account_pattern = r"\d{12}(?=\.dkr\.ecr\.)"
    for path_obj in sorted(REPO_ROOT.glob("k8s/**/overlays/prod/kustomization.yaml")):
        substitute(str(path_obj.relative_to(REPO_ROOT)), account_pattern, account_id)

    # ── 4. GitHub repo URL — every k8s/argocd/*.yaml ────────────────────────
    repo_pattern = r"https://github\.com/[^\s/]+/[^\s.]+\.git"
    for path_obj in sorted(REPO_ROOT.glob("k8s/argocd/*.yaml")):
        substitute(str(path_obj.relative_to(REPO_ROOT)), repo_pattern, f"https://github.com/{github_repo}.git")

    # ── 5. Region — the shared ClusterSecretStore's own region field ───────
    # Every service's own ExternalSecret references this one ClusterSecretStore
    # by name, so a wrong region here breaks secret sync cluster-wide.
    region_pattern = r"(?<=region: )\S+"
    substitute("k8s/base/secrets/external-secret.yaml", region_pattern, region)

    print(f"""
Done. Commit and push the stamped k8s files so ArgoCD deploys the real
values, not whatever was there before (it syncs from git, not this local
checkout):

     git add k8s/base/ingress/ingress.yaml k8s/services/api-gateway/base/ingress.yaml \\
             k8s/services/api-gateway/base/configmap.yaml \\
             k8s/argocd/application.yaml k8s/argocd/appproject.yaml \\
             k8s/argocd/applicationset-microservices.yaml \\
             k8s/overlays/prod/kustomization.yaml \\
             k8s/services/*/overlays/prod/kustomization.yaml \\
             k8s/base/secrets/external-secret.yaml
     git commit -m "chore: configure for {domain}"
     git push

Then follow docs/DEPLOYMENT.md starting at Step 1 (this script was Step 1's
own command) for the actual apply sequence -- Terraform state bootstrap,
the apply itself, and the post-apply verification steps.

Note: config.env and terraform/terraform.tfvars are gitignored -- never
commit them. This is a config-drift script, not a one-time fix: re-run it
any time config.env changes (new domain, new AWS account, new repo), and
remember to commit + push the k8s/ changes it makes -- ArgoCD only ever
syncs from git, never from local disk.
""")


if __name__ == "__main__":
    main()
