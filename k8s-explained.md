# k8s folder, explained — file by file, service by service

Personal reference doc, not part of the repo's real documentation (that's `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `docs/UML.md`, `docs/DR-STANDBY-PLAN.md`). Tracked on the `dr` branch only (gitignored on `main`) — companion to [[terraform-explained.md]], written 2026-08-27 against `main`'s state, extended 2026-08-29 with Part 5 covering this branch's own `k8s/argocd/dr/` + `overlays/dr/` trees after their first real live sync. If the manifests change further, this can go stale — treat it as a snapshot, not a live doc.

Everything lives under `k8s/`. Two independent kustomize trees on `main`: `k8s/base/` + `k8s/overlays/` (the **frontend**, deployed by a single hand-written ArgoCD `Application`) and `k8s/services/<name>/` × 5 (the **backend microservices**, deployed by one ArgoCD `ApplicationSet`). `k8s/argocd/` holds the GitOps wiring that ties both trees to the cluster. This branch (`dr`) adds a *third* set of trees — `k8s/argocd/dr/`, `k8s/overlays/dr/`, `k8s/services/*/overlays/dr/` — deploying the exact same base manifests into the DR standby region's own cluster (see Part 5). No Helm anywhere in this folder — every workload is plain kustomize.

---

## Part 1 — Frontend tree (`k8s/base/`, `k8s/overlays/`)
 
### `base/kustomization.yaml`
The frontend's kustomize base. Sets `namespace: bookstore` for every resource listed (so none of the individual files below need their own `namespace:` field except `namespace.yaml` itself, which defines that namespace). Deliberately **excludes** `monitoring/servicemonitor.yaml` and `monitoring/prometheus-rules.yaml` — both are `monitoring.coreos.com/v1` CRDs that require the Prometheus Operator, which this cluster doesn't run (Prometheus lives on the standalone `monitoring-ec2` instance instead, see `docs/ARCHITECTURE.md`). Confirmed live: an unknown CRD type doesn't just get skipped, it fails the *entire* ArgoCD sync batch (`ComparisonError`/`SyncFailed`) — see `TROUBLESHOOTING.md` OBS-012. Both files are kept in the tree as reference/history, just never wired into the resources list.

### `base/namespace.yaml`
Creates the `bookstore` namespace with Pod Security Standards labels (`enforce`/`audit`/`warn: restricted`) — the strictest PSS tier, blocking any pod that runs as root, escalates privileges, or skips a seccomp profile at admission time, before it ever reaches a kubelet.

### `base/frontend/deployment.yaml`
The only workload in this tree. 2 replicas, `nginx`-based static SPA. `automountServiceAccountToken: false` (no K8s API calls from this pod, ever). `runAsUser: 101` — nginx's own unprivileged UID inside the `nginxinc` image, not an arbitrary choice. `readOnlyRootFilesystem: true` forces three `emptyDir` mounts (`/tmp`, `/var/cache/nginx`, `/var/run`) since nginx needs to write there at runtime even with no other persistent state.

### `base/frontend/service.yaml`
`ClusterIP` on port 80 → container port 8080. Only consumer is the shared ALB via `base/ingress/ingress.yaml`.

### `base/ingress/ingress.yaml`
One `Ingress` object, `ingressClassName: alb` (AWS Load Balancer Controller, not the retired `ingress-nginx`). `alb.ingress.kubernetes.io/group.name: bookstore` is the load-bearing line — the api-gateway's own Ingress (`k8s/services/api-gateway/base/ingress.yaml`) carries the *same* group name, so the controller provisions **one** ALB shared by both host rules instead of two separate ALBs. No `certificate-arn` annotation — the controller auto-discovers a matching ACM cert by host (see `terraform/ingress-cert.tf`). Host is a placeholder (`bookstore.YOUR_DOMAIN_HERE.com`) stamped by `scripts/configure.py`, never hand-edited.

### `base/network-policy/network-policy.yaml`
Two policies. `default-deny-all`: empty `podSelector` + both `Ingress`/`Egress` policy types = default-deny for the whole `bookstore` namespace, the baseline every other rule here punches a hole through. `frontend-policy`: ingress allowed only from `170.20.0.0/16` (the VPC CIDR) on port 8080 — an `ipBlock`, not a `namespaceSelector`, because the ALB (`target-type: ip`) connects straight to the pod's real ENI IP from its own ENIs in the VPC, never routing through some `ingress-nginx` namespace's pods the way the old controller did. Egress allowed only to DNS (53/UDP+TCP) — the SPA's actual API calls happen client-side, from the user's browser straight to `api-gateway` over the internet, not from this pod server-side, so no `api-gateway` egress rule exists here at all.

### `base/pdb/pdb.yaml`
`frontend-pdb`: `minAvailable: 1` against 2 replicas — safe here specifically because replicas > 1 means Kubernetes can still evict one pod for a node drain/upgrade without violating the budget. (Contrast with the backend services below, which run `replicas: 1` and use `maxUnavailable` instead — see Part 2.)

### `base/limitrange.yaml` / `base/quota.yaml`
`LimitRange`: default request/limit (50m/64Mi request, 250m/128Mi limit) applied to any *future* container added without its own explicit values — every container already in this tree sets its own, so this is a backstop, not an active constraint. `ResourceQuota`: namespace-wide ceiling (2 CPU / 2Gi requests, 4 CPU / 4Gi limits, 20 pods) — sized for the frontend's own footprint, unrelated to the per-service quotas backend namespaces don't have (see Part 2).

### `base/storageclass/gp3.yaml`
Cluster-scoped `StorageClass`, `is-default-class: true`, `reclaimPolicy: Retain` (a deleted PVC doesn't delete the underlying EBS volume — deliberate, though nothing in this repo currently provisions a PVC against it). Listed in `k8s/argocd/appproject.yaml`'s `clusterResourceWhitelist` since it's cluster-scoped, same reasoning as `Namespace` and `ClusterSecretStore`.

### `base/secrets/external-secret.yaml`
Defines the `ClusterSecretStore` (`aws-secretsmanager`) — the one piece every namespace's `ExternalSecret` points at via `secretStoreRef`. `argocd.argoproj.io/hook: PreSync`, `sync-wave: "-2"` — must exist before *any* `ExternalSecret`'s own PreSync hook runs (this file's wave is one earlier than every service's `sync-wave: "-1"`, see Part 2). Without the hook annotation this would be a plain Sync-phase resource, and ArgoCD always applies Sync-phase resources *after* every PreSync hook — on a from-scratch cluster that guarantees every `ExternalSecret`'s first reconcile fails with "could not get ClusterSecretStore … not found", not as a race, every single time. See `TROUBLESHOOTING.md` OBS-030. The old backend monolith's own `db-secret` `ExternalSecret` used to live in this file too — deleted with the monolith; the `ClusterSecretStore` itself stayed, since every microservice's own `ExternalSecret` still references it by name.

### `base/monitoring/servicemonitor.yaml`, `base/monitoring/prometheus-rules.yaml`
Not applied (see `base/kustomization.yaml` above) — kept as reference for what a Prometheus-Operator-based setup *would* look like. `prometheus-rules.yaml` still references the deleted backend monolith (`job="backend"`, `ingress="bookstore-ingress"` on an nginx-ingress metric that no longer exists post AWS Load Balancer Controller migration) — stale content, harmless only because it's never synced.

### `overlays/dev/kustomization.yaml`
References `../../base`, patches `Deployment/frontend` replicas from 2 → 1 via a JSON patch. Not wired into any ArgoCD `Application` currently — `k8s/argocd/application.yaml` points at `overlays/prod`. Exists for a local `kustomize build k8s/overlays/dev` / manual `kubectl apply -k` workflow only.

### `overlays/prod/kustomization.yaml` + `hpa-frontend.yaml`
The tree ArgoCD actually deploys (`k8s/argocd/application.yaml`'s `path`). Adds `hpa-frontend.yaml` (`minReplicas: 1`, `maxReplicas: 3`, 70% CPU target) on top of the base. `images:` block rewrites `bookstore-frontend:latest` → the real ECR URI + SHA tag — CI's deploy stage runs `kustomize edit set image` here on every push (`${ECR_REGISTRY}/bookstore-frontend:${SHA}`); the `000000000000` account-ID placeholder in the committed file is intentional, never hand-edited.

---

## Part 2 — Backend microservices (`k8s/services/<name>/`)

Five services, near-identical structure: `catalog-service`, `user-service`, `order-service`, `notification-service`, `api-gateway`. Each owns its **own namespace** (`catalog`, `user`, `order`, `notification`, `gateway`) — full blast-radius isolation, not just a logical grouping within one namespace like the frontend tree. `catalog-service` is used as the reference below since it's the most complete (has the schema-init Job the others share the same pattern for); differences per service are called out at the end of this Part.

### `base/kustomization.yaml`
Sets `namespace: <service-namespace>`. Resource list order matters for readability but *not* for apply order — ArgoCD's own sync-wave/hook annotations control that (see below), kustomize just concatenates.

### `base/namespace.yaml`
Same PSS-`restricted` pattern as the frontend's. One per service — 5 separate namespace objects, all in the `clusterResourceWhitelist`'s `Namespace` entry (a single whitelist rule covers all of them, since AppProject rules match by `kind`, not by name).

### `base/configmap.yaml`
Non-secret config only: `DB_PORT`, `DB_NAME`, `APP_PORT`, and for `api-gateway` specifically, the other services' internal DNS names (`http://catalog-service.catalog.svc.cluster.local`, etc.) plus `FRONTEND_URL` for its CORS allow-list — the only origin the gateway will accept browser requests from, stamped by `scripts/configure.py` the same way the Ingress hosts are.

### `base/external-secret.yaml`
Per-service `ExternalSecret` (e.g. `catalog-db-secret`), pulling from a per-service Secrets Manager path (`/bookstore/catalog-db-credentials`) — `DB_USERNAME`/`DB_PASSWORD`/`DB_HOST`. `PreSync`, `sync-wave: "-1"` — one wave *after* the `ClusterSecretStore`'s `-2` (must exist first) and at the *same* wave as `admin-db-secret.yaml` and *before* `schema-init-job.yaml`'s implicit wave `0`. Getting this ordering wrong was a real incident: without it, the schema-init Job failed with `CreateContainerConfigError: secret … not found`, retried for 15 minutes, never self-resolved — see `TROUBLESHOOTING.md` OBS-013. `refreshInterval: 1h` — ESO polls Secrets Manager hourly and rotates the K8s Secret automatically on change, no redeploy needed.

### `base/admin-db-secret.yaml`
Pulls the *shared RDS admin* credentials (`/bookstore/db-credentials` — same secret `terraform/main.tf` creates once) a second time, into this service's own namespace, so `schema-init-job.yaml` can create its schema/user without a manual cross-namespace `kubectl get secret | kubectl apply` step. No new IAM permissions needed — the `ClusterSecretStore`'s IRSA role is already scoped to all of `/bookstore/*`. Same `PreSync`/`-1` wave as `external-secret.yaml`, same reasoning.

### `base/schema-init-job.yaml`
An ArgoCD `PreSync` hook `Job` (default wave `0`, i.e. after both `-1`-wave secrets). Hooks are exempt from ArgoCD's normal selfHeal/prune diffing — necessary because Jobs are *immutable* once created; a plain (non-hook) Job here would make every subsequent sync error trying to re-apply an unchanged-but-immutable spec. `hook-delete-policy: BeforeHookCreation,HookSucceeded` — not just `HookSucceeded`: a Job that *fails* is never cleaned up by `HookSucceeded` alone (it only fires on success), so every following sync would just keep waiting on the same permanently-broken Job forever instead of retrying. `BeforeHookCreation` deletes the previous hook resource — success or failure — before creating the next one, matching the SQL's own idempotent design (`CREATE ... IF NOT EXISTS`, `INSERT ... ON DUPLICATE KEY UPDATE`, guarded seed-count check) — see `TROUBLESHOOTING.md` OBS-015.

Two sharp edges baked into the inline SQL, both from real incidents:
- The heredoc is unquoted (`<<SQL`, not `<<'SQL'`) so `$CATALOG_DB_PASSWORD` expands — but an unquoted heredoc *also* treats bare backticks as shell command substitution. An earlier version had literal `` `desc` `` (a MySQL reserved-word column name needing backtick-quoting) run as the shell command `desc` three times instead of staying literal text — silently stripping the identifier from every statement that used it. Fixed by escaping every backtick as `` \` `` (a literal backtick to the shell; MySQL still sees a quoted identifier). See `TROUBLESHOOTING.md` OBS-003 / OBS-017.
- The seed-insert is guarded on `SELECT COUNT(*) FROM catalog_db.books = 0`, not on "these two specific titles are missing" — because this Job runs on *every* sync (idempotent-by-design, not one-time), and an admin who deliberately deletes a default book should see it stay deleted, not get silently re-inserted on the next ArgoCD sync.

`Job`'s pod template carries `labels: app: catalog-service` — without this exact label the namespace's `default-deny-all` NetworkPolicy blocks its egress to RDS outright, since it wouldn't match `catalog-service-policy`'s `podSelector`.

### `base/deployment.yaml`
`replicas: 1` (not 2+ like the frontend — see the PDB note below). `prometheus.io/scrape`/`port`/`path` annotations — the mechanism Prometheus on `monitoring-ec2` actually uses to discover this pod (via the EKS API server's pod-proxy, since pod IPs aren't reachable from outside the cluster network — see [[terraform-explained.md]]'s `monitoring-ec2` section). `runAsUser: 1001` (Node.js images' conventional non-root UID, distinct from the frontend's nginx-specific `101`). Env vars split cleanly: `DB_HOST`/`DB_USERNAME`/`DB_PASSWORD` from the `ExternalSecret`-backed Secret, `DB_PORT`/`DB_NAME`/`APP_PORT` from the plain `ConfigMap` — secrets and config never mixed in the same source. `readinessProbe`/`livenessProbe` both hit `/health` on 3000, with `livenessProbe`'s longer `initialDelaySeconds: 30` giving the app strictly more time before Kubernetes considers restarting it outright, vs. readiness's 10s just gating traffic.

### `base/service.yaml`
`ClusterIP`, port 80 → container port 3000. Internal-only — no service in this tree is ever `LoadBalancer` or `NodePort`; the only path in from outside the cluster is the shared ALB via `api-gateway`'s own `Ingress`.

### `base/hpa.yaml`
`catalog-service`: `minReplicas: 1`, `maxReplicas: 5`, both CPU (70%) *and* memory (80%) triggers — the only backend service with a memory trigger, reflecting that catalog reads/serves larger payloads (book cover URLs, descriptions) than the others. The rest scale on CPU only.

### `base/network-policy.yaml`
Same `default-deny-all` + one scoped allow policy pattern as the frontend, but pod-to-pod traffic uses `namespaceSelector` (matching `kubernetes.io/metadata.name`), not `ipBlock` — unlike the ALB-to-pod hop, this traffic genuinely originates from another pod's real address inside the cluster, so `namespaceSelector` is the correct match here (vs. `ipBlock` for the ALB, see frontend section and `api-gateway`'s own comment on this same distinction). Egress to RDS uses the VPC CIDR block (`170.20.0.0/16`) as an `ipBlock` — RDS isn't a pod, so `namespaceSelector` isn't an option there regardless. `api-gateway-policy` specifically calls out one more sharp edge: egress rules matching ports must use **3000** (the pod's real `containerPort`), not **80** (the `Service`'s port) — NetworkPolicy egress matches the actual destination pod port *after* `kube-proxy`'s Service DNAT, not the Service's advertised port. An earlier version used 80 here, which would have silently blocked all `api-gateway → microservice` traffic the moment a NetworkPolicy-enforcing CNI was turned on. See `TROUBLESHOOTING.md` OBS-049.

### `base/pdb.yaml`
`maxUnavailable: 1`, **not** `minAvailable: 1` — deliberate, and the opposite of the frontend's PDB. These services run `replicas: 1` with no overlay bumping it; `minAvailable: 1` equal to the *total* replica count would mean Kubernetes could never permit a voluntary eviction of the only pod at all — `kubectl drain`, an EKS managed-node-group upgrade, or Cluster Autoscaler consolidation on that node would hang indefinitely waiting on a budget that can never be satisfied. `maxUnavailable: 1` still creates a real PDB object (blocks *simultaneous* multi-pod disruption where relevant) without blocking routine single-node maintenance on a single-replica service. See `TROUBLESHOOTING.md` OBS-049 (same incident ID as the NetworkPolicy port sharp edge above — one troubleshooting session, two related fixes).

### `base/limitrange.yaml`
Identical values to the frontend's (50m/64Mi request, 250m/128Mi limit default) — same backstop-not-active-constraint reasoning, just duplicated per namespace since kustomize bases don't share cluster-scoped-adjacent resources across trees.

### `overlays/prod/kustomization.yaml`
Same `images:` rewrite pattern as the frontend overlay — CI's `kustomize edit set image bookstore-<service>=${ECR_REGISTRY}/bookstore-<service>:${SHA}` on every push, `000000000000` placeholder for manual local deploys.

### Per-service differences from `catalog-service`
- **`user-service`** — deployment adds a `JWT_SECRET` env var (the same shared HS256 secret `api-gateway` verifies against; `user-service` is the only one that *signs* tokens). No `schema-init-job.yaml` peer difference — same pattern, own schema.
- **`order-service`** — deployment adds `NOTIFICATION_SERVICE_URL` (config, not secret) since placing an order triggers a notification. NetworkPolicy egress includes an extra `namespaceSelector` rule to the `notification` namespace on top of the RDS/DNS rules every service has.
- **`notification-service`** — NetworkPolicy ingress is scoped to the `order` namespace only (not `gateway` like the other three) — nothing calls notification-service directly from the gateway; it's purely an internal callee of `order-service`.
- **`api-gateway`** — the odd one out structurally: no `schema-init-job.yaml`/`admin-db-secret.yaml` at all (it owns no database), but it does own its own `ingress.yaml` (see below) and a distinct `external-secret.yaml` pulling `JWT_SECRET` instead of DB credentials. `replicas: 2` (the only backend service not at 1 — it's the single point every external request passes through, so it doesn't get the "single-replica is fine" treatment the DB-backed services get). Larger resource requests/limits (100m/128Mi request, 500m/256Mi limit vs. 50m/64Mi and 250m/128Mi elsewhere) to match.

### `api-gateway/base/ingress.yaml`
The other half of the shared-ALB pair with `k8s/base/ingress/ingress.yaml` (same `group.name: bookstore`). One extra annotation the frontend's Ingress doesn't need: `alb.ingress.kubernetes.io/healthcheck-path: /health` — without it the ALB target-group health check defaults to path `/`, which `api-gateway`'s `app.js` never implements (only `/health`). Every target would sit permanently unhealthy (404 on every check) and the ALB would never route real traffic to it, even with the pods themselves fully `Running`/`Ready` per Kubernetes — a gap invisible from `kubectl get pods` alone.

---

## Part 3 — ArgoCD wiring (`k8s/argocd/`)

### `appproject.yaml`
Scopes both `Application`/`ApplicationSet` below to exactly what they use: one source repo, the cluster's own API server as the only allowed destination, the 6 real namespaces (`bookstore` + the 5 backend ones), and a `clusterResourceWhitelist` of exactly 3 kinds — `Namespace`, `StorageClass`, `ClusterSecretStore`. Omitting `clusterResourceWhitelist` entirely denies *all* cluster-scoped resources by default; without these 3 entries, `namespace.yaml` (every tree includes one), `storageclass/gp3.yaml`, and every `ExternalSecret`'s referenced `ClusterSecretStore` would all fail to sync. Applied by Terraform (`kubectl_manifest.argocd_appproject` in `terraform/argocd.tf`) *before* the `Application`/`ApplicationSet` that reference it — this genuinely was a manual-only step for a while and got missed on a from-scratch rebuild, see `TROUBLESHOOTING.md` OBS-058.

### `application.yaml`
The frontend's ArgoCD `Application`. `source.path: k8s/overlays/prod`, `kustomize: {}` (ArgoCD runs `kustomize build` itself, no separate Helm/Jsonnet step). `targetRevision` and `repoURL` are both stamped from `config.env` by `scripts/configure.py` — never hand-edited, since a branch/repo mismatch against what's actually deployed breaks ArgoCD sync outright (`unable to resolve '<branch>' to a commit SHA`). `syncPolicy.automated: { prune: true, selfHeal: true }` — deletes resources removed from git *and* reverts any manual `kubectl edit` back to match git, both directions of drift covered. `retry` backoff (5s → doubling → capped at 3m, 5 attempts) absorbs transient failures (an in-flight secret rotation, a slow-starting dependency) without a human re-triggering sync by hand.

**The one thing worth internalizing about this whole GitOps setup:** ArgoCD syncs from the **pushed git branch**, never from local disk. `scripts/configure.py` stamping real values into these files locally does nothing for a live cluster until that commit is actually pushed to `targetRevision`. This tripped up a real apply/destroy cycle test — `configure.py` had run locally, but with nothing pushed, ArgoCD kept applying the placeholder-value manifests from git, and `external-secrets` logs showed a literal `"region":"AWS_REGION_HERE"` in its provider config. Only `argocd.tf`'s own `kubectl_manifest` resources (the `AppProject`, and a couple of bootstrap objects) read local disk directly — every actual workload manifest goes through git first.

### `applicationset-microservices.yaml`
One `ApplicationSet` generating 5 `Application` objects — a `list` generator with one `{service, namespace}` pair per backend service, rather than 5 hand-written `Application` files. Adding a 6th microservice later means adding one list element here, not copy-pasting a whole new `Application` manifest. Template's `source.path: 'k8s/services/{{service}}/overlays/prod'` and `destination.namespace: '{{namespace}}'` interpolate per-generated-Application. Same `syncPolicy`/`retry`/`targetRevision`-stamping pattern as `application.yaml`, same underlying reason each choice was made.

---

## Part 4 — Cross-cutting patterns worth naming once

These show up identically across every tree above; called out once here instead of re-explained per file.

- **Sync-wave ordering**: `ClusterSecretStore` (`-2`) → per-service `ExternalSecret`s (`-1`) → `schema-init-job.yaml` (`0`, implicit) → everything else (`0`, implicit, plain Sync phase). All PreSync hooks run strictly before any Sync-phase resource, in ascending wave order within that phase — getting a wave number wrong doesn't cause an occasional race, it causes a **guaranteed** failure on a from-scratch cluster (see OBS-013, OBS-030 above), because there's no existing state to accidentally paper over the ordering bug.
- **NetworkPolicy shape**: every namespace gets `default-deny-all` (empty `podSelector`, both policy types) plus exactly one scoped policy per real workload. `ipBlock: 170.20.0.0/16` for anything crossing the ALB or RDS boundary (neither is a pod K8s can label-select); `namespaceSelector` for genuine pod-to-pod traffic within the cluster. Egress to DNS (53/UDP+TCP) is present in literally every policy in this repo — the one rule that's never optional, since without it nothing can even resolve a Service's ClusterIP DNS name.
- **PDB shape by replica count**: `replicas ≥ 2` → `minAvailable: 1` is safe (frontend, and implicitly api-gateway's 2 replicas — though api-gateway currently has no PDB file of its own, worth noting as a gap against the pattern). `replicas: 1` → must use `maxUnavailable`, never `minAvailable` equal to total replicas, or voluntary disruptions (node drain, node-group upgrade, autoscaler consolidation) hang forever (OBS-049).
- **Security context floor**: every container in every tree sets `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]`, `seccompProfile: RuntimeDefault`, and `automountServiceAccountToken: false` at the pod level (nothing here talks to the K8s API directly). Combined with each namespace's PSS `restricted` label, this is enforced twice over — once at admission (PSS) and once by each manifest's own explicit values (defense in depth, not redundant, since PSS alone wouldn't catch a missing resource request/limit the way `LimitRange` does).
- **Secrets vs. config split**: `ExternalSecret` → K8s `Secret` → `secretKeyRef` for anything from AWS Secrets Manager (DB creds, JWT secret); plain `ConfigMap` → `configMapKeyRef` for everything else (ports, DB names, internal service URLs, CORS origin). Never mixed in the same source object anywhere in this tree.
- **What's *not* here**: no `StatefulSet` anywhere (RDS is the only stateful component, and it's Terraform-managed, outside `k8s/` entirely) — no PVC currently claims the `gp3` `StorageClass` despite it being defined. No `ServiceMesh`/sidecar of any kind (Istio, Linkerd) — NetworkPolicy plus the ALB's own TLS termination is the full extent of in-cluster traffic control today, which is exactly the gap [[terraform-explained.md]]'s Part 4 gap-analysis (mTLS/mesh) was written against.

---

## Part 5 — DR overlay tree (`dr` branch, `terraform/dr-standby.tf`'s `var.enable_dr_standby`)

Same manifests, different destination cluster — nothing here is new application content, only new kustomize entry points and one patch. Deployed by `terraform/dr-standby.tf`'s own ArgoCD bootstrap (`kubectl_manifest.argocd_appproject_dr`/`application_dr`/`applicationset_microservices_dr`, `provider = kubectl.secondary`), a fully independent ArgoCD instance running inside the DR cluster itself — not the primary's ArgoCD reaching across regions.

### `k8s/argocd/dr/appproject.yaml`, `application.yaml`, `applicationset-microservices.yaml`
Structurally identical to their `k8s/argocd/` counterparts — same `sourceRepos`/`clusterResourceWhitelist` shape, same `syncPolicy`. Two differences: `source.path` points at `k8s/overlays/dr` (frontend) / `k8s/services/{{service}}/overlays/dr` (the `ApplicationSet`'s per-service template) instead of `.../prod`, and `destination.server` is still `https://kubernetes.default.svc` — correct as written, since that's *this* cluster's own in-cluster API server address from the DR ArgoCD's own point of view, not a cross-cluster reference. `repoURL`/`targetRevision` are stamped by `scripts/configure.py` the exact same way the primary `k8s/argocd/*.yaml` files are (Part 3's "ArgoCD wiring" section — `configure.py` didn't originally reach these nested `dr/` files at all; that gap was closed alongside this branch's live bug-fixing pass, see [[terraform-explained.md]] Part 5's bug table, item 3).

### `k8s/overlays/dr/kustomization.yaml`
Same shape as `k8s/overlays/prod/kustomization.yaml` (`resources: [../../base, hpa-frontend.yaml]` + an `images:` block), plus one thing `prod` doesn't have: a strategic-merge-style JSON patch on the base `ClusterSecretStore` —

```yaml
patches:
  - target: { group: external-secrets.io, kind: ClusterSecretStore, name: aws-secretsmanager }
    patch: |
      - op: replace
        path: /spec/provider/aws/region
        value: us-west-2
```

— pointing the DR cluster's ExternalSecrets Operator at the *secondary* region's Secrets Manager, where the primary's secrets are readable as cross-region `replica{}` copies (see [[terraform-explained.md]] Part 5's `dr-standby.tf` section). Without this patch the DR cluster's ClusterSecretStore would try to read `us-west-1`'s Secrets Manager from inside a `us-west-2` cluster — cross-region reads aren't how Secrets Manager IRSA scoping works here, so every ExternalSecret would fail to sync. `images:` block's `newName` points at the `us-west-2` ECR replica (`modules/ecr`'s `secondary_region` replication), not the primary registry — a genuinely different registry, not just a different tag.

### `k8s/services/*/overlays/dr/kustomization.yaml` (× 5)
One per backend microservice, same shape as its `overlays/prod` sibling — `resources: [../../base]` + an `images:` block pointing at that service's own `us-west-2` ECR replica. No `ClusterSecretStore` patch here (that only exists once, in the frontend tree's `k8s/base/secrets/external-secret.yaml` — every namespace's `ExternalSecret` references the *same* `ClusterSecretStore` by name via `secretStoreRef`, so patching it once in the frontend overlay is enough; the backend overlays don't need their own copy of that patch).

**Known, confirmed-real gap** (not fixed this session): CI's deploy job (`.github/workflows/ci-cd.yml`) only ever runs `kustomize edit set image` against `overlays/prod` paths — never `overlays/dr`. A push to `dr` builds and pushes fresh images to the primary registry's tag, but the DR overlays' `newTag` stays frozen at whatever `scripts/configure.py` stamped at initial setup. The DR cluster's ArgoCD keeps syncing that stale tag forever until someone manually re-runs `configure.py` or hand-edits these 6 files. See `docs/DR-STANDBY-PLAN.md`'s "Not done" list, item 4.

## Apply order (real dependency graph, not directory order)

1. `terraform/argocd.tf` applies the `AppProject` directly (`kubectl_manifest`, reads local disk).
2. `terraform/argocd.tf` applies `application.yaml` + `applicationset-microservices.yaml` directly, same way.
3. ArgoCD's own controller takes over from here — polls `targetRevision` every 3 minutes (or reacts instantly to a hard `argocd.argoproj.io/refresh=hard` annotation), runs `kustomize build` against each `source.path`, and reconciles.
4. Within each Application's sync: `ClusterSecretStore` (wave `-2`) → per-service `ExternalSecret`s (wave `-1`) → `schema-init-job.yaml` Jobs (wave `0`, PreSync) → every remaining plain resource (Deployment, Service, Ingress, NetworkPolicy, HPA, PDB — wave `0`, Sync phase).
5. `selfHeal: true` means step 3 onward repeats forever, correcting any drift, for the life of the cluster.

**With `enable_dr_standby=true`**: steps 1–5 above repeat independently inside the DR cluster, using the DR ArgoCD instance and `k8s/argocd/dr/*.yaml` — running *concurrently* with the primary chain, not after it (see [[terraform-explained.md]] Part 5's own apply-order section). The two clusters' ArgoCD instances never talk to each other; each just reconciles its own overlay path against the same git repo, independently, forever.
