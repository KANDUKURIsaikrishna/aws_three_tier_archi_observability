# Kubernetes dependency graph — what deploys, and in what order

The Terraform graph is one thing Terraform resolves for you. On the k8s side
there are **two** orderings that matter, and neither is the file layout:

1. **Build-time composition** — how `kustomize build` assembles the manifests
   ArgoCD actually applies (overlay → base → resource files → image-tag patch).
2. **Runtime sync order** — ArgoCD's PreSync/Sync/PostSync phases and
   `sync-wave` numbers, which decide what lands before what on a real cluster.

Personal reference notes — snapshot, not a maintained doc. Tracked on the `dr`
branch only (gitignored on `main`). Verified against `k8s/` and
`terraform/argocd.tf` on 2026-08-29; extended the same day with Graph C below
(the `dr` branch's own `k8s/argocd/dr/` tree) after its first real live sync.
See [[k8s-explained.md]] Part 5 for the prose version.

---

## Graph A — who deploys what

```mermaid
flowchart TD
    TF["Terraform · argocd.tf<br/>kubectl_manifest.* (after module.eks_addons)"]
    TF --> APRJ["AppProject/bookstore<br/>scopes: 1 repo · 1 cluster · 6 namespaces<br/>cluster kinds: Namespace, StorageClass, ClusterSecretStore"]
    TF --> APP["Application/bookstore"]
    TF --> APPSET["ApplicationSet/bookstore-microservices<br/>list generator · 5 elements"]
    APRJ -. governs .-> APP
    APRJ -. governs .-> APPSET

    APP --> OVF["kustomize build k8s/overlays/prod<br/>= ../../base + hpa-frontend.yaml + images: patch (CI-stamped)"]
    OVF --> NSB["namespace: bookstore<br/>ClusterSecretStore/aws-secretsmanager · frontend Deployment/Service/Ingress<br/>NetworkPolicy · PDB · ResourceQuota · LimitRange · StorageClass gp3"]

    APPSET --> ASVC["Application/{catalog,user,order,notification,api-gateway}-service<br/>(one per generator element)"]
    ASVC --> OVS["kustomize build k8s/services/&lt;svc&gt;/overlays/prod<br/>= ../../base + images: patch (CI-stamped)"]
    OVS --> NSS["namespace: &lt;svc&gt;  (catalog | user | order | notification | gateway)<br/>ConfigMap · ExternalSecret ×2 · schema-init Job* · Deployment · Service<br/>HPA · PDB · NetworkPolicy   (*api-gateway has Ingress instead of a Job)"]

    EXCL["k8s/base/monitoring/ — REMOVED 2026-08-29<br/>(ServiceMonitor/PrometheusRule: no Prometheus Operator;<br/>an unknown CRD fails the whole sync batch — don't re-add)"]

    classDef off fill:#f0f0f0,stroke:#999,stroke-dasharray:4 3,color:#666;
    class EXCL off;
```

- **`Application/bookstore`** renders `k8s/overlays/prod` → the `bookstore`
  namespace: the React frontend plus the three cluster-scoped resources the
  whole platform shares (`ClusterSecretStore`, `StorageClass`, and the
  `Namespace` itself).
- **`ApplicationSet/bookstore-microservices`** stamps out one `Application`
  per list entry, each rendering `k8s/services/<svc>/overlays/prod` into its
  own namespace. Adding a 6th service = one more list element, no new file.
- Each overlay is thin: `resources: [../../base]` plus an `images:` block that
  **CI rewrites** every deploy (`kustomize edit set image …` on the
  *overlay*, never the base).
- `k8s/base/monitoring/` (`ServiceMonitor`/`PrometheusRule`) was **removed
  2026-08-29** (`cdbd5cc`) — `monitoring.coreos.com` CRDs with no Prometheus
  Operator to register them, and an unknown resource type fails the **entire**
  Application sync, not just those two objects (OBS-012). The
  `base/kustomization.yaml` comment says don't reintroduce them.

---

## Graph B — sync order within one microservice

ArgoCD runs **all PreSync hooks** (in ascending `sync-wave`) → then the whole
**Sync phase** → then PostSync. The k8s `Secret` objects are not ArgoCD
resources at all: the ESO controller materializes them from the
`ExternalSecret`s on its own reconcile loop, and anything waiting on them just
retries until they appear.

```mermaid
flowchart TD
    ESO["ESO controller (Helm, from Terraform)<br/>external-secrets-sa + IRSA role-arn annotation"]
    ALBC["AWS Load Balancer Controller (Helm, from Terraform)"]
    RDS["RDS MySQL reachable (Terraform)"]

    W2["PreSync wave -2<br/>ClusterSecretStore/aws-secretsmanager<br/>(owned by Application/bookstore, not the svc App)"]
    W1["PreSync wave -1<br/>ExternalSecret/admin-db-secret<br/>ExternalSecret/&lt;svc&gt;-db-secret"]
    SECRET["k8s Secret/admin-db-secret + &lt;svc&gt;-db-secret<br/>(materialized by ESO — async, not wave-ordered)"]
    W0["PreSync wave 0<br/>Job/&lt;svc&gt;-schema-init<br/>CREATE DATABASE / USER / GRANT + seed (idempotent)"]

    NSSTUFF["Sync: Namespace · LimitRange · ConfigMap"]
    DEP["Sync: Deployment/&lt;svc&gt;<br/>env from &lt;svc&gt;-db-secret + &lt;svc&gt;-config"]
    REST["Sync: Service · HPA · PDB · NetworkPolicy"]
    ING["Sync: Ingress  (api-gateway + frontend only)"]

    NP["NetworkPolicy default-deny-all<br/>pod needs label app=&lt;svc&gt; to reach RDS :3306 and DNS :53"]

    W2 --> W1
    ESO --> W1
    W1 --> SECRET
    W1 --> W0
    SECRET --> W0
    RDS --> W0
    NP -. label gate .-> W0
    W0 --> NSSTUFF
    W0 --> DEP
    SECRET --> DEP
    NSSTUFF --> DEP
    NP -. label gate .-> DEP
    DEP --> REST
    DEP --> ING
    ALBC --> ING

    classDef critpath fill:#e6f0ff,stroke:#2b6cb0,stroke-width:2px;
    class W0,DEP,ING critpath;
```

Blue = the slow lane on a fresh cluster: the schema-init Job (waits on ESO's
first reconcile, then a live RDS round-trip), the Deployment (ECR image pull +
`/health` readiness gate), and the Ingress (ALB controller has to provision a
real load balancer — minutes).

---

## Graph C — the DR cluster's own tree (`dr` branch, `var.enable_dr_standby`)

Not a variant of Graph A — a **fully separate, independent ArgoCD instance**
running inside the DR cluster itself, bootstrapped by `terraform/dr-standby.tf`
(not the primary's `argocd.tf`, and not the primary's ArgoCD reaching across
regions). Same repo, same branch, different `source.path` and a different
destination cluster (though `destination.server` is still
`https://kubernetes.default.svc` — correct, since that's *this* cluster's own
API server from the DR ArgoCD's point of view).

```mermaid
flowchart TD
    TFDR["Terraform · dr-standby.tf<br/>kubectl_manifest.*_dr (provider = kubectl.secondary)"]
    TFDR --> APRJDR["AppProject/bookstore (DR cluster's own copy)"]
    TFDR --> APPDR["Application/bookstore"]
    TFDR --> APPSETDR["ApplicationSet/bookstore-microservices"]
    APRJDR -. governs .-> APPDR
    APRJDR -. governs .-> APPSETDR

    APPDR --> OVFDR["kustomize build k8s/overlays/dr<br/>= ../../base + hpa-frontend.yaml<br/>+ ClusterSecretStore region patch (us-west-2)<br/>+ images: patch (us-west-2 ECR replica)"]
    OVFDR --> NSBDR["namespace: bookstore (DR cluster)<br/>ClusterSecretStore reads Secrets Manager in us-west-2<br/>(the primary's cross-region replica{} secrets)"]

    APPSETDR --> ASVCDR["Application/{catalog,user,order,notification,api-gateway}-service (DR)"]
    ASVCDR --> OVSDR["kustomize build k8s/services/&lt;svc&gt;/overlays/dr<br/>= ../../base + images: patch (us-west-2 ECR replica)"]
    OVSDR --> NSSDR["namespace: &lt;svc&gt; (DR cluster)<br/>same ConfigMap/ExternalSecret/Job/Deployment/Service/HPA/PDB/NetworkPolicy<br/>shape as Graph A — no DR-specific overlay for these"]

    STALE["CI's deploy job only runs<br/>`kustomize edit set image` against overlays/prod —<br/>never overlays/dr. Tags here stay frozen at whatever<br/>scripts/configure.py stamped at initial setup."]
    OVFDR -.->|"confirmed real gap,<br/>not fixed yet"| STALE
    OVSDR -.->|"confirmed real gap,<br/>not fixed yet"| STALE

    classDef gap fill:#fff3cd,stroke:#c9a227,stroke-width:1.5px,color:#7a5c00;
    class STALE gap;
```

Only one manifest actually differs from Graph A's tree: `k8s/overlays/dr/kustomization.yaml`'s
`ClusterSecretStore` patch (`spec.provider.aws.region: us-west-2`) — every
other resource, in both the frontend and all 5 service overlays, is the exact
same base content Graph A already covers, just deployed a second time into a
different cluster with different image-registry/region wiring. The
`STALE` node above is the one confirmed, still-open gap: a `dr` branch push
rebuilds and pushes fresh images to the *primary* ECR/tag, but nothing ever
tells the DR overlays' `kustomization.yaml` files about the new tag — see
`docs/DR-STANDBY-PLAN.md`'s "Not done" list, item 4.

---

## Sync waves

| Phase / wave | Resources | Waits on | Notes |
|---|---|---|---|
| **PreSync −2** | `ClusterSecretStore/aws-secretsmanager` | ESO controller running | Only in `Application/bookstore`. Must be a hook — as a plain Sync resource it applies *after* every PreSync hook, so every `ExternalSecret`'s first reconcile fails "ClusterSecretStore not found" (OBS-030). |
| **PreSync −1** | `ExternalSecret/admin-db-secret`, `ExternalSecret/<svc>-db-secret` | wave −2 + ESO + IRSA on `external-secrets-sa` | The IRSA annotation bug lived here — SA created with no `role-arn`, every pull failed silently. |
| *(async)* | k8s `Secret` objects | ESO reconcile of the above | Not ArgoCD-ordered. Consumers retry until present. |
| **PreSync 0** | `Job/<svc>-schema-init` | wave −1 secrets materialized + RDS reachable | `mysql:8.0` pod, idempotent SQL. Pod label `app:<svc>` required or default-deny-all NetworkPolicy blocks egress to :3306/:53. `hook-delete-policy: BeforeHookCreation,HookSucceeded` so a *failed* job is also cleaned before the next sync (OBS-015). **api-gateway has no such Job.** |
| **Sync** | Namespace, LimitRange, ConfigMap, Deployment, Service, HPA, PDB, NetworkPolicy, Ingress | all PreSync hooks succeeded | Deployment env: `DB_*` from `<svc>-db-secret`, `DB_PORT`/`DB_NAME`/`APP_PORT` from `<svc>-config`. |
| *(async)* | ALB | frontend + `api-gateway` `Ingress` objects | AWS Load Balancer Controller reconciles the Ingress, auto-discovers the ACM cert (no `certificate-arn` annotation), provisions one shared ALB via `group.name`. |

---

## Critical path (fresh cluster, from ArgoCD installed)

```
module.eks_addons installs ArgoCD (Terraform)
  → kubectl_manifest.argocd_appproject → argocd_application / applicationset
  → [Application/bookstore]  ClusterSecretStore                 (PreSync −2)
  → [Application/<svc>]      ExternalSecret admin + db          (PreSync −1)
  → ESO first reconcile → k8s Secret materialized              (async, ~seconds)
  → Job/<svc>-schema-init → RDS round-trip: CREATE DATABASE/USER (PreSync 0)
  → Deployment/<svc> → ECR image pull → /health readiness       (Sync)
  → Service / HPA / PDB / NetworkPolicy                         (Sync)
  → api-gateway Ingress → ALB controller provisions ALB         (async, minutes)
```

The 5 microservice Applications sync **in parallel** with each other — no
ordering between them. Within each, the PreSync chain is strictly serial.
`api-gateway` is effectively the long pole because its `Ingress` gates
external traffic and the ALB takes minutes to come up.

---

## Places the file layout lies about order

1. **An unknown CRD fails the whole batch, not just itself.** `ServiceMonitor`
   / `PrometheusRule` under `k8s/base/monitoring/` were never in
   `base/kustomization.yaml` — including even one would break *every* resource
   in the `bookstore` Application's sync (OBS-012). The files themselves were
   deleted 2026-08-29 (`cdbd5cc`); the `base/kustomization.yaml` comment
   records why not to bring them back.

2. **`ClusterSecretStore` sits in `k8s/base/secrets/external-secret.yaml` next
   to nothing that depends on it visually — but it's wave −2 and a PreSync
   hook.** Drop the hook annotations and it becomes a plain Sync resource that
   ArgoCD applies *after* all PreSync hooks, guaranteeing every
   `ExternalSecret` fails first reconcile on a fresh cluster (OBS-030).

3. **The schema-init Job and its `ExternalSecret`s are in the same `base/`
   folder, listed adjacently — but they're in different ArgoCD phases.** Both
   are PreSync hooks; the Job is wave 0, the secrets wave −1. Without the
   explicit waves the Job's first pod starts before the Secret exists
   ("secret not found", CreateContainerConfigError, 15-min retry loop —
   OBS-013).

4. **Cross-Application order is not guaranteed.** The 5 service Applications
   reference `ClusterSecretStore/aws-secretsmanager` by name, but the
   `bookstore` Application owns it. ArgoCD doesn't sequence across
   Applications — a fresh cluster relies on retry/backoff until `bookstore`
   has synced the store.

5. **`api-gateway/base/kustomization.yaml` has `ingress.yaml` where every
   other service has `schema-init-job.yaml`.** Same list position, completely
   different resource — api-gateway is stateless (no schema, no DB user) and
   is the only backend with an Ingress.

6. **CI edits the overlay, not the base.** `kustomize edit set image` in the
   deploy job rewrites `overlays/prod/kustomization.yaml`'s `images:` block;
   `base/` never carries a real tag.

---

## Regenerate the real thing

```bash
# Exactly what ArgoCD will apply:
kustomize build k8s/overlays/prod
kustomize build k8s/services/catalog-service/overlays/prod

# From a running ArgoCD:
argocd app manifests bookstore
argocd app resources  catalog-service     # live tree + sync-wave per resource
argocd app get        catalog-service --show-operation

# Hook/wave annotations across the repo:
grep -rn 'argocd.argoproj.io/\(hook\|sync-wave\)' k8s/
```
