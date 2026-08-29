.PHONY: init import plan apply destroy dr-plan dr-apply dr-destroy monitoring-status monitoring-logs monitoring-key

TF_DIR = terraform

# Only used by `import`'s SES identity line below -- read straight from
# config.env since aws_sesv2_email_identity's import ID is the email address
# itself, not a fixed path like the Secrets Manager imports above it.
ALERT_EMAIL = $(shell grep -E '^ALERT_EMAIL=' config.env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')

# ── Setup ─────────────────────────────────────────────────────────────────────

init:
	terraform -chdir=$(TF_DIR) init

# Import pre-existing secrets that Terraform can't create (state lost due to S3 backend).
# Run once per fresh state. || true prevents failure if already imported.
#
# aws_iam_role.cluster (the EKS cluster's own IAM role) deliberately isn't
# imported here, even though it can orphan the same way -- terraform import
# always resolves every configured provider up front, including the
# kubectl/helm/kubernetes ones, and those depend on module.eks.cluster_endpoint
# etc., which don't exist yet on exactly the kind of from-scratch apply where
# this role is most likely to be orphaned (a previous attempt died after
# creating the role but before the cluster itself came up). Importing it here
# would fail with "Invalid provider configuration" in that exact scenario.
# See TROUBLESHOOTING.md for the real recovery: delete the orphaned role via
# AWS CLI and let Terraform recreate an identical one -- nothing about an EKS
# cluster role's identity is worth preserving via import.
import:
	terraform -chdir=$(TF_DIR) import \
	  module.rds.aws_secretsmanager_secret.db_credentials \
	  /bookstore/db-credentials 2>/dev/null || echo "db-credentials already in state"
	terraform -chdir=$(TF_DIR) import \
	  module.eks_addons.aws_secretsmanager_secret.grafana_admin \
	  /bookstore/grafana-admin 2>/dev/null || echo "grafana-admin already in state"
	terraform -chdir=$(TF_DIR) import \
	  aws_secretsmanager_secret.jwt_secret \
	  /bookstore/jwt-secret 2>/dev/null || echo "jwt-secret already in state"
	# Same "state lost due to S3 backend" class of problem as the three
	# imports above -- confirmed live 2026-08-26 against an account with
	# two different state buckets from past testing
	# (bookstore-terraform-state-<acct> and a stale -v2). An apply against
	# whichever bucket ISN'T the one that originally created this identity
	# hits AlreadyExistsException on aws_sesv2_email_identity.alerts,
	# even though a real `terraform destroy` against the correct state
	# does clean it up properly (verified: it's gone from both AWS and
	# state after a real destroy, this import step is a no-op on that path).
	terraform -chdir=$(TF_DIR) import \
	  aws_sesv2_email_identity.alerts \
	  $(ALERT_EMAIL) 2>/dev/null || echo "SES email identity already in state (or ALERT_EMAIL not set)"

plan: init
	terraform -chdir=$(TF_DIR) plan

# Full automated deploy: init → import known conflicts → apply
apply: init import
	terraform -chdir=$(TF_DIR) apply -auto-approve

destroy:
	terraform -chdir=$(TF_DIR) destroy -auto-approve

# ── DR standby region (var.enable_dr_standby) ─────────────────────────────────
# The two regions provision/tear down concurrently -- higher -parallelism
# because a both-flags run has ~2x the resource count. See
# docs/DR-STANDBY-PLAN.md and docs/DR-FAILOVER-RUNBOOK.md.

dr-plan: init
	terraform -chdir=$(TF_DIR) plan -var enable_dr_standby=true -parallelism=20

dr-apply: init import
	terraform -chdir=$(TF_DIR) apply -auto-approve -var enable_dr_standby=true -parallelism=20

dr-destroy:
	terraform -chdir=$(TF_DIR) destroy -auto-approve -var enable_dr_standby=true -parallelism=20

# ── Monitoring helpers ────────────────────────────────────────────────────────

MONITORING_IP = $(shell terraform -chdir=$(TF_DIR) output -raw grafana_url 2>/dev/null | sed 's|http://||' | cut -d: -f1)
MONITORING_KEY = .monitoring-ssh-key.pem

# Fetch the auto-generated SSH private key from Terraform state and save it
# locally (mode 400, required by ssh/scp). Re-run any time it goes missing —
# idempotent, just re-reads the same state-stored key, doesn't regenerate it.
monitoring-key:
	@rm -f $(MONITORING_KEY)
	terraform -chdir=$(TF_DIR) output -raw monitoring_ssh_private_key > $(MONITORING_KEY)
	chmod 400 $(MONITORING_KEY)

# Tail the cloud-init log on the monitoring EC2
monitoring-logs: monitoring-key
	@echo "Tailing /var/log/monitoring-init.log on $(MONITORING_IP)"
	ssh -o StrictHostKeyChecking=no -i $(MONITORING_KEY) ubuntu@$(MONITORING_IP) \
	  "tail -f /var/log/monitoring-init.log /var/log/grafana-dashboard-import.log 2>/dev/null"

# Show Docker Compose status on the monitoring EC2
monitoring-status: monitoring-key
	@echo "Docker Compose status on $(MONITORING_IP)"
	ssh -o StrictHostKeyChecking=no -i $(MONITORING_KEY) ubuntu@$(MONITORING_IP) \
	  "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
