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

- **Apply:** primary stack unchanged. Secondary `module.network_dr` →
  `eks_dr` → `eks_addons_dr` (Helm) → ArgoCD bootstrap → ALB discovery →
  `route53` secondary record. `aws_db_instance.dr_replica` depends on
  `module.rds` (source) **and** `module.network_dr` (subnet group). Expect
  the secondary apply to add ~20–25 min on top of the primary.
- **Destroy:** the read replica must be deleted **before** the source RDS
  instance (AWS refuses to drop a replicated source). Add
  `aws_db_instance.dr_replica` to the primary RDS module's implicit
  teardown-before set via `depends_on`, or document "disable
  `enable_dr_standby` and apply, then full destroy". The secondary cluster
  needs the same `null_resource.cleanup_eks_networking` +
  `delete_ingress_objects` treatment the primary has — instantiate those for
  the DR VPC too.

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

## Staging of the implementation

1. **Provider refactor** — `eks-addons` `configuration_aliases`, primary call
   site `providers = {}`, add `providers-dr.tf`. Apply → no-op diff on
   primary. *Gate: primary plan is clean.*
2. **Secret replication** — replica blocks, `enable_dr_standby` var. Apply
   with flag off → no-op.
3. **DB DNS indirection** — `db.bookstore.internal`, per-service `DB_HOST`
   swap. Apply → services reconnect via DNS (verify checkout still works).
4. **Secondary network + EKS + addons** — `dr-standby.tf` part 1. Apply with
   flag on.
5. **Secondary RDS replica + private zone.**
6. **Secondary ACM + ALB discovery + ArgoCD bootstrap + `k8s/overlays/dr`.**
7. **Route53 secondary failover wire-up + monitoring-ec2 DR.**
8. **Docs + blog + CI matrix + runbook.**
