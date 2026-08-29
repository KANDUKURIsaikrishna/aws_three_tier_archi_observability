# DR failover runbook — us-west-1 → us-west-2

Prerequisite: the standby stack is deployed (`enable_dr_standby = true`,
`terraform apply` completed) and the secondary EKS cluster is running the
same workloads. See [DR-STANDBY-PLAN.md](DR-STANDBY-PLAN.md).

Route53 already carries a `SECONDARY` failover A/CNAME record (see
`terraform/modules/route53/main.tf`). It serves automatically once the
`aws_route53_health_check.primary` on the primary ALB goes unhealthy — no
manual DNS step for traffic cutover. The manual work is promoting the
database and confirming the standby is production-sized.

---

## 1. Confirm it's a real region loss

- `aws eks describe-cluster --name bookstore-eks --region us-west-1` — timeout / 5xx, not just slow.
- AWS Health Dashboard / status page shows a us-west-1 event.
- Route53 health check `bookstore-primary` is `Failure` in the console.

If the primary is flapping (health check unhealthy but region is up), do
**not** promote — a promoted replica can't be un-promoted, and you'd then
have two writable databases diverging. Wait it out or fix forward.

---

## 2. Promote the RDS read replica

```bash
aws rds promote-read-replica \
  --db-instance-identifier bookstore-db-dr \
  --region us-west-2

# wait until it reports "available" (a few minutes; it takes one reboot)
aws rds wait db-instance-available \
  --db-instance-identifier bookstore-db-dr \
  --region us-west-2
```

The replica becomes a standalone writable instance. Its endpoint does not
change.

---

## 3. Point the standby services at the promoted DB

Services in the standby cluster connect via the private DNS name
`db.bookstore.internal` (secondary-region private hosted zone → replica
endpoint). Promotion doesn't change the endpoint, so **no redeploy is
needed** — the same name is now writable.

Verify:

```bash
aws eks update-kubeconfig --name bookstore-eks-dr --region us-west-2
kubectl -n order exec deploy/order-service -- \
  sh -c 'nslookup db.bookstore.internal && echo ok'
```

If a service still holds a dead connection pool to the old primary, restart
it: `kubectl -n <ns> rollout restart deploy/<svc>`.

---

## 4. Confirm traffic is on the standby

```bash
dig +short api.bookstore.<domain>          # should resolve to the us-west-2 ALB
curl -sS https://api.bookstore.<domain>/healthz
```

Then click through the real UI: login → add to cart → checkout → order
history (same check as `docs/DEPLOYMENT.md`).

---

## 5. Scale the standby to production sizing

The DR node group defaults to `dr_node_desired_size = 2` (hot standby).
Under real traffic, raise it:

```bash
# via Terraform (preferred — keeps state honest)
terraform apply -var enable_dr_standby=true -var dr_node_desired_size=3

# or immediately, then reconcile Terraform after
aws eks update-nodegroup-config --cluster-name bookstore-eks-dr \
  --nodegroup-name bookstore-dr-node-group \
  --scaling-config minSize=2,maxSize=4,desiredSize=3 --region us-west-2
```

HPAs and `topologySpreadConstraints` are already in the manifests, so pods
spread as nodes come up.

---

## 6. Alerting

The primary-region monitoring EC2 is gone with the region. The standby has
its own monitoring EC2 (Prometheus/Grafana/Alertmanager). SES for alert
email is still primary-region unless mirrored — if alert email matters
during the incident, verify a us-west-2 SES identity or switch Alertmanager
to a webhook (Slack/PagerDuty) receiver.

---

## 7. Failing back (after us-west-1 recovers)

Do **not** rush this. Order:

1. Rebuild us-west-1 as the new **standby**: `terraform apply` with the
   primary region's compute, and a fresh read replica **from the promoted
   us-west-2 instance** (reverse the replication direction — update
   `replicate_source_db` to point at `bookstore-db-dr`).
2. Let it catch up (replica lag → 0).
3. During a maintenance window: stop writes, promote the us-west-1 replica,
   flip `db.bookstore.internal` in the primary private zone, let the
   Route53 health check recover so the `PRIMARY` record serves again.
4. Rebuild us-west-2 back into the standby role.

Fail-back is a planned change, not an emergency one.
