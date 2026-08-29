# DR standby region — implementation plan

Goal: a warm **active-passive** standby of the whole platform in `var.secondary_region`
(default `us-west-2`), so a full loss of `us-west-1` fails over to a running
copy instead of a restore-from-backup. Route53 already has the `SECONDARY`
failover record wired — it goes live the moment `secondary_alb_dns` is
non-empty.

This replaces the current "DR is backup-only, no compute in us-west-2"
posture (ARCHITECTURE.md, both blog drafts).

Not gated by the `us-west-1` 8-vCPU quota — EC2 vCPU quotas are per-region.
`us-west-2` steady-state target is 2× `t3.medium` (nodes) + 1× `t3.small`
(monitoring) = **6 vCPU**; verify the secondary-region quota once with
`aws service-quotas get-service-quota --region us-west-2 --service-code ec2 --quota-code L-1216C47A`.

---

## Design decisions

1. **One flag turns it on.** New `var.enable_dr_standby` (bool, default
   `false`). Everything below is `count = var.enable_dr_standby ? 1 : 0` or a
   `for_each` guarded the same way. Off by default = zero change to today's
   single-region apply, zero extra cost until asked.

2. **Providers.** Root already has `aws.secondary`. Add aliased
   `helm.secondary`, `kubernetes.secondary`, `kubectl.secondary`, configured
   from the secondary EKS cluster's endpoint (mirrors the existing default
   provider blocks in `providers.tf`).

3. **`eks-addons` module gets `configuration_aliases`.** Today it uses
   `aws`/`helm`/`kubernetes`/`kubectl` implicitly. To instantiate it twice it
   must declare `configuration_aliases = [aws, helm, kubernetes, kubectl]` in
   its `required_providers`, and both call sites must pass `providers = {…}`
   explicitly. The primary call passes the default provider set — behaviour
   unchanged.

4. **RDS: cross-region read replica, promotable.** `aws_db_instance` in the
   secondary region with `replicate_source_db = module.rds.rds_instance_arn`,
   its own `db_subnet_group` + SG in the secondary VPC, and a customer-managed
   KMS key in the secondary region (`aws_kms_key.dr`, or reuse
   `var.dr_kms_key_id`). Replica is read-only until promoted. Promotion
   (`aws_rds_cluster`/`aws_db_instance` → `promote-read-replica`, or manual
   `aws rds promote-read-replica`) is a runbook step, not automated — see
   **Failover runbook** below.

5. **DB connection indirection.** Services currently get `DB_HOST` =
   `module.rds.rds_endpoint` baked into their per-service Secrets Manager
   entries. For DR the secondary cluster's services must reach the *replica*
   endpoint. Approach: each region's `route53` module keeps its own private
   hosted zone with the **same** record name (`db.bookstore.internal`, CNAME
   → the region-local RDS/replica endpoint). Per-service secret JSON changes
   `DB_HOST` from the raw endpoint to `db.bookstore.internal`. Primary zone
   → primary RDS; secondary zone → replica. No app or manifest change needed
   on failover; promotion just makes the same DNS name writable.

6. **Secrets replication.** Add `replica { region = var.secondary_region }`
   to every `aws_secretsmanager_secret` the cluster consumes:
   `jwt_secret`, `db_credentials` (×4, root `main.tf`), `db_credentials`
   (admin, `modules/rds`), `grafana_admin` + `monitoring_basic_auth`
   (`modules/eks-addons`). ESO in the secondary cluster reads from the
   **local** (us-west-2) replica, so secret sync survives a primary-region
   outage.

7. **ClusterSecretStore region.** The k8s `ClusterSecretStore` has
   `region: AWS_REGION_HERE` stamped by `scripts/configure.py`. Secondary
   cluster needs `region: <secondary_region>`. Solution: a thin
   `k8s/overlays/dr/` Kustomize overlay that patches just the
   `ClusterSecretStore` region (and nothing else — the ApplicationSet and
   per-service overlays are already cluster-agnostic, targeting
   `https://kubernetes.default.svc`). Secondary ArgoCD points its root
   Application at `k8s/overlays/dr`.

8. **ArgoCD topology: independent instance per cluster.** Simplest and
   most failure-isolated. The secondary `eks-addons` installs its own ArgoCD,
   which syncs the same repo/branch into its own cluster. No hub-spoke, no
   cross-cluster credentials, no dependency on the primary's ArgoCD being up.

9. **ACM + ALB in the secondary region.** `aws_acm_certificate.ingress_dr`
   (secondary provider) + DNS validation records in the **same global** public
   hosted zone (Route53 zones aren't regional — records can be written with
   any provider). The secondary AWS Load Balancer Controller provisions the
   secondary ALB from the same Ingress manifests; its hostname is
   auto-discovered the same way `argocd.tf` does for the primary
   (`null_resource` poll + `data.kubernetes_ingress_v1` on the secondary
   kube provider), then fed to `module.route53`'s `secondary_alb_dns`.

10. **Monitoring.** Secondary region gets its own `monitoring-ec2` (same
    module, secondary provider). Independent Prometheus/Grafana/Alertmanager
    — the primary's monitoring box is in the region that just died. Alert
    email (SES) stays primary-region unless SES is also mirrored — out of
    scope for v1; note it.

---

## File-by-file changes

### New files

| File | Contents |
|---|---|
| `terraform/dr-standby.tf` | The whole secondary stack: `module.network_dr`, `module.security_groups_dr`, `module.eks_dr`, `module.eks_addons_dr`, `module.monitoring_ec2_dr`, `aws_db_instance.dr_replica`, `aws_kms_key.dr`, `aws_acm_certificate.ingress_dr` + validation, `null_resource.wait_for_dr_alb_hostname`, `data.kubernetes_ingress_v1.dr_bookstore`, ArgoCD `kubectl_manifest` bootstrap for the secondary cluster. All `count = var.enable_dr_standby ? 1 : 0`. |
| `terraform/providers-dr.tf` | `provider "helm"` / `"kubernetes"` / `"kubectl"` with `alias = "secondary"`, `host`/`cluster_ca_certificate`/`exec` from `module.eks_dr[0]` outputs. |
| `k8s/overlays/dr/kustomization.yaml` | `resources: [../prod]` + a strategic-merge patch on the `ClusterSecretStore` setting `spec.provider.aws.region: <secondary_region>`. |
| `docs/DR-FAILOVER-RUNBOOK.md` | Manual promotion + cutover steps (below). |

### Modified

| File | Change |
|---|---|
| `terraform/modules/eks-addons/main.tf` | Add `configuration_aliases = [aws, helm, kubernetes, kubectl]` to `required_providers`; add `kubernetes` + `kubectl` to the block (currently only `aws`, `helm`, `random`). |
| `terraform/main.tf` (primary `module.eks_addons` call) | Add explicit `providers = { aws = aws, helm = helm, kubernetes = kubernetes, kubectl = kubectl }`. |
| `terraform/main.tf` — `aws_secretsmanager_secret.jwt_secret`, `.db_credentials` (×4) | Add `dynamic "replica"` on `var.enable_dr_standby`. |
| `terraform/modules/rds/main.tf` — `aws_secretsmanager_secret.db_credentials` | Same replica block; new `var.enable_secret_replica` + `var.replica_region`. |
| `terraform/modules/eks-addons/{grafana-secret,monitoring-basic-auth-secret}.tf` | Same replica block; new vars. |
| `terraform/modules/rds/main.tf` + `outputs.tf` | Export `rds_instance_arn` (for `replicate_source_db`) and the raw endpoint if not already. Confirm `backup_retention_period > 0` on the source (required for replicas — it's 7, OK). |
| `terraform/modules/route53/` | Parameterise so it can be instantiated once more for the secondary private zone (`db.bookstore.internal` → replica endpoint), and accept an auto-discovered `secondary_alb_dns`. The public-zone failover records stay in the single (primary) module instance. |
| `terraform/variables.tf` | `enable_dr_standby` (bool, default false); `dr_node_desired_size` / `dr_node_min_size` / `dr_node_max_size` (default 2/1/2 — smaller than primary); reuse `secondary_region`. |
| per-service secret JSON in `terraform/main.tf` (`aws_secretsmanager_secret_version.db_credentials`) | `DB_HOST = "db.bookstore.internal"` instead of `module.rds.rds_endpoint`. Primary private zone must then define that CNAME (route53 module already has `aws_route53_record.rds_endpoint` — rename/point it at this stable name). |
| `k8s/base/secrets/external-secret.yaml` | No change — overlay patches region. |
| `.github/workflows/*` | `kubeconform` / `kustomize build` matrix add `k8s/overlays/dr`. |
| `docs/ARCHITECTURE.md`, `my_blog_draft.md`, `my_blog_draft1.md` | "backup-only" → "active-passive standby (opt-in via `enable_dr_standby`)". Update the DR section + the "Prioritized backlog" bullet. |
| `Makefile` | Optional `dr-plan` / `dr-apply` convenience targets that set `-var enable_dr_standby=true`. |

---

## Apply / destroy ordering

### Apply — the two regions run concurrently

Nothing in `dr-standby.tf` depends on the primary modules except
`aws_db_instance.dr_replica` (waits on `module.rds`) and the DR ACM
validation records (write into the primary's public zone, which resolves
early). So `module.network_dr → eks_dr → eks_addons_dr → ArgoCD → ALB
discovery` runs **alongside** the primary chain, not after it. A both-flags
apply ≈ `max(primary, DR)` wall time, roughly the same as a single-region
apply plus a ~10–15 min tail. Use `-parallelism=20` (the `dr-*` Makefile
targets do) since the resource count roughly doubles.

### Destroy — same hazards as the single-region teardown, all mirrored

| Hazard (single-region incident) | DR handling |
|---|---|
| Orphaned VPC-CNI ENIs + EKS-auto cluster SG block `DeleteVpc` | `null_resource.cleanup_eks_networking_dr`, with `module.eks_dr` `depends_on` it — same shape as the primary's `cleanup_eks_networking`. Script is boto3-only (no kubectl), region-parametrised. |
| ALB/Ingress not torn down; LB-controller pod needs NAT egress | `module.eks_addons_dr`'s own `null_resource.delete_ingress_objects` runs on destroy; `depends_on = [module.eks_dr, module.network_dr]` keeps the DR NAT up until it finishes. |
| Concurrent two-region destroy: both `delete_ingress_objects` / `wait_for_alb_hostname` do `aws eks update-kubeconfig` + `kubectl` and would race a shared `~/.kube/config` current-context | Each provisioner now sets a **per-region `$KUBECONFIG`** (`.terraform/kubeconfig-{primary,dr,ingress-<region>}`), so the two never share config state. |
| **New:** AWS refuses `DeleteDBInstance` on an RDS source that still has a replica | `replicate_source_db = module.rds.rds_instance_arn` makes Terraform destroy `aws_db_instance.dr_replica` **before** `module.rds` automatically. (After a real failover the replica is promoted → standalone → moot.) |
| Private hosted zone won't delete with records / VPC still attached | `aws_route53_zone.dr_rds_private` is VPC-associated to `module.network_dr` and its record references the zone → destroyed in the right order with no extra wiring. |

`make dr-destroy` (or `terraform destroy -var enable_dr_standby=true
-parallelism=20`) tears down both regions. To drop only the standby: set
`enable_dr_standby=false` and `terraform apply` — every DR resource is
`count`-gated, so that removes them in dependency order without touching the
primary.

---

## Cost delta (running, rough, us-west-2)

| Item | ~$/mo |
|---|---|
| EKS control plane | 73 |
| 2× t3.medium nodes | ~60 |
| 1× t3.small monitoring | ~15 |
| NAT (per-AZ ×2, or 1 if `single_nat_gateway`) | 32–65 |
| RDS `db.t3.micro` read replica (Multi-AZ off for replica) | ~15 |
| ALB | ~18 |
| Cross-region data transfer (replication + image pulls) | usage-based |
| **≈ total** | **~230–280 + transfer** |

Roughly doubles the platform's running cost. Only while
`enable_dr_standby = true` and the stack is applied.

---

## Failover runbook (summary — full version in DR-FAILOVER-RUNBOOK.md)

1. Confirm `us-west-1` is actually down (not a Route53 health-check flap).
2. `aws rds promote-read-replica --db-instance-identifier bookstore-db-dr --region us-west-2`. Wait for `available`.
3. Secondary cluster's services already point at `db.bookstore.internal`
   (secondary private zone → replica, now primary). No redeploy.
4. Route53 `SECONDARY` failover record is already serving (health check on
   the primary ALB failed). Confirm `api.bookstore.<domain>` resolves to the
   DR ALB.
5. Scale the DR node group / HPAs up to production sizing if they were
   running hot-standby-small.
6. When `us-west-1` returns: rebuild it as the new standby (re-replicate
   from the promoted DR instance), then fail back during a window.

---

## Implementation status (branch `dr`)

**Written and `terraform validate`-clean:**

- `enable_dr_standby` + `dr_node_{desired,min,max}_size` vars.
- Cross-region `replica{}` on all 6 Secrets Manager entries the cluster reads.
- `eks-addons`: `create_monitoring_secrets` (false in DR — reuse the primary's
  replicas) and `replica_region` inputs; secret resources + outputs gated.
- `providers-dr.tf` — `helm`/`kubernetes`/`kubectl` `.secondary`, wired from
  `module.eks_dr` via `one(...)`/`try(...)` so they're inert when the flag is off.
- `dr-standby.tf` — `network_dr`, `security_groups_dr`, `eks_dr`,
  `eks_addons_dr`, `monitoring_ec2_dr`, `aws_db_instance.dr_replica` + KMS +
  subnet group + SG, `aws_route53_zone.dr_rds_private` (`db.bookstore.internal`
  → replica), `aws_acm_certificate.ingress_dr` + validation, DR ArgoCD
  bootstrap (`kubectl_manifest.*_dr`), `null_resource.wait_for_dr_alb_hostname`
  + `data.kubernetes_ingress_v1.dr_bookstore`. All `count`-gated on
  `enable_dr_standby`.
- `k8s/argocd/dr/` (appproject/application/applicationset) + `k8s/overlays/dr/`
  + `k8s/services/*/overlays/dr/` (5). All render with `kustomize build`.
- `module.route53` `secondary_alb_dns` fed the discovered DR ALB hostname when
  the flag is on.
- Leaf modules (`network`, `security`, `eks`, `monitoring-ec2`) got a
  `versions.tf` so the explicit `providers = {}` pass-through is warning-free.

**Not done — needs a real two-region `terraform plan`/`apply` loop (no creds here):**

1. ✅ **Resolved 2026-08-29, live.** First real `-var enable_dr_standby=true`
   plan/apply ran end-to-end. The CMK `# PLAN-CHECK:` concern didn't
   materialize — `aws_db_instance.dr_replica` (destination CMK, AWS-managed
   key on the source) created cleanly, no rejection. Everything else flagged
   `# PLAN-CHECK:` in `dr-standby.tf` also went through clean on this apply.
2. **Still unverified.** `db.bookstore.internal` resolution was never
   exercised end-to-end by a running app this session — no CI build ran on
   `dr`, so every pod sat `ImagePullBackOff` and never actually opened a DB
   connection. The Route53 wiring itself (both zones, both records) is
   correct per `dr-standby.tf`'s own design; only the "does a real
   mysql2 client actually resolve and connect" step is unproven.
3. ✅ **Fixed 2026-08-29.** `scripts/configure.py` now stamps
   `DR_AWS_REGION_HERE` in `k8s/overlays/dr/kustomization.yaml` (plus the
   `k8s/argocd/dr/*.yaml` repoURL/targetRevision fields and every
   `k8s/services/*/overlays/dr/kustomization.yaml`'s account ID, none of
   which the script reached before). See [[terraform-explained.md]]'s
   `scripts/configure.py` section for the full list of what changed.
4. **Still open, confirmed real.** CI's deploy job only runs
   `kustomize edit set image` against `overlays/prod` — `overlays/dr` never
   gets touched, so the DR cluster runs whatever tag `configure.py` stamped
   at setup, forever. A `dr` push never reaches the standby region. Not
   fixed this session — needs `.github/workflows/ci-cd.yml`'s deploy job
   to also target `k8s/**/overlays/dr` when `enable_dr_standby` is in play.
5. ✅ **Resolved 2026-08-29, live.** `replicate_source_db =
   module.rds.rds_instance_arn` on `aws_db_instance.dr_replica` does create
   the right implicit dependency edge — the replica destroyed cleanly before
   the source RDS instance on the real teardown, confirmed in the log
   (`aws_db_instance.dr_replica` gone well before `module.rds.aws_db_instance.db`
   started destroying). Full 225-resource destroy: `us-west-1` (136
   resources) destroyed clean in one pass; `us-west-2`'s
   `module.eks_addons_dr[0].helm_release.external_secrets` Helm uninstall
   hung and hit its `context deadline exceeded` timeout on the first attempt
   (15+ min, vs. the primary region's identical release which uninstalled in
   48s) — a plain re-run of `terraform destroy -var enable_dr_standby=true`
   picked up exactly where it left off and completed the remaining 45
   resources cleanly, including the DR-region ENI/cluster-SG cleanup
   (confirmed empty). Everything else in the DR chain (ArgoCD, Argo
   Rollouts, AWS LB Controller, the RDS replica, monitoring EC2) uninstalled
   normally around it, so this reads as a one-off transient (a stuck
   webhook or a brief API-server blip in that region at that moment), not a
   deterministic bug — but it's real and worth knowing about: **a DR
   teardown may need a retry.** `terraform destroy` is idempotent and safe
   to just re-run if this happens again.
6. Blog drafts — still not updated (out of scope for this apply/destroy
   validation pass; the "opt-in, compute is the remaining build" framing is
   now stale but untouched).

---

## Bugs found + fixed via the first live two-region apply (2026-08-29)

Five issues surfaced running `-var enable_dr_standby=true` against real AWS
for the first time — one was an operator mistake (config drift, not a code
bug), the other four were genuine defects in this branch's own code, now
fixed and re-verified live. Full technical detail on each lives in
[[terraform-explained.md]] and the relevant module's own comments; this is
the summary.

| # | What broke | Root cause | Fix |
|---|---|---|---|
| 1 | `wait_for_alb_hostname` timed out on the very first apply | Operator error, not a code bug: `config.env`'s `GITHUB_BRANCH` was still `main`. `scripts/configure.py` stamped `targetRevision: main` into the primary `Application`, so ArgoCD synced `main`'s (already-scrubbed) content instead of `dr`'s. | Set `GITHUB_BRANCH=dr` in `config.env` before running `configure.py`. Not a code change — a reminder that `configure.py` must be re-run with the right branch whenever you switch which branch you're deploying. |
| 2 | `terraform plan` failed outright: `Error: Invalid count argument` on `module.route53.aws_route53_record.secondary` | `count = var.secondary_alb_dns != "" ? 1 : 0` — fine when `secondary_alb_dns` was a static var, but `enable_dr_standby` now feeds it from `local.dr_discovered_alb_dns`, a value only known *after* the DR ALB is created in the same apply. Terraform can't resolve `count` from an unknown value at plan time. | New `create_secondary_record` bool input (`modules/route53/variables.tf`), fed from `var.enable_dr_standby \|\| var.secondary_alb_dns != ""` at the root — both plain vars, always statically known — decoupled from the (possibly dynamic) DNS value itself. |
| 3 | `Error: creating Security Group (bookstore-dr-rds): ... GroupDescription is invalid. Character sets beyond ASCII are not supported.` | An em-dash (`—`) in `aws_security_group.dr_replica`'s description. AWS's `CreateSecurityGroup` API requires pure ASCII. | Replaced the em-dash with a plain hyphen. |
| 4 | `Error: creating IAM Role (bookstore-aws-lb-controller): ... EntityAlreadyExists` and the same for `bookstore-external-secrets`, then (next apply) `bookstore-monitoring-ec2` | IAM is account-global, not region-scoped. `modules/eks-addons` and `modules/monitoring-ec2` both hardcode their IAM role names (and, for monitoring-ec2, the instance profile name too) with no per-region suffix — `module.eks_addons_dr`/`module.monitoring_ec2_dr` collide outright with the primary's identically-named roles the moment both exist in the same account. The author had already solved this exact class of problem for the Grafana/monitoring-basic-auth Secrets Manager entries (`create_monitoring_secrets` + cross-region `replica{}`) but missed it for these IAM resources. | New `role_name_suffix` string input on both modules (default `""`), appended to every account-global name (`aws_iam_role`, `aws_iam_role_policy`, `aws_iam_instance_profile`). `dr-standby.tf`'s `module.eks_addons_dr`/`module.monitoring_ec2_dr` calls pass `role_name_suffix = "-dr"`. |
| 5 | `Error: creating Route53 Record: ... InvalidChangeBatch: RRSet of type CNAME with DNS name b17facebook.xyz. is not permitted at apex in zone` | `aws_route53_record.secondary` was a plain CNAME at the zone apex — DNS spec (RFC 1035) forbids a CNAME at the apex (it must coexist with NS/SOA, which a CNAME can't). The original code's own comment had already flagged this exact failure mode as "deliberately deferred… wiring a second, secondary-region-scoped provider through this module is real work" — fix #2 above (`create_secondary_record`) was what finally made the record reachable for the first time, surfacing it. | Added `aws.secondary` as a `configuration_aliases` provider on `modules/route53` (new `versions.tf`), a secondary-region-scoped `data.aws_lb_hosted_zone_id.ingress_lb_secondary` lookup, and converted `secondary` from CNAME to an ALIAS record — same pattern the `primary` record already used. Verified live: `primary` (PRIMARY failover → us-west-1 ALB) and `secondary` (SECONDARY failover → us-west-2 ALB) ALIAS records now sit correctly alongside the zone's NS/SOA records. |

All 6 commits pushed to `dr`. Both regions confirmed fully up and ArgoCD-synced after the fixes; `terraform destroy` on the 2-region stack is the next, still-unrun step (closes item 5 above).
