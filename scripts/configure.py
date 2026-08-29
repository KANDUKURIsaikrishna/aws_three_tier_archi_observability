#!/usr/bin/env python3
"""
configure.py — Stamp real values (domain, AWS account, GitHub repo/branch,
region) into every project file that needs them.

Run after cloning, and again any time you change config.env -- safe to
re-run any number of times. Substitution is regex-anchored to each field's
known shape, not a one-shot placeholder swap, so it always re-stamps the
CURRENT config.env values even over a real value a previous run already
stamped in with different settings. (An earlier version matched only the
literal placeholder text -- once that was replaced once, a later domain/
account/repo change in config.env silently had nowhere left to land: the
script reported "already configured" while the deployed files quietly kept
serving the old value.)

In CI the values come from GitHub Secrets automatically -- this script is
for local development / first-time setup only.
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


def substitute(rel_path: str, pattern: re.Pattern, replacement: str):
    p = REPO_ROOT / rel_path
    if not p.exists():
        print(f"  SKIP  {rel_path}  (file not found)")
        return
    text = p.read_text(encoding="utf-8")
    new_text, count = pattern.subn(replacement, text)
    if count == 0:
        print(f"  !!  {rel_path}  (expected pattern not found -- check this file by hand)")
    elif new_text != text:
        p.write_text(new_text, encoding="utf-8")
        print(f"  [ok]  {rel_path}")
    else:
        print(f"  --{rel_path}  (already up to date)")


def substitute_glob(glob_pattern: str, pattern: re.Pattern, replacement: str):
    matched_any = False
    for p in sorted(REPO_ROOT.glob(glob_pattern)):
        matched_any = True
        substitute(str(p.relative_to(REPO_ROOT)), pattern, replacement)
    if not matched_any:
        print(f"  SKIP  {glob_pattern}  (no files matched)")


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

    account_id       = cfg["AWS_ACCOUNT_ID"]
    region           = cfg["AWS_REGION"]
    domain           = cfg["DOMAIN"]
    github_repo      = cfg["GITHUB_REPO"]
    alert_email      = cfg["ALERT_EMAIL"]
    github_branch    = cfg.get("GITHUB_BRANCH") or "main"
    secondary_region = cfg.get("SECONDARY_REGION") or "us-west-2"

    print(f"\nConfiguring project with:")
    print(f"  Account    : {account_id}")
    print(f"  Region     : {region}")
    print(f"  DR Region  : {secondary_region}")
    print(f"  Domain     : {domain}")
    print(f"  Repo       : {github_repo}")
    print(f"  Branch     : {github_branch}")
    print(f"  Alerts     : {alert_email}")
    print()

    # ── 1. terraform/terraform.tfvars ────────────────────────────────────────
    # Fully regenerated every run -- no idempotency ambiguity possible here,
    # which is exactly the property the substitutions below now match too.
    # secondary_region only matters if enable_dr_replication=true elsewhere
    # (off by default) -- written unconditionally anyway since it matches
    # variables.tf's own "us-west-2" default when SECONDARY_REGION is unset,
    # so this is a no-op for anyone not using DR.
    tfvars = REPO_ROOT / "terraform" / "terraform.tfvars"
    tfvars.write_text(
        f'aws_region       = "{region}"\n'
        f'secondary_region = "{secondary_region}"\n'
        f'domain           = "{domain}"\n'
        f'github_repo      = "{github_repo}"\n'
        f'alert_email      = "{alert_email}"\n',
        encoding="utf-8",
    )
    print(f"  [ok]  terraform/terraform.tfvars  (generated)")

    # ── 2. Ingress hostnames: bookstore.<domain>, api.bookstore.<domain> ─────
    # Matches "bookstore." followed by whatever's currently there (the
    # original YOUR_DOMAIN_HERE.com placeholder, or a real domain an earlier
    # run already stamped in) -- so a domain change in config.env gets
    # re-stamped too, not just the very first run. [A-Za-z0-9._-] (with the
    # underscore) so it matches the YOUR_DOMAIN_HERE.com placeholder itself,
    # not just real domains. Scoped to these specific files rather than a
    # repo-wide scan: k8s/base/monitoring/prometheus-rules.yaml's
    # PrometheusRule group names ("bookstore.pods", "bookstore.http") would
    # false-positive-match a looser search.
    domain_pattern = re.compile(r"bookstore\.[A-Za-z0-9._-]+")
    for rel_path in [
        "k8s/base/ingress/ingress.yaml",
        "k8s/services/api-gateway/base/ingress.yaml",
        "k8s/services/api-gateway/base/configmap.yaml",
    ]:
        substitute(rel_path, domain_pattern, f"bookstore.{domain}")

    # ── 3. ECR image references (<account_id>.dkr.ecr.<region>.amazonaws.com) ─
    # One per service's overlays/prod/kustomization.yaml. CI overwrites these
    # on every push via `kustomize edit set image`, so this only matters for
    # a local apply before CI has ever run for this account. Globbed (not a
    # fixed file list) so a new microservice's kustomization.yaml is picked
    # up automatically instead of silently being skipped. Region is part of
    # this match too, not just the account -- a region change in config.env
    # needs to land here as well, not only in terraform.tfvars.
    ecr_pattern = re.compile(r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com")
    substitute_glob(
        "k8s/**/overlays/prod/kustomization.yaml",
        ecr_pattern,
        f"{account_id}.dkr.ecr.{region}.amazonaws.com",
    )
    # DR overlays (var.enable_dr_standby) pull from the us-west-2 ECR replica,
    # not the primary region -- same account, different region in the URL, so
    # this can't reuse the substitute_glob call above. Only present on the dr
    # branch (k8s/**/overlays/dr/ doesn't exist on main) -- substitute_glob's
    # own "no files matched" SKIP line makes this a harmless no-op there.
    substitute_glob(
        "k8s/**/overlays/dr/kustomization.yaml",
        ecr_pattern,
        f"{account_id}.dkr.ecr.{secondary_region}.amazonaws.com",
    )

    # ── 4. ArgoCD repoURL / sourceRepos (https://github.com/<repo>.git) ──────
    # All three k8s/argocd/*.yaml files reference the repo. appproject.yaml's
    # sourceRepos allowlist and applicationset-microservices.yaml's repoURL
    # both have to match application.yaml's, or ArgoCD rejects the
    # Application/ApplicationSet at admission for naming a repo outside its
    # AppProject's allowlist. k8s/argocd/dr/*.yaml (var.enable_dr_standby,
    # dr branch only) needs the exact same repoURL/sourceRepos stamp -- a
    # separate glob rather than widening the pattern above to `**`, so a
    # missing dr/ directory on main stays a clean SKIP instead of silently
    # changing what the primary glob matches.
    repo_pattern = re.compile(r"https://github\.com/[^\s/]+/[^\s.]+\.git")
    substitute_glob("k8s/argocd/*.yaml", repo_pattern, f"https://github.com/{github_repo}.git")
    substitute_glob("k8s/argocd/dr/*.yaml", repo_pattern, f"https://github.com/{github_repo}.git")

    # ── 5. ArgoCD targetRevision (the branch ArgoCD deploys from) ────────────
    # Only application.yaml and applicationset-microservices.yaml have a
    # source.targetRevision field -- appproject.yaml doesn't, so this is an
    # explicit file list rather than the k8s/argocd/*.yaml glob used above
    # (globbing it would print a spurious "pattern not found" warning for
    # appproject.yaml every run). A value copied over from a different
    # repo/branch layout than the one actually deployed breaks ArgoCD sync
    # outright ("unable to resolve '<branch>' to a commit SHA") if that
    # branch doesn't exist in the real repo -- config-driven, not hardcoded,
    # for exactly that reason. Same two dr/ counterparts, same reasoning --
    # missing on main, so `substitute`'s own SKIP-if-not-found is enough,
    # no separate existence check needed.
    branch_pattern = re.compile(r"targetRevision: \S+")
    for rel_path in [
        "k8s/argocd/application.yaml",
        "k8s/argocd/applicationset-microservices.yaml",
        "k8s/argocd/dr/application.yaml",
        "k8s/argocd/dr/applicationset-microservices.yaml",
    ]:
        substitute(rel_path, branch_pattern, f"targetRevision: {github_branch}")

    # ── 6. ClusterSecretStore region ──────────────────────────────────────────
    # Every microservice's ExternalSecret references this one
    # ClusterSecretStore by name, so a wrong region here breaks secret sync
    # cluster-wide, not just one service.
    region_pattern = re.compile(r"region: [a-zA-Z0-9_-]+")
    substitute("k8s/base/secrets/external-secret.yaml", region_pattern, f"region: {region}")

    # ── 7. DR standby overlay's ClusterSecretStore region ─────────────────────
    # k8s/overlays/dr/kustomization.yaml (var.enable_dr_standby, dr branch
    # only) patches the base ClusterSecretStore to read Secrets Manager in
    # the SECONDARY region instead -- the standby cluster's ExternalSecrets
    # pull the primary's cross-region-replicated secrets from
    # secondary_region, not region. Only the kustomize patch's `value:` line
    # matches this pattern in that file.
    dr_region_pattern = re.compile(r"value: [a-zA-Z0-9_-]+")
    substitute("k8s/overlays/dr/kustomization.yaml", dr_region_pattern, f"value: {secondary_region}")

    print(f"""
Done. Commit and push the stamped k8s files so ArgoCD deploys the real
values, not whatever was there before (it syncs from git, not this local
checkout):

     git add k8s/
     git commit -m "chore: configure for {domain}"
     git push

Then follow docs/DEPLOYMENT.md starting at Step 1 (this script was Step 1's
own command) for the actual apply sequence -- Terraform state bootstrap,
the apply itself, and the post-apply verification steps.

Note: config.env and terraform/terraform.tfvars are gitignored -- never
commit them. This is a config-drift script, not a one-time fix: re-run it
any time config.env changes (new domain, new AWS account, new repo, new
branch), and remember to commit + push the k8s/ changes it makes -- ArgoCD
only ever syncs from git, never from local disk.
""")


if __name__ == "__main__":
    main()
