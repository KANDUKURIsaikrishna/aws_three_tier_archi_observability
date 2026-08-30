# Architecture

Current state of `main`. This describes what the code actually builds, not an aspirational design.

## What this is

A bookstore web app built as a learning/reference implementation of a production-grade AWS three-tier architecture, now fully cut over to a microservices split. The old monolith is gone.

1. The **frontend** (`client/`, React) is what users actually load — deployed as its own `frontend`/`frontend-service` in the `bookstore` namespace, serving the React static build via nginx.
2. Every API call that frontend makes goes to the **microservices platform** — `catalog-service`, `user-service`, `order-service`, `notification-service`, all behind `api-gateway`. The old monolith's `backend/` (Node/Express API, the original `bookstore-backend` Rollout) was deleted outright once it was confirmed to have zero ingress routes and zero references anywhere in the live frontend bundle. `k8s/base/ingress/ingress.yaml` routes `bookstore.<domain>` → `frontend-service`; `k8s/services/api-gateway/base/ingress.yaml` exclusively owns `api.bookstore.<domain>`.

## Top-level system diagram (current, real traffic split)

```
                                   Internet
                                      |
                         Route53 (public zone, failover routing)
                                      |
                    ┌─────────────────────────────────┐
                    │   CloudFront (optional, off by   │
                    │      default) or direct to ALB   │
                    └─────────────────────────────────┘
                                      |
                        ALB (AWS Load Balancer Controller)
                          ┌───────────┴───────────┐
                 host: bookstore.<domain>   host: api.bookstore.<domain>
                          |                           |
                  frontend Service              api-gateway Service
              (React static, nginx)      (Node/Express, JWT verification)
                                                       |
                          ┌────────────┬──────────────┼──────────────┐
                          |            |              |              |
                  catalog-service  user-service  order-service  notification-service
                          |            |              |              |
                          └────────────┴──────┬───────┴──────────────┘
                                               |
                                        RDS MySQL 8.0
                                        (Multi-AZ, us-west-1, per-service schemas)
```

Everything above lives in one EKS cluster (`bookstore-eks`, us-west-1), split across the `bookstore` namespace (frontend only, now that backend is deleted) and 5 microservice namespaces (`catalog`, `user`, `order`, `notification`, `gateway`), deployed via ArgoCD from `k8s/overlays/prod` and `k8s/services/*/overlays/prod` respectively.

## Region layout

**Primary: us-west-1** — all live workloads (EKS, RDS, monitoring EC2, all traffic).
**Secondary: us-west-2** — DR only. ECR image replication + RDS automated-backup replication (needs an explicit KMS key, off by default) + a Route53 failover record. **No EKS cluster in us-west-2.** If us-west-1 goes down, there's no compute to fail over to yet — DR today is backup-level, not active-active. See [`dr.tf`](../terraform/dr.tf).

## Terraform module graph

`network → security → rds → route53 → ecr → eks → monitoring-ec2 → eks-addons` is the module *call* order in `main.tf`, but that's not the real dependency graph — Terraform parallelizes anything not actually connected by a resource/output reference, regardless of where it's written in the file. (No `acm` module appears here because none exists — the wildcard ACM cert is created directly by root-level `ingress-cert.tf`, not a module.) The real shape:

```
network ──┬─→ security ──┬─→ rds ──→ route53 ──→ ingress-cert.tf
          │              └─→ eks ──┬─→ eks-addons ─────┐
          │                        └─→ monitoring-ec2 ←┘ (needs eks + the
ecr  (independent)                                        eks-addons Grafana
iam.tf (independent)                                       secret only, not
                                                            any Helm install)
```

`ecr` and the root `iam.tf` resources have no dependency on `network` at all and run fully in parallel with it. `rds` and `eks` both depend only on `network`+`security`, not on each other, so they provision concurrently — this is why a full stand-up takes roughly `max(RDS time, EKS time)` for that stage, not the sum. `ingress-cert.tf`'s ACM cert only needs `route53`'s hosted zone to exist (for DNS validation records), not the zone's ALB-pointing alias records specifically, so it doesn't get stuck behind the `eks-addons`/ALB-discovery chain those alias records do wait on. `monitoring-ec2` used to have a blanket `depends_on = [module.eks_addons]` forcing it to wait for every Helm chart in `eks-addons` (up to 900s for ArgoCD) even though it only needs the fast Grafana secret — that's been removed.

| Module | Creates | Depends on |
|---|---|---|
| `network` | VPC `170.20.0.0/16`, 2 public + 6 private subnets, IGW, NAT gateway per AZ (one private route table each; `single_nat_gateway = true` collapses to one shared NAT), S3 Gateway VPC Endpoint (free — keeps ECR/S3 traffic off the NAT) | — |
| `security` | Security groups: ALB (80/443 from internet), RDS (3306 from VPC CIDR) | `network` |
| `rds` | MySQL 8.0 `db.t3.micro`, Multi-AZ, gp3 storage, Secrets Manager admin credentials, optional cross-region backup replication | `network`, `security` |
| `route53` | Private zone (RDS internal DNS) + public zone with active-passive failover records | `network`, `rds`, `eks` (needs ALB DNS) |
| `ecr` | ECR repos for `frontend`, plus any `extra_repos` (currently `catalog-service`, `user-service`, `order-service`, `notification-service`, `api-gateway`), 10-image lifecycle policy, optional cross-region replication — `backend` repo deleted along with the old monolith | — |
| `eks` | EKS 1.31 cluster, managed node group (`t3.medium`, min 1 / max 3 / desired 3), OIDC provider (enables IRSA), node launch template running node-exporter + Fluent Bit as systemd services | `network`, `security` |
| `eks-addons` | Helm-installed cluster add-ons: External Secrets Operator, AWS Load Balancer Controller (provisions the ALB), ArgoCD, Argo Rollouts; plus the VPC CNI (NetworkPolicy enforcement), EBS CSI, and metrics-server EKS addons | `eks` |
| `monitoring-ec2` | Standalone EC2 (`t3.small`) running Prometheus + Grafana + Loki + Alertmanager + kube-state-metrics via Docker Compose | `network`, `eks-addons` |

No `acm` module — the wildcard ACM cert is created directly by root-level `ingress-cert.tf`, not a module. Root-level `.tf` files add cross-cutting resources not owned by any module: `iam.tf` (GitHub OIDC role for CI), `ingress-cert.tf` (wildcard ACM cert for the ingress domain), `cloudfront.tf` (optional CDN, ACM cert in us-east-1), `dr.tf` (cross-region backup replication).

No CloudWatch, CloudTrail, or GuardDuty anywhere in this stack — removed 2026-08-23. Prometheus/Grafana/Loki/Alertmanager on `monitoring-ec2` already cover metrics, logs, and alerting; GuardDuty findings had no alerting wired to them and nothing ever read CloudTrail, so both were dead weight. VPC Flow Logs, EKS control-plane log export, and RDS Enhanced Monitoring were dropped with them — all three shipped to CloudWatch Logs and nowhere else.

A destroy-time-only `null_resource.cleanup_eks_networking` (root `main.tf`) sits between `network` and `eks` in the destroy graph — `module.eks` `depends_on` it, so on `terraform destroy` it runs after the cluster is gone but before `network`'s VPC/subnets, cleaning up orphaned VPC CNI ENIs and the EKS-auto-created cluster security group (both created directly via the EC2 API, outside Terraform's own resource graph, and both able to block the VPC destroy with `DependencyViolation` if left behind).

## Why monitoring runs on EC2, not in the cluster

The original design put `kube-prometheus-stack` in EKS. On a single `t3.medium` node it starved every other pod pulling images and never became `Ready` within any reasonable Helm timeout. The fix: move Prometheus, Grafana, Loki, and Alertmanager to a dedicated EC2 instance running Docker Compose. The EKS cluster itself runs **zero monitoring pods** — `node-exporter` and `Fluent Bit` run as systemd services baked into the node launch template instead of DaemonSets, and `kube-state-metrics` runs as a Docker container on the monitoring EC2, reading the cluster over the network via a read-only EKS access entry.

`k8s/base/monitoring/` once held `ServiceMonitor`/`PrometheusRule` CRD manifests; they were removed (2026-08-29). Nothing installs the Prometheus Operator that would consume them, and the EC2 Prometheus scrapes via static configs and `file_sd_configs` (a cron script rewriting target files), not via `ServiceMonitor` discovery. Re-adding them would also break ArgoCD sync outright — an unknown CRD type fails the whole sync batch (TROUBLESHOOTING.md OBS-012).

## The database

Single RDS MySQL 8.0 instance, `db.t3.micro`, Multi-AZ, in two private subnets dedicated to RDS (subnet indices 4-5 of the 6 private subnets — see [Subnet layout](#subnet-layout)). Admin credentials live in Secrets Manager at `/bookstore/db-credentials`, synced into the cluster as a K8s Secret via External Secrets Operator.

The dead `k8s/base/database/` files (`mysql-statefulset.yaml`, `mysql-service.yaml`, `mysql-init-configmap.yaml`) from an earlier in-cluster-MySQL design have been deleted (2026-08-14) — they were never referenced by `k8s/base/kustomization.yaml`. RDS is, and has always been in the live deployment, the real, only database.

## Secrets flow (and the bug that used to break it)

```
AWS Secrets Manager (/bookstore/*)
        |
        | IRSA (IAM Role for Service Account)
        v
External Secrets Operator (external-secrets-sa, namespace external-secrets)
        |
        | ClusterSecretStore "aws-secretsmanager" (cluster-scoped, shared)
        v
ExternalSecret (per namespace, e.g. db-secret, catalog-db-secret)
        |
        v
K8s Secret → mounted into pod env vars
```

This used to be broken: the External Secrets Operator's Helm release created a ServiceAccount with no IRSA role and no annotation, even though the `ClusterSecretStore` already expected one named exactly `external-secrets-sa`. No ExternalSecret anywhere in the cluster — old or new — could actually pull from Secrets Manager. Fixed in `modules/eks-addons/external-secrets.tf` (IRSA role + trust policy, Helm release now names and annotates the ServiceAccount correctly).

The `ClusterSecretStore` + shared IRSA role is **cluster-wide**, scoped in IAM to `/bookstore/*`. Every service's `ExternalSecret` — old `db-secret` and every future microservice's own secret — reuses the same role. This is a deliberate simplification: true per-service secret isolation would need per-namespace `SecretStore` objects instead of one shared `ClusterSecretStore`, which is more machinery than this project's current stage justifies.

## GitOps / deployment flow

```
git push → GitHub Actions CI
  1. secret-scan (Gitleaks)
  2. sast (Semgrep, npm audit, unit tests)          ┐
     validate (ESLint, kubeconform)                  ├─ parallel
  3. build-and-push (Docker build → Trivy scan → ECR push)
  4. deploy (main branch only, manual approval gate):
       kustomize edit set image ... → commit → push
                    |
                    v
            ArgoCD polls repo every 3 min
                    |
                    v
        kustomize build k8s/overlays/prod
                    |
                    v
          reconciles cluster (auto-prune, self-heal)
```

CI never runs `kubectl` directly — it only edits image tags in git, and ArgoCD does the actual apply.

## The microservices platform (live — every frontend API call goes through it)

All 5 services are implemented, registered with ArgoCD, and live. **The frontend's every API call — `/books`, `/auth`, `/cart`, `/orders`, all of it — goes through `api-gateway`.** This isn't partial: `client/`'s build-time `API_URL` (`REACT_APP_API_URL`, set via the `API_URL` GitHub secret) points at `api.bookstore.<domain>`, which `api-gateway`'s `Ingress` exclusively owns — confirmed by inspecting the deployed JS bundle directly. `k8s/base/ingress/ingress.yaml` routes `bookstore.<domain>` (path `/`) to `frontend-service`.

```
frontend (React static assets, served by frontend-service via bookstore-ingress)
    |  every API call, unconditionally
api-gateway (Node/Express + http-proxy-middleware, JWT verification) — sole entry point
    ├── /books         → catalog-service         (GET public; POST needs a JWT; PUT/DELETE need admin role)
    ├── /auth, /users   → user-service            (login/register/profile)
    ├── /orders, /cart   → order-service           (cart, checkout, order history)
    └── (internal)        → notification-service    (called by order-service, not by the frontend directly)
```

The cutover is done — the old monolith's `backend/` (Node/Express, the `bookstore-backend` Rollout, its ECR repo, and every backend-only manifest) was deleted outright once confirmed to have zero ingress routes and zero references anywhere in the live frontend bundle.

Each service: own ECR repo, own K8s namespace, own Deployment/Service/HPA/PDB, own schema inside the *same* shared RDS instance (schema-level isolation, not per-service RDS — that's an explicit non-goal for now), own `/metrics` endpoint labeled `service="<name>"`. Deployed via a single ArgoCD `ApplicationSet` (`k8s/argocd/applicationset-microservices.yaml`) with a list generator, now listing all 5 services.

All 6 app Deployments (frontend + 5 services) carry `topologySpreadConstraints` on `topology.kubernetes.io/zone` then `kubernetes.io/hostname`, `whenUnsatisfiable: ScheduleAnyway` — a soft hint that spreads replicas across AZs and nodes without ever blocking a pod from scheduling (the cluster's pod-slot budget is tight, so a hard constraint would risk `Pending`). Zero extra pods or vCPU; it just biases the scheduler away from co-locating replicas so a lost node or AZ can't take a whole service down. Effective now for the 2-replica workloads (frontend, api-gateway); correct-by-default when HPA scales the others up.

`catalog-service`/`user-service`/`order-service`'s `NetworkPolicy`s were tightened once `api-gateway` existed — ingress is now scoped to the `gateway` namespace instead of allowing all traffic (see commit `153bed2`). `api-gateway` has a real public `Ingress` (`k8s/services/api-gateway/base/ingress.yaml`) for `api.bookstore.<domain>`, and it's now the sole claimant of that host — `k8s/base/ingress/ingress.yaml`'s duplicate rule was removed as part of building the frontend's login/cart/checkout UI (see [Plan 5](#related)), which needed the gateway to be reliably reachable rather than winning by undefined nginx tie-breaking.

Explicitly deferred (see the design spec's Non-goals): service mesh / mTLS, async messaging (SQS), per-service RDS instances, distributed tracing, NetworkPolicy hardening beyond what's described above.

## Subnet layout

```
VPC 170.20.0.0/16 (us-west-1)

public[0]   170.20.1.0/24   us-west-1a   — IGW, ALB
public[1]   170.20.2.0/24   us-west-1c   — IGW, ALB
private[0]  170.20.3.0/24   us-west-1a   — EKS nodes
private[1]  170.20.4.0/24   us-west-1c   — EKS nodes
private[2]  170.20.5.0/24   us-west-1a   — EKS nodes
private[3]  170.20.6.0/24   us-west-1c   — EKS nodes
private[4]  170.20.7.0/24   us-west-1a   — RDS
private[5]  170.20.8.0/24   us-west-1c   — RDS
```

NAT gateway per AZ by default — one in each public subnet, each AZ's private subnets routed through the NAT in their own AZ, so losing an AZ only takes out that AZ's egress. NAT is a managed service and doesn't count against the account's EC2 vCPU quota, so per-AZ is available even while that quota is capped. Set `single_nat_gateway = true` on the `network` module to collapse back to one shared NAT in `public[0]` (cheaper, single-AZ SPOF) — the old demo default.

## Traffic flow (current, real split)

```
Static assets (HTML/JS/CSS):
Internet → Route53 (bookstore.<domain>) → (CloudFront, optional) → ALB
    → frontend Service (static React via nginx)

Every API call the loaded frontend makes:
Internet → Route53 (api.bookstore.<domain>) → (CloudFront, optional) → ALB
    → api-gateway Service (JWT verification on writes)
    → catalog-service / user-service / order-service / notification-service
    → RDS :3306 (per-service schema, shared instance)
```

The old backend's Argo Rollout (canary 10%→25%→50%→100%) is gone — deleted along with the rest of the monolith. There's no canary deploy anywhere in the platform right now; each microservice deploys as a plain rolling-update Deployment via its ArgoCD `ApplicationSet` entry.

## Related docs

- [`DEPLOYMENT.md`](DEPLOYMENT.md) — how to actually stand this up
- [`UML.md`](UML.md) — application-layer UML: component/class/ER/sequence diagrams
- [`../README.md`](../README.md) — tech stack, repo structure, local development, CI/CD overview
