# ═════════════════════════════════════════════════════════════════════════════
# DR standby region — full active-passive copy of the platform.
#
# EVERYTHING here is count/for_each-gated on var.enable_dr_standby. Off (the
# default) => zero resources, zero cost, and no plan diff on a single-region
# apply. See docs/DR-STANDBY-PLAN.md and docs/DR-FAILOVER-RUNBOOK.md.
#
# Providers: aws.secondary (providers.tf) + helm/kubernetes/kubectl .secondary
# (providers-dr.tf, wired from module.eks_dr).
#
# NOT plan-verified in-repo (needs AWS creds + a two-region `terraform plan`).
# Known things to check on the first real plan are tagged `# PLAN-CHECK:`.
# ═════════════════════════════════════════════════════════════════════════════

locals {
  dr = var.enable_dr_standby
}

# ── Secondary-region AZs + subnet layout (mirrors locals.tf for the primary) ──
data "aws_availability_zones" "dr" {
  count    = local.dr ? 1 : 0
  provider = aws.secondary
  state    = "available"
}

locals {
  dr_az_a = local.dr ? data.aws_availability_zones.dr[0].names[0] : ""
  dr_az_b = local.dr ? data.aws_availability_zones.dr[0].names[1] : ""

  # Same CIDR block as the primary VPC. Fine — regions are isolated; only
  # matters if you ever peer them (out of scope, see DR-STANDBY-PLAN.md).
  dr_public_subnets = local.dr ? [
    { cidr = "170.20.1.0/24", az = local.dr_az_a },
    { cidr = "170.20.2.0/24", az = local.dr_az_b },
  ] : []
  dr_private_subnets = local.dr ? [
    { cidr = "170.20.3.0/24", az = local.dr_az_a }, # [0-3] EKS nodes
    { cidr = "170.20.4.0/24", az = local.dr_az_b },
    { cidr = "170.20.5.0/24", az = local.dr_az_a },
    { cidr = "170.20.6.0/24", az = local.dr_az_b },
    { cidr = "170.20.7.0/24", az = local.dr_az_a }, # [4-5] RDS replica
    { cidr = "170.20.8.0/24", az = local.dr_az_b },
  ] : []
  dr_eks_node_subnet_cidrs = [for s in slice(local.dr_private_subnets, 0, 4) : s.cidr]

  # DR ALB hostname, discovered within the apply (below). Fed to
  # module.route53's secondary_alb_dns so the SECONDARY failover record points
  # at the real DR ALB instead of needing a manual var.
  dr_discovered_alb_dns = try(
    data.kubernetes_ingress_v1.dr_bookstore[0].status[0].load_balancer[0].ingress[0].hostname,
    ""
  )

  # Wildcard ARNs for the primary's Secrets Manager replicas in the secondary
  # region — the DR monitoring EC2's IAM policy needs GetSecretValue on these.
  # (Secrets Manager appends a random 6-char suffix, hence -*.)
  dr_secret_arn_prefix = "arn:aws:secretsmanager:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:secret:"
}

# ── Destroy-time ENI / cluster-SG cleanup for the DR VPC (mirrors main.tf) ────
resource "null_resource" "cleanup_eks_networking_dr" {
  count = local.dr ? 1 : 0
  triggers = {
    vpc_id = module.network_dr[0].vpc_id
    region = var.secondary_region
  }
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["python3"]
    command     = "${path.module}/../scripts/cleanup_eks_networking.py"
    environment = {
      VPC_ID = self.triggers.vpc_id
      REGION = self.triggers.region
    }
  }
}

# ── Network ─────────────────────────────────────────────────────────────────
module "network_dr" {
  count     = local.dr ? 1 : 0
  source    = "./modules/network"
  providers = { aws = aws.secondary }

  vpc_cidr        = local.vpc_cidr
  public_subnets  = local.dr_public_subnets
  private_subnets = local.dr_private_subnets
  # per-AZ NAT (module default). Set single_nat_gateway = true here to trade
  # the standby's NAT HA for cost until failover.
}

# ── Security groups ─────────────────────────────────────────────────────────
module "security_groups_dr" {
  count     = local.dr ? 1 : 0
  source    = "./modules/security"
  providers = { aws = aws.secondary }

  vpc_id               = module.network_dr[0].vpc_id
  prefix               = "bookstore-dr"
  eks_node_cidr_blocks = local.dr_eks_node_subnet_cidrs
}

# ── EKS ─────────────────────────────────────────────────────────────────────
module "eks_dr" {
  count     = local.dr ? 1 : 0
  source    = "./modules/eks"
  providers = { aws = aws.secondary, tls = tls }

  cluster_name    = "bookstore-eks-dr"
  cluster_version = "1.31"
  prefix          = "bookstore-dr"
  depends_on      = [null_resource.cleanup_eks_networking_dr]

  subnet_ids = [
    module.network_dr[0].private_subnet_ids[0],
    module.network_dr[0].private_subnet_ids[1],
    module.network_dr[0].private_subnet_ids[2],
    module.network_dr[0].private_subnet_ids[3],
  ]
  node_instance_type = "t3.medium"
  node_min_size      = var.dr_node_min_size
  node_max_size      = var.dr_node_max_size
  node_desired_size  = var.dr_node_desired_size
  region             = var.secondary_region

  admin_principal_arns = concat(
    [data.aws_caller_identity.current.arn],
    var.extra_admin_principal_arns
  )
}

# ── EKS add-ons (ESO, ALB controller, ArgoCD, Argo Rollouts) ────────────────
module "eks_addons_dr" {
  count  = local.dr ? 1 : 0
  source = "./modules/eks-addons"
  providers = {
    aws    = aws.secondary
    helm   = helm.secondary
    random = random
    null   = null
  }

  cluster_name      = module.eks_dr[0].cluster_name
  oidc_provider_arn = module.eks_dr[0].oidc_provider_arn
  oidc_provider_url = module.eks_dr[0].oidc_provider_url
  aws_region        = var.secondary_region
  node_role_name    = module.eks_dr[0].node_role_name

  # The primary's grafana-admin / monitoring-basic-auth already exist in this
  # region as cross-region replicas — don't recreate them (name collision).
  create_monitoring_secrets = false
  replica_region            = ""

  depends_on = [module.eks_dr, module.network_dr]
}

# ── RDS: promotable cross-region read replica ───────────────────────────────
resource "aws_kms_key" "dr_rds" {
  count                   = local.dr ? 1 : 0
  provider                = aws.secondary
  description             = "bookstore RDS cross-region read replica encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  tags                    = { Name = "bookstore-dr-rds" }
}

resource "aws_db_subnet_group" "dr_replica" {
  count      = local.dr ? 1 : 0
  provider   = aws.secondary
  name       = "bookstore-dr-replica"
  subnet_ids = [module.network_dr[0].private_subnet_ids[4], module.network_dr[0].private_subnet_ids[5]]
  tags       = { Name = "bookstore-dr-replica" }
}

resource "aws_security_group" "dr_replica" {
  count       = local.dr ? 1 : 0
  provider    = aws.secondary
  name        = "bookstore-dr-rds"
  description = "DR RDS read replica — MySQL from the DR VPC only"
  vpc_id      = module.network_dr[0].vpc_id

  ingress {
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [local.vpc_cidr]
    description = "MySQL from the DR VPC"
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound"
  }
  tags = { Name = "bookstore-dr-rds" }
}

resource "aws_db_instance" "dr_replica" {
  count    = local.dr ? 1 : 0
  provider = aws.secondary

  identifier = "bookstore-db-dr"
  # Cross-region replica: replicate_source_db must be the source instance ARN
  # (identifier only works same-region). Source needs backup_retention_period
  # > 0 — it's 7 (main.tf).
  # PLAN-CHECK: the source RDS uses the AWS-managed key (module.rds kms_key_arn
  # is null). A cross-region encrypted replica needs a destination CMK
  # (kms_key_id below) — that is allowed for an AWS-managed-key source. If
  # RDS rejects it, set var.kms_key_arn on the primary module.rds to a CMK.
  replicate_source_db = module.rds.rds_instance_arn

  instance_class         = "db.t3.micro"
  db_subnet_group_name   = aws_db_subnet_group.dr_replica[0].name
  vpc_security_group_ids = [aws_security_group.dr_replica[0].id]
  kms_key_id             = aws_kms_key.dr_rds[0].arn
  storage_encrypted      = true

  auto_minor_version_upgrade = true
  apply_immediately          = true
  skip_final_snapshot        = true
  deletion_protection        = false

  tags = { Name = "bookstore-db-dr" }
}

# Private DNS for the replica, same name the primary zone uses, so the DR
# cluster's services connect to `db.bookstore.internal` and failover needs no
# redeploy. PLAN-CHECK: confirm modules/route53 exposes/creates the matching
# `db.bookstore.internal` record in the primary private zone; if it still
# uses the raw endpoint, the per-service secret JSON in main.tf must switch
# DB_HOST to this name in both regions (see DR-STANDBY-PLAN.md item 5).
resource "aws_route53_zone" "dr_rds_private" {
  count    = local.dr ? 1 : 0
  provider = aws.secondary
  name     = "bookstore.internal"
  vpc {
    vpc_id = module.network_dr[0].vpc_id
  }
  tags = { Name = "bookstore-dr-rds-private" }
}

resource "aws_route53_record" "dr_rds_endpoint" {
  count    = local.dr ? 1 : 0
  provider = aws.secondary
  zone_id  = aws_route53_zone.dr_rds_private[0].zone_id
  name     = "db.bookstore.internal"
  type     = "CNAME"
  ttl      = 60
  records  = [aws_db_instance.dr_replica[0].address]
}

# ── Monitoring EC2 (its own — the primary's box is in the region that died) ──
resource "aws_eip" "monitoring_dr" {
  count    = local.dr ? 1 : 0
  provider = aws.secondary
  domain   = "vpc"
  tags     = { Name = "bookstore-dr-monitoring-eip" }
}

module "monitoring_ec2_dr" {
  count     = local.dr ? 1 : 0
  source    = "./modules/monitoring-ec2"
  providers = { aws = aws.secondary, tls = tls }

  vpc_id            = module.network_dr[0].vpc_id
  vpc_cidr          = local.vpc_cidr
  public_subnet_id  = module.network_dr[0].public_subnet_ids[0]
  eip_allocation_id = aws_eip.monitoring_dr[0].id
  cluster_name      = module.eks_dr[0].cluster_name
  region            = var.secondary_region
  eks_cluster_sg_id = module.eks_dr[0].cluster_security_group_id
  eks_api_server    = module.eks_dr[0].cluster_endpoint
  admin_cidr_blocks = var.monitoring_admin_cidr

  # Secrets are the primary's cross-region replicas (resolved by name in the
  # secondary region); IAM policy needs GetSecretValue on the replica ARNs.
  grafana_admin_secret_arn  = "${local.dr_secret_arn_prefix}/bookstore/grafana-admin-*"
  grafana_admin_secret_name = "/bookstore/grafana-admin"

  monitoring_basic_auth_secret_arn  = "${local.dr_secret_arn_prefix}/bookstore/monitoring-basic-auth-*"
  monitoring_basic_auth_secret_name = "/bookstore/monitoring-basic-auth"

  alertmanager_smtp_secret_arn  = "${local.dr_secret_arn_prefix}/bookstore/alertmanager-smtp-*"
  alertmanager_smtp_secret_name = "/bookstore/alertmanager-smtp"

  depends_on = [module.eks_dr, module.eks_addons_dr]
}

# ── ACM cert for the DR ALB (regional, secondary provider) ──────────────────
resource "aws_acm_certificate" "ingress_dr" {
  count                     = local.dr ? 1 : 0
  provider                  = aws.secondary
  domain_name               = var.domain
  subject_alternative_names = ["*.${var.domain}", "*.bookstore.${var.domain}"]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

# DNS validation records go in the (global) public hosted zone — written with
# the default provider. Same domain as the primary cert => identical CNAME
# name/value, so allow_overwrite handles the overlap.
resource "aws_route53_record" "ingress_dr_cert_validation" {
  for_each = local.dr ? {
    for dvo in aws_acm_certificate.ingress_dr[0].domain_validation_options :
    dvo.domain_name => { name = dvo.resource_record_name, record = dvo.resource_record_value, type = dvo.resource_record_type }
  } : {}

  zone_id         = module.route53.public_zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "ingress_dr" {
  count                   = local.dr ? 1 : 0
  provider                = aws.secondary
  certificate_arn         = aws_acm_certificate.ingress_dr[0].arn
  validation_record_fqdns = [for r in aws_route53_record.ingress_dr_cert_validation : r.fqdn]
}

# ── ArgoCD bootstrap for the DR cluster (points at k8s/**/overlays/dr) ───────
resource "kubectl_manifest" "argocd_appproject_dr" {
  count     = local.dr ? 1 : 0
  provider  = kubectl.secondary
  yaml_body = file("${path.module}/../k8s/argocd/dr/appproject.yaml")

  depends_on = [module.eks_addons_dr]
}

resource "kubectl_manifest" "argocd_application_dr" {
  count     = local.dr ? 1 : 0
  provider  = kubectl.secondary
  yaml_body = file("${path.module}/../k8s/argocd/dr/application.yaml")

  depends_on = [module.eks_addons_dr, kubectl_manifest.argocd_appproject_dr]
}

resource "kubectl_manifest" "argocd_applicationset_microservices_dr" {
  count     = local.dr ? 1 : 0
  provider  = kubectl.secondary
  yaml_body = file("${path.module}/../k8s/argocd/dr/applicationset-microservices.yaml")

  depends_on = [module.eks_addons_dr, kubectl_manifest.argocd_appproject_dr]
}

# ── Discover the DR ALB hostname (mirrors argocd.tf for the primary) ────────
resource "null_resource" "wait_for_dr_alb_hostname" {
  count = local.dr ? 1 : 0
  triggers = {
    argocd_application_id = kubectl_manifest.argocd_application_dr[0].id
  }
  depends_on = [module.eks_addons_dr, kubectl_manifest.argocd_application_dr]

  provisioner "local-exec" {
    interpreter = ["python3"]
    environment = {
      CLUSTER_NAME = module.eks_dr[0].cluster_name
      REGION       = var.secondary_region
    }
    command = "${path.module}/../scripts/wait_for_alb_hostname.py"
  }
}

data "kubernetes_ingress_v1" "dr_bookstore" {
  count    = local.dr ? 1 : 0
  provider = kubernetes.secondary
  metadata {
    name      = "bookstore-ingress"
    namespace = "bookstore"
  }
  depends_on = [null_resource.wait_for_dr_alb_hostname]
}
