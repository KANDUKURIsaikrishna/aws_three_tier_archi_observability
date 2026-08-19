# Bookstore — AWS Three-Tier Microservices Platform

A production-grade, cloud-native bookstore application on AWS, built as a reference implementation of a three-tier architecture cut over to microservices. Infrastructure is fully codified in Terraform, services are containerised with Docker, orchestrated on Kubernetes (EKS) via ArgoCD GitOps, and protected by a DevSecOps CI/CD pipeline with full observability.

## Documentation

| Doc | Covers |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System-level view: current state, module graph, region layout, the microservices platform |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | How to stand this up from zero, step by step |

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Tech Stack](#tech-stack)
3. [Repository Structure](#repository-structure)
4. [Prerequisites](#prerequisites)
5. [Local Development](#local-development)
6. [Building and Pushing Docker Images](#building-and-pushing-docker-images)
7. [Infrastructure, Deploy, and CI/CD](#infrastructure-deploy-and-cicd)
8. [Secret Management](#secret-management)
9. [Security Controls](#security-controls)
10. [GitHub Secrets Reference](#github-secrets-reference)

---

## Architecture Overview

```
                                   Internet
                                      |
                         Route 53 (public zone, failover routing)
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

Everything runs in one EKS cluster (`bookstore-eks`, `us-west-1`), split across the `bookstore` namespace (frontend only) and five microservice namespaces (`catalog`, `user`, `order`, `notification`, `gateway`), deployed via ArgoCD from `k8s/overlays/prod` and `k8s/services/*/overlays/prod`. Monitoring (Prometheus, Grafana, Loki, Alertmanager) runs off-cluster on a dedicated EC2 instance — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why.

**Traffic flow:**
1. User → Route 53 → ALB → frontend Service (React SPA via nginx)
2. Frontend calls `api.bookstore.<domain>` → same ALB → `api-gateway` (JWT verification, request routing)
3. `api-gateway` fans out to `catalog-service`, `user-service`, `order-service`, `notification-service`
4. Each service reads/writes its own schema in the shared RDS MySQL instance
5. Each service exposes `/metrics` (prom-client); the monitoring EC2 scrapes them via static/file_sd configs into Prometheus → Grafana

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 18, Nginx (Alpine) |
| Microservices | Node.js, Express, mysql2, prom-client, helmet, morgan |
| Database | MySQL 8.0 (RDS, Multi-AZ) |
| Container Registry | Amazon ECR |
| Orchestration | Kubernetes 1.31 on Amazon EKS |
| Ingress | AWS Load Balancer Controller (ALB) |
| Progressive Delivery | Argo Rollouts |
| Infrastructure as Code | Terraform ≥ 1.7, AWS provider ~5.0, Helm provider |
| CI/CD | GitHub Actions |
| GitOps | ArgoCD |
| Secret Management | AWS Secrets Manager + External Secrets Operator |
| Observability | Prometheus + Grafana + Loki + Alertmanager on a dedicated EC2 (Docker Compose) |
| Security Scanning | Trivy (containers), Gitleaks (secrets), tfsec (IaC), SonarCloud (code quality + coverage gate) |
| TLS | cert-manager + Let's Encrypt / ACM |
| Testing | Vitest per service, `vi.fn()` mock db |
| DR | Cross-region (us-west-2) ECR replication + RDS backup replication + Route53 failover |

---

## Repository Structure

```
.
├── terraform/                  # All infrastructure as code
│   ├── main.tf                  # Root config — module call order + Helm provider
│   ├── argocd.tf / cloudfront.tf / cloudtrail.tf / dr.tf / guardduty.tf
│   ├── iam.tf / observability-rbac.tf / outputs.tf / providers.tf / variables.tf / versions.tf
│   ├── modules/                  # Reusable modules
│   │   ├── ecr/                    # ECR repositories per service
│   │   ├── eks/                     # EKS cluster + OIDC + node group
│   │   ├── eks-addons/               # Helm: ALB controller, ESO, ArgoCD, Argo Rollouts, VPC CNI, EBS CSI
│   │   ├── monitoring-ec2/            # Standalone Prometheus/Grafana/Loki/Alertmanager EC2
│   │   ├── network/                 # VPC, subnets, NAT gateway
│   │   ├── rds/                     # RDS MySQL (Multi-AZ)
│   │   ├── route53/                 # Public + private hosted zones
│   │   └── security/                # Security groups
│   └── environments/             # Per-environment tfvars templates (dev, staging)
│
├── eks_bootstrap.py           # Post-apply cluster bootstrap script
│
├── client/                    # React frontend
│   ├── Dockerfile              # Multi-stage: build → Nginx
│   └── src/
│
├── services/                  # Microservices (all Node/Express, Vitest)
│   ├── api-gateway/            # JWT verification, request routing
│   ├── catalog-service/
│   ├── user-service/
│   ├── order-service/
│   └── notification-service/
│   # each: Dockerfile, app.js, index.js, __tests__/
│
├── k8s/                        # Kubernetes manifests (Kustomize base + overlays)
│   ├── base/                    # Shared frontend resources
│   ├── services/                # Per-microservice base + overlays
│   │   ├── api-gateway/
│   │   ├── catalog-service/
│   │   ├── user-service/
│   │   ├── order-service/
│   │   └── notification-service/
│   ├── overlays/dev/ , overlays/prod/
│   └── argocd/                  # ArgoCD Application manifests
│
├── scripts/                     # build-and-push, init-backend, init-domain, configure.py, simulate-load
├── docs/                        # ARCHITECTURE.md, DEPLOYMENT.md
└── .github/workflows/           # ci-cd.yml, terraform.yml, terraform-drift.yml
```

---

## Prerequisites

| Tool | Minimum Version | Purpose |
|---|---|---|
| Node.js | 18 | Local service/frontend development |
| Docker | 24 | Building images |
| Terraform | 1.7 | Provisioning AWS infrastructure |
| AWS CLI | 2.x | ECR login, EKS kubeconfig |
| kubectl | 1.31 | Deploying k8s manifests |
| helm | 3.x | Querying cluster add-ons (installed by Terraform) |
| kustomize | 5.x | Building manifests locally |

---

## Local Development

### A microservice (e.g. `order-service`)

```bash
cd services/order-service
npm install

# Create .env with your local MySQL details
cat > .env <<EOF
DB_HOST=localhost
DB_USERNAME=root
DB_PASSWORD=yourpassword
DB_PORT=3306
DB_NAME=order_db
APP_PORT=3000
EOF

npm run dev     # nodemon, auto-reload
# or
npm start
```

Each service exposes `/metrics` (prom-client) alongside its API routes.

### Run tests (no database required)

```bash
cd services/<service-name>
npm test
# Vitest, vi.fn() mock db — no MySQL needed.
```

### Frontend

```bash
cd client
npm install
npm start          # development server
# or
npm run build      # production build → build/
```

---

## Building and Pushing Docker Images

```bash
# Usage
./scripts/build-and-push.sh <AWS_ACCOUNT_ID> <AWS_REGION> <IMAGE_TAG> [REACT_APP_API_URL]

# Example
./scripts/build-and-push.sh 123456789012 us-west-1 v1.2.0 https://api.bookstore.b17facebook.xyz
```

> The CI/CD pipeline performs these steps automatically on every merge to `main`. Manual use of this script is for hotfixes or pre-release testing only.

---

## Infrastructure, Deploy, and CI/CD

The platform is provisioned by 8 Terraform modules plus root-level cross-cutting resources (IAM/OIDC, CloudTrail, GuardDuty, CloudFront, DR), and deployed via ArgoCD GitOps across the frontend and five microservice namespaces. Full step-by-step instructions — Terraform state bootstrap, `config.env`/`scripts/configure.py`, the apply itself, and post-apply verification — live in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Module-by-module and traffic-flow detail is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

The GitHub Actions pipeline (`.github/workflows/ci-cd.yml`) runs, per push: secret scan (Gitleaks) → test/audit/validate (Vitest + coverage, npm audit, SonarCloud, kubeconform) → build-and-push (Docker build → Trivy scan → ECR push) → deploy on `main` (manual approval gate, `kustomize edit set image` → commit → ArgoCD sync).

---

## Secret Management

| Context | Mechanism | How it works |
|---|---|---|
| Production (EKS) | External Secrets Operator | ESO reads AWS Secrets Manager and creates native k8s Secrets in-cluster, per namespace |
| CI/CD pipeline | GitHub Secrets only | `AWS_ROLE_ARN`, `AWS_ACCOUNT_ID`, `API_URL` — no DB credentials in the pipeline at all |
| Local development | `.env` file | Never committed; see `.gitignore` |
| Terraform state | AWS Secrets Manager | RDS admin credentials at `/bookstore/db-credentials`, Grafana admin at `/bookstore/grafana-admin` |

**Rule:** No credential, password, or account ID should ever appear in plain text in any committed file.

---

## Security Controls

| Control | Implementation |
|---|---|
| Secret detection | Gitleaks scans every commit and full git history |
| Code quality / SAST | SonarCloud Quality Gate (bugs, code smells, security hotspots, coverage) |
| Unit tests | Vitest per service — runs before audit in CI |
| Dependency CVEs | `npm audit --omit=dev --audit-level=high` per service and frontend |
| Container CVEs | Trivy blocks pushes on CRITICAL/HIGH unfixed vulns |
| IaC security | tfsec runs on every Terraform change |
| No static AWS keys | GitHub OIDC → IAM role assumption |
| Secrets in-cluster | External Secrets Operator + AWS Secrets Manager |
| Non-root containers | All pods run as non-root |
| Read-only filesystems | `readOnlyRootFilesystem: true` on all app containers |
| Network segmentation | Kubernetes NetworkPolicy restricts pod-to-pod traffic |
| TLS everywhere | cert-manager + Let's Encrypt / ACM; force-redirect HTTP → HTTPS |
| Progressive delivery | Argo Rollouts — easy rollback if errors spike |
| Manual deploy gate | GitHub Environments `production` requires reviewer approval |
| Threat detection | GuardDuty (S3, K8s audit, EBS malware) + multi-region CloudTrail |

---

## GitHub Secrets Reference

Configure these in **Settings → Secrets and variables → Actions** before running the pipeline:

| Secret | Description | Example |
|---|---|---|
| `AWS_ACCOUNT_ID` | Your 12-digit AWS account ID | `123456789012` |
| `AWS_ROLE_ARN` | ARN of the OIDC IAM role the pipeline assumes | `arn:aws:iam::123456789012:role/bookstore-github-oidc-role` |
| `API_URL` | Public URL of the api-gateway (injected into the React build) | `https://api.bookstore.b17facebook.xyz` |
| `SONAR_TOKEN` | SonarCloud auth token — sonarcloud.io → My Account → Security | `token...` |
| `SONAR_ORGANIZATION` | SonarCloud organization key | `kandukurisaikrishna` |
| `SONAR_PROJECT_KEY` | SonarCloud project key | `KANDUKURIsaikrishna_aws_three_tier_archi_observability` |
